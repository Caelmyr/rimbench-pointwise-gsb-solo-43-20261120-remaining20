from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from .audit import AUDIT_BYTES, AUDIT_LIMIT, pending_review
from .domain import (
    Asset, InspectionError, MAX_PROJECTS, MAX_SNAPSHOTS, Project,
    check_revision, valid_id,
)


_SHA = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    sha256 TEXT PRIMARY KEY, width INTEGER NOT NULL, height INTEGER NOT NULL,
    pixels TEXT NOT NULL, byte_size INTEGER NOT NULL, stored_at TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS by_recency ON projects(updated_at DESC, id);
CREATE TABLE IF NOT EXISTS project_assets (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset TEXT NOT NULL REFERENCES assets(sha256),
    PRIMARY KEY(project_id, asset)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_for ON snapshots(project_id, captured_at DESC, id);
CREATE TABLE IF NOT EXISTS snapshot_assets (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    asset TEXT NOT NULL REFERENCES assets(sha256),
    PRIMARY KEY(snapshot_id, asset)
);
CREATE TABLE IF NOT EXISTS audits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audits_for ON audits(project_id, recorded_at DESC, id);
"""


def asset_id(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InspectionError("影像标识无效。", status=404, code="missing-image")
    return value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _load(raw):
    return json.loads(raw)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory/"inspection.sqlite3"
        with self.connection(write=True) as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def connection(self, *, write=False):
        connection = sqlite3.connect(self.database, timeout=8, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 8000")
            if write:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            if "locked" in str(exc).lower():
                raise InspectionError(
                    "另一个检验写入正在进行，请稍后重试。",
                    status=503, code="storage-busy",
                ) from exc
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def ping(self):
        with self.connection() as connection:
            return connection.execute("SELECT count(*) FROM projects").fetchone()[0]

    def project(self, identity, connection=None):
        identity = valid_id(identity, "样件ID")
        if connection is None:
            with self.connection() as active:
                return self.project(identity, active)
        row = connection.execute("SELECT data FROM projects WHERE id=?", (identity,)).fetchone()
        if not row:
            raise InspectionError("找不到此样件。", status=404, code="missing-project")
        return Project.from_data(_load(row["data"]))

    def list_projects(self):
        with self.connection() as connection:
            found = connection.execute("SELECT data FROM projects ORDER BY updated_at DESC, id").fetchall()
        return [Project.from_data(_load(row["data"])).summary() for row in found]

    def save_new(self, project, assets=()):
        with self.connection(write=True) as connection:
            count = connection.execute("SELECT count(*) FROM projects").fetchone()[0]
            if count >= MAX_PROJECTS:
                raise InspectionError(f"本工作站最多保存 {MAX_PROJECTS} 个样件。", status=409)
            self._insert_assets(connection, assets)
            connection.execute(
                "INSERT INTO projects(id, updated_at, data) VALUES (?, ?, ?)",
                (project.id, project.updated_at, _dump(project.data())),
            )
            self._sync_project_assets(connection, project)
        return project

    def change(self, identity, revision, fn, assets=()):
        with self.connection(write=True) as connection:
            before = self.project(identity, connection)
            check_revision(revision, before.revision)
            after = fn(before)
            if before == after:
                return before
            if after.id != before.id or after.revision != before.revision+1:
                raise RuntimeError("Project write must advance one revision")
            self._insert_assets(connection, assets)
            connection.execute(
                "UPDATE projects SET updated_at=?, data=? WHERE id=?",
                (after.updated_at, _dump(after.data()), after.id),
            )
            self._sync_project_assets(connection, after)
            self._remove_unused_assets(connection, self._project_assets(before)-self._project_assets(after))
            return after

    def remove_project(self, identity, revision):
        with self.connection(write=True) as connection:
            project = self.project(identity, connection)
            check_revision(revision, project.revision)
            keys = set(self._project_assets(project))
            keys.update(row[0] for row in connection.execute(
                "SELECT DISTINCT sa.asset FROM snapshot_assets AS sa JOIN snapshots AS s "
                "ON sa.snapshot_id=s.id WHERE s.project_id=?", (identity,),
            ))
            connection.execute("DELETE FROM projects WHERE id=?", (identity,))
            self._remove_unused_assets(connection, keys)

    def image(self, identity, connection=None):
        identity = asset_id(identity)
        if connection is None:
            with self.connection() as active:
                return self.image(identity, active)
        row = connection.execute(
            "SELECT sha256, width, height, pixels, byte_size, stored_at, payload FROM assets WHERE sha256=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise InspectionError("无法取得这张影像。", status=404, code="missing-image")
        return Asset(*tuple(row)[:-1]), bytes(row["payload"])

    def images_for(self, project, snapshot=None):
        query = (
            "SELECT asset FROM project_assets WHERE project_id=?" if snapshot is None
            else "SELECT asset FROM snapshot_assets s JOIN snapshots p "
                 "ON s.snapshot_id=p.id WHERE p.project_id=? AND s.snapshot_id=?"
        )
        args = (project,) if snapshot is None else (project, snapshot)
        with self.connection() as connection:
            ids = [row[0] for row in connection.execute(query, args)]
            return {identity: self.image(identity, connection) for identity in ids}

    def project_and_loader(self, identity):
        return self.project(identity), self.image

    def read_project_images(self, identity, fn):
        with self.connection() as connection:
            project = self.project(identity, connection)
            return fn(project, lambda asset: self.image(asset, connection))

    def list_snapshots(self, identity):
        with self.connection() as connection:
            self.project(identity, connection)
            rows = connection.execute(
                "SELECT data FROM snapshots WHERE project_id=? ORDER BY captured_at DESC, id",
                (identity,),
            ).fetchall()
        return [_load(row["data"])["summary"] for row in rows]

    def snapshot(self, project, identity):
        valid_id(project, "样件ID")
        valid_id(identity, "快照ID")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM snapshots WHERE project_id=? AND id=?", (project, identity)
            ).fetchone()
        if row is None:
            raise InspectionError("找不到此测量快照。", status=404, code="missing-snapshot")
        return _load(row["data"])

    def save_snapshot(self, project, data):
        summary = data["summary"]
        with self.connection(write=True) as connection:
            latest = self.project(project.id, connection)
            check_revision(project.revision, latest.revision)
            count = connection.execute(
                "SELECT count(*) FROM snapshots WHERE project_id=?", (project.id,)
            ).fetchone()[0]
            if count >= MAX_SNAPSHOTS:
                raise InspectionError(f"一个样件最多保留 {MAX_SNAPSHOTS} 份测量快照。", status=409)
            connection.execute(
                "INSERT INTO snapshots(id, project_id, captured_at, data) VALUES (?, ?, ?, ?)",
                (summary["id"], project.id, summary["captured_at"], _dump(data)),
            )
            connection.executemany(
                "INSERT INTO snapshot_assets(snapshot_id, asset) VALUES (?, ?)",
                ((summary["id"], key) for key in self._project_assets(project)),
            )
        return summary

    def remove_snapshot(self, project, identity):
        valid_id(project, "样件ID")
        valid_id(identity, "快照ID")
        with self.connection(write=True) as connection:
            keys = [row[0] for row in connection.execute(
                "SELECT asset FROM snapshot_assets WHERE snapshot_id=?", (identity,)
            )]
            affected = connection.execute(
                "DELETE FROM snapshots WHERE project_id=? AND id=?", (project, identity)
            ).rowcount
            if not affected:
                raise InspectionError("找不到此测量快照。", status=404, code="missing-snapshot")
            self._remove_unused_assets(connection, keys)

    def list_audits(self, identity):
        with self.connection() as connection:
            project = self.project(identity, connection)
            rows = connection.execute(
                "SELECT summary FROM audits WHERE project_id=? ORDER BY recorded_at DESC, id",
                (project.id,),
            )
            summaries = [_load(row["summary"]) for row in rows]
        return {
            "revision": project.revision,
            "items": [{**summary, "revision_status": pending_review(project, {"summary": summary})} for summary in summaries],
        }

    def audit(self, project, identity):
        valid_id(project, "样件ID")
        valid_id(identity, "核验记录ID")
        with self.connection() as connection:
            current = self.project(project, connection)
            row = connection.execute(
                "SELECT data FROM audits WHERE project_id=? AND id=?",
                (project, identity),
            ).fetchone()
        if row is None:
            raise InspectionError("找不到这份核验记录。", status=404, code="missing-audit")
        saved = _load(row["data"])
        return {**saved, "revision_status": pending_review(current, saved)}

    def save_audit(self, project, archive):
        summary = archive["summary"]
        data = _dump(archive)
        if len(data.encode("utf-8")) > AUDIT_BYTES:
            raise InspectionError("核验记录过大，请缩小检查范围。", status=413, code="audit-size")
        with self.connection(write=True) as connection:
            latest = self.project(project.id, connection)
            check_revision(project.revision, latest.revision)
            count = connection.execute("SELECT count(*) FROM audits WHERE project_id=?", (project.id,)).fetchone()[0]
            if count >= AUDIT_LIMIT:
                raise InspectionError(f"一个样件最多保留 {AUDIT_LIMIT} 份核验记录。", status=409, code="audit-limit")
            connection.execute(
                "INSERT INTO audits(id, project_id, recorded_at, summary, data) VALUES (?, ?, ?, ?, ?)",
                (summary["id"], project.id, summary["when"], _dump(summary), data),
            )
        return summary

    def remove_audit(self, project, identity):
        valid_id(project, "样件ID")
        valid_id(identity, "核验记录ID")
        with self.connection(write=True) as connection:
            changed = connection.execute(
                "DELETE FROM audits WHERE project_id=? AND id=?",
                (project, identity),
            ).rowcount
        if not changed:
            raise InspectionError("找不到这份核验记录。", status=404, code="missing-audit")

    @staticmethod
    def _project_assets(project):
        keys = {view.asset for view in project.views}
        if project.reference:
            keys.add(project.reference)
        return keys

    def _sync_project_assets(self, connection, project):
        connection.execute("DELETE FROM project_assets WHERE project_id=?", (project.id,))
        connection.executemany(
            "INSERT INTO project_assets(project_id, asset) VALUES (?, ?)",
            ((project.id, key) for key in self._project_assets(project)),
        )

    @staticmethod
    def _remove_unused_assets(connection, keys):
        connection.executemany(
            "DELETE FROM assets WHERE sha256=? AND NOT EXISTS "
            "(SELECT 1 FROM project_assets WHERE asset=assets.sha256) AND NOT EXISTS "
            "(SELECT 1 FROM snapshot_assets WHERE asset=assets.sha256)",
            ((key,) for key in keys),
        )

    @staticmethod
    def _insert_assets(connection, assets):
        connection.executemany(
            "INSERT OR IGNORE INTO assets(sha256,width,height,pixels,byte_size,stored_at,payload) "
            "VALUES (?,?,?,?,?,?,?)",
            ((meta.sha256, meta.width, meta.height, meta.pixels, meta.byte_size,
              meta.stored_at, payload) for meta, payload in assets),
        )

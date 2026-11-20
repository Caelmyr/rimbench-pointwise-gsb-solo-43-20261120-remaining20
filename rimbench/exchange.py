from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import re
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from .domain import (
    Calibration, Gauge, InspectionError, MAX_GAUGES, MAX_IMAGE_BYTES, MAX_NOTES, MAX_VIEWS,
    Note, Project, Segmentation, View, entries, flag, integer, label, record, valid_id,
)
from .images import ingest
from .store import asset_id


FORMAT = "rimbench.project.v1"
MANIFEST = "rimbench.json"
MAX_ARCHIVE_BYTES = 150*1024*1024
MAX_MANIFEST_BYTES = 512*1024


def build_bundle(project, load):
    images = set(view.asset for view in project.views)
    if project.reference:
        images.add(project.reference)
    return _build(project, images, load)


def _build(project, images, load):
    manifest = _manifest(project, sorted(images))
    stream = BytesIO()
    try:
        with ZipFile(stream, "w", compression=ZIP_STORED, allowZip64=False) as archive:
            archive.writestr(MANIFEST, manifest)
            projected = len(manifest) + 64*1024
            for identity in sorted(images):
                meta, png = load(identity)
                projected += meta.byte_size
                if projected >= MAX_ARCHIVE_BYTES:
                    raise InspectionError("影像总量超过交换包上限 150 MiB。", status=413)
                if sha256(png).hexdigest() != identity:
                    raise InspectionError("工作站存档中的影像摘要与实际内容不符。", code="asset-corrupt")
                archive.writestr(_member(identity), png)
    except OSError as exc:
        raise InspectionError("无法组装交换包。", status=503) from exc
    return stream.getvalue()


def _manifest(project, images):
    data = project.data()
    for field in ("id", "created_at", "updated_at", "revision"):
        del data[field]
    return json.dumps({"format": FORMAT, "settings": data, "images": images}, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _member(identity):
    return f"frames/{identity}.png"


def open_bundle(payload, title_override=None):
    if not 0 < len(payload) <= MAX_ARCHIVE_BYTES:
        raise InspectionError("交换包不能为空，且上限为 150 MiB。", status=413)
    try:
        with ZipFile(BytesIO(payload), "r") as archive:
            by_name = _index(archive)
            raw = archive.read(by_name[MANIFEST])
            envelope = record(json.loads(raw), "交换包目录")
            if envelope.get("format") != FORMAT:
                raise InspectionError("交换包版本无法识别。", code="bundle-version")
            project, keys = _read_settings(envelope.get("settings"), title_override)
            inventory = [asset_id(identity) for identity in entries(envelope.get("images"), "影像目录", MAX_VIEWS+1)]
            if len(inventory) != len(set(inventory)) or set(inventory) != keys:
                raise InspectionError("交换包的项目影像与目录不一致。", code="bundle-assets")
            if {_member(key) for key in inventory} != set(by_name)-{MANIFEST}:
                raise InspectionError("交换包缺少影像或含有未声明的文件。", code="bundle-assets")
            assets = [_read_asset(archive, by_name[_member(key)], key, project) for key in inventory]
            return project, assets
    except InspectionError:
        raise
    except (BadZipFile, OSError, RuntimeError, UnicodeError, ValueError, KeyError) as exc:
        raise InspectionError("交换包格式无效或未完整下载。", code="bundle-invalid") from exc


def _index(archive):
    result = {}
    size = 0
    for info in archive.infolist():
        if info.filename in result or info.is_dir() or len(result) >= MAX_VIEWS+2 or info.flag_bits & 1:
            raise InspectionError("交换包含重复、加密或额外文件。", code="bundle-members")
        size += info.file_size
        if size >= MAX_ARCHIVE_BYTES:
            raise InspectionError("展开的交换包超过 150 MiB。", status=413)
        if info.filename == MANIFEST:
            if info.file_size > MAX_MANIFEST_BYTES:
                raise InspectionError("交换包目录过大。", status=413)
        elif not re.fullmatch(r"frames/[a-f0-9]{64}\.png", info.filename) or info.file_size > MAX_IMAGE_BYTES:
            raise InspectionError("交换包含有不支持的文件条目。", code="bundle-members")
        result[info.filename] = info
    if MANIFEST not in result:
        raise InspectionError("交换包缺少 RimBench 项目目录。", code="bundle-manifest")
    return result


def _read_asset(archive, info, identity, project):
    raw = archive.read(info)
    if sha256(raw).hexdigest() != identity:
        raise InspectionError("交换包影像无法通过摘要检查。", code="asset-corrupt")
    meta, canonical = ingest(raw)
    if (meta.width, meta.height) != (project.width, project.height):
        raise InspectionError("交换包影像尺寸与取景基准不一致。", code="frame-size")
    if sha256(canonical).hexdigest() != identity:
        # Compression levels and encoders may differ between RimBench versions.
        meta = replace(meta, sha256=identity, byte_size=len(raw))
        return meta, raw
    return meta, canonical


def _read_settings(data, title_override):
    source = record(data, "项目设置")
    title = title_override if title_override else source.get("title")
    project = Project.create(
        title, source.get("station", ""), source.get("operator", ""),
        sample=flag(source.get("sample", False), "合成样件状态"),
    )
    width = integer(source.get("width"), "取景宽度", 0, 250000)
    height = integer(source.get("height"), "取景高度", 0, 250000)
    if bool(width) != bool(height) or (width and min(width, height) < 24):
        raise InspectionError("交换包取景尺寸无效。", code="frame-size")
    if width*height > 6_000_000:
        raise InspectionError("交换包取景超过有效画幅。", code="frame-size")
    recipe = Segmentation.from_data(record(source.get("segmentation"), "轮廓配方"))
    calibration = Calibration.from_data(source.get("calibration"), width, height) if width else None
    if width and recipe.inset*2 >= min(width, height)-2:
        raise InspectionError("交换包的保护边距超过取景范围。")
    captures = []
    view_ids = set()
    hashes = set()
    for data in entries(source.get("views"), "角度列表", MAX_VIEWS):
        original = View.from_data(record(data, "角度影像"))
        if original.id in view_ids:
            raise InspectionError("交换包中的角度标识不能重复。", code="bundle-members")
        view_ids.add(original.id)
        key = asset_id(original.asset)
        hashes.add(key)
        captures.append(original)
    gauges = tuple(
        Gauge.from_data(row) for row in entries(source.get("gauges"), "公差量规", MAX_GAUGES)
    )
    if len({gauge.title.casefold() for gauge in gauges}) != len(gauges):
        raise InspectionError("交换包的量规名称不能重复。")
    notes = [_read_note(row, view_ids) for row in entries(source.get("notes"), "检验记录", MAX_NOTES)]
    reference = source.get("reference")
    if reference is not None:
        reference = asset_id(reference)
        hashes.add(reference)
    if hashes and not width:
        raise InspectionError("有影像的交换包必须包含画幅和轴标定。")
    return replace(
        project, width=width, height=height, calibration=calibration,
        segmentation=recipe, reference=reference, views=tuple(captures), gauges=gauges, notes=tuple(notes),
    ), hashes


def _read_note(raw, view_ids):
    data = record(raw, "检验记录")
    note = Note.create(data)
    if note.view and note.view not in view_ids:
        raise InspectionError("检验记录引用了不存在的角度。")
    created_at = label(data.get("created_at"), "记录时间", 48)
    return replace(note, created_at=created_at)

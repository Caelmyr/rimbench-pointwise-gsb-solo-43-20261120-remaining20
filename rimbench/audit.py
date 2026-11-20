from __future__ import annotations

from dataclasses import dataclass

from .clearance import SleeveRequest, sleeve_report
from .contour import section_form
from .domain import InspectionError, check_revision, choice, label, new_id, number, record, utc_now
from .edgecheck import EdgeRequest, edge_report
from .form import FormRequest, form_scan
from .lobes import LobeRequest, analyze_lobes
from .repeats import RepeatRequest, repeat_report


METHOD = "rimbench-inspection-1"
AUDIT_LIMIT = 30
AUDIT_BYTES = 1_500_000


@dataclass(frozen=True)
class RoundRequest:
    z: float
    datum_x: float
    datum_y: float

    @classmethod
    def parse(cls, data):
        data = record(data, "圆拟合")
        z = number(data.get("z", 22), "截面高度", -100000, 100000)
        x = number(data.get("datum_x", 0), "参考圆心 X", -100000, 100000)
        y = number(data.get("datum_y", 0), "参考圆心 Y", -100000, 100000)
        return cls(z, x, y)

    def data(self):
        return {"z": self.z, "datum_x": self.datum_x, "datum_y": self.datum_y}


def single_round(meter, request):
    return {
        **section_form(meter, request.z, request.datum_x, request.datum_y),
        "parameters": request.data(),
        "project_id": meter.project.id,
    }


@dataclass(frozen=True)
class ReportKind:
    name: str
    request: type
    execute: object


REPORTS = {
    "round": ReportKind("截面圆拟合", RoundRequest, single_round),
    "form": ReportKind("轴向形位", FormRequest, form_scan),
    "clearance": ReportKind("套环包络", SleeveRequest, sleeve_report),
    "lobes": ReportKind("周向形状", LobeRequest, analyze_lobes),
    "edges": ReportKind("成像边缘质检", EdgeRequest, edge_report),
    "repeat": ReportKind("投影复核", RepeatRequest, repeat_report),
}


def calculate(meter, kind, data):
    kind = choice(kind, "核验类型", REPORTS)
    handler = REPORTS[kind]
    return handler.execute(meter, handler.request.parse(data))


def report_state(kind, report):
    if kind in ("round",):
        return report["state"]
    if kind in ("form", "clearance"):
        return report["summary"]["state"]
    if kind == "edges":
        if not report["rows"]:
            return "unavailable"
        return "checked" if all(row["state"] == "measured" for row in report["rows"]) else "review"
    if kind == "repeat":
        compared = report["summary"]["compared"]
        if compared == 0:
            return "unavailable"
        return "checked" if compared == report["summary"]["states"].get("within") == len(report["rows"]) else "review"
    measured = report["summary"]["with_phase"]
    return "checked" if measured == len(report["rows"]) else "review" if measured else "unavailable"


def used_assets(kind, report, project):
    if kind == "repeat":
        identities = {report["first"]["id"], report["second"]["id"]}
    elif kind == "edges":
        identities = {row["view_id"] for row in report["rows"]}
    else:
        identities = {view.id for view in project.views if view.included}
    return [
        {"view": view.id, "image_hash": view.asset, "angle": view.angle, "included": view.included}
        for view in project.views if view.id in identities
    ]


def record_audit(meter, body):
    document = record(body, "核验记录")
    project = meter.project
    check_revision(document.get("revision"), project.revision)
    kind = choice(document.get("kind"), "核验类型", tuple(REPORTS))
    results = calculate(meter, kind, document.get("parameters"))
    title = label(document.get("title"), "核验记录名称")
    summary = {
        "id": new_id(),
        "kind": kind,
        "kind_label": REPORTS[kind].name,
        "title": title,
        "when": utc_now(),
        "state": report_state(kind, results),
        "revision": project.revision,
        "project_title": project.title,
        "project_id": project.id,
        "method": METHOD,
    }
    setup = {
        "calibration": project.calibration.data() if project.calibration else None,
        "recipe": project.segmentation.data(),
        "reference_sha256": project.reference,
        "captures": used_assets(kind, results, project),
    }
    return {"summary": summary, "setup": setup, "result": results}


def frozen_revision(arguments, revision):
    value = arguments.get("revision")
    if value is not None:
        check_revision(value, revision)


def pending_review(current, audit):
    saved = audit["summary"]
    if saved["method"] != METHOD:
        return {"state": "legacy-method", "recorded": saved["revision"], "current": current.revision}
    if saved["revision"] != current.revision:
        return {"state": "earlier-revision", "recorded": saved["revision"], "current": current.revision}
    return {"state": "current-revision", "recorded": saved["revision"], "current": current.revision}

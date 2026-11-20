from __future__ import annotations

from .domain import InspectionError, check_revision, degrees, label, new_id, number, record, utc_now
from .geometry import circular_coverage
from .profile import ProfileRequest, profile
from .quality import gauge_results, review_gate


METHOD = "silhouette-hull-1"


def capture(meter, document):
    document = record(document, "测量快照")
    project = meter.project
    check_revision(document.get("revision"), project.revision)
    title = label(document.get("title"), "快照名称")
    z = number(document.get("z"), "截面高度", -100000, 100000)
    direction = degrees(document.get("direction", 0))
    outline = meter.section(z, direction)
    if outline["state"] != "measured":
        raise InspectionError("需先取得有效截面才可归档快照。", code="no-valid-section")
    curve_request = document.get("profile")
    curve = profile(meter, ProfileRequest.parse(curve_request)) if curve_request is not None else None
    gauges = gauge_results(meter)
    verification = review_gate(project, gauges)
    summary = {
        "id": new_id(), "title": title, "captured_at": utc_now(),
        "revision": project.revision, "z": z, "direction": direction,
        "project_id": project.id, "project_title": project.title,
        "curve_metric": curve["parameters"]["metric"] if curve else None,
        "method": METHOD, "review": verification,
        "views": outline["usable_views"],
    }
    return {
        "summary": summary, "project": project.data(),
        "section": outline, "profile": curve, "gauges": gauges,
        "acquisition": circular_coverage(project.views),
    }


def public_snapshot(raw):
    archived = raw["project"]
    section = raw["section"]
    return {
        "summary": raw["summary"],
        "geometry": {"width": archived["width"], "height": archived["height"],
                     "calibration": archived["calibration"], "segmentation": archived["segmentation"]},
        "section": section,
        "profile": raw["profile"],
        "gauges": raw["gauges"],
        "acquisition": raw["acquisition"],
    }

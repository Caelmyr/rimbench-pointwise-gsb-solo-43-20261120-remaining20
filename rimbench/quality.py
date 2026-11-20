from __future__ import annotations

from collections import Counter, defaultdict

from .domain import Gauge, InspectionError, MAX_GAUGES, entries, record
from .geometry import METRIC_LABELS, METRIC_UNITS, circular_coverage
from .meter import healthy_range, shape_section


def replace_gauges(project, data):
    rows = entries(data, "公差量规", MAX_GAUGES)
    parsed = []
    names = set()
    accepted_ids = {old.id for old in project.gauges}
    reused = set()
    for row in rows:
        row = record(row, "公差量规")
        identity = row.get("id")
        if identity is not None:
            if not isinstance(identity, str) or identity not in accepted_ids or identity in reused:
                raise InspectionError("量规标识已失效或重复。")
            reused.add(identity)
        gauge = Gauge.from_data(row, identity)
        folded = gauge.title.casefold()
        if folded in names:
            raise InspectionError("量规名称不能重复。")
        names.add(folded)
        parsed.append(gauge)
    return project.changed(gauges=tuple(parsed))


def gauge_results(meter):
    project = meter.project
    if not project.gauges:
        return {"revision": project.revision, "rows": [], "summary": {}}
    if not project.calibration:
        states = [_missing(gauge, "样件还没有影像标定。") for gauge in project.gauges]
    else:
        levels = list(dict.fromkeys(gauge.z for gauge in project.gauges))
        observations = dict(zip(levels, meter.observe_grid(levels)))
        sections = {}
        states = []
        for gauge in project.gauges:
            key = (gauge.z, gauge.direction if gauge.metric == "width" else 0)
            if key not in sections:
                sections[key] = shape_section(
                    project, gauge.z, key[1], observations[gauge.z], detailed=False,
                )
            states.append(judge(gauge, sections[key]))
    return {
        "revision": project.revision,
        "rows": states,
        "summary": dict(Counter(row["decision"] for row in states)),
    }


def _missing(gauge, detail, section=None):
    return {
        "gauge": gauge.data(), "value": None, "interval": None,
        "decision": "unavailable", "detail": detail,
        "usable_views": section["usable_views"] if section else 0,
        "unit": METRIC_UNITS[gauge.metric], "metric_label": METRIC_LABELS[gauge.metric],
    }


def judge(gauge, section):
    if section["metrics"] is None:
        return _missing(gauge, section["detail"], section)
    value = section["metrics"][gauge.metric]
    bounds = healthy_range(section, gauge.metric)
    if bounds is None:
        decision = "review"
        detail = "这个量值目前没有误差区间。"
    elif bounds[1] < gauge.low or bounds[0] > gauge.high:
        decision = "fail"
        detail = "整个测量区间均在公差之外。"
    elif gauge.low <= bounds[0] and bounds[1] <= gauge.high:
        decision = "pass"
        detail = "整个测量区间落在公差中。"
    else:
        decision = "review"
        detail = "测量区间与公差边界重合，建议复测。"
    return {
        "gauge": gauge.data(), "value": value, "interval": bounds,
        "decision": decision, "detail": detail,
        "usable_views": section["usable_views"],
        "unit": METRIC_UNITS[gauge.metric], "metric_label": METRIC_LABELS[gauge.metric],
    }


def acquisition_report(meter, z):
    observations = meter.observations_at(z)
    grouped = defaultdict(list)
    for row in observations:
        grouped[row["state"]].append(row["view_id"])
    result = circular_coverage(meter.project.views)
    state_groups = {state: identities for state, identities in sorted(grouped.items())}
    result.update({"z": z, "states": state_groups, "observations": observations})
    return result


def review_gate(project, result):
    available = result["summary"]
    holds = [note.id for note in project.notes if note.kind == "hold"]
    if holds:
        return {"state": "on-hold", "detail": "存在未移除的复查挂起记录。", "notes": holds}
    if not available:
        return {"state": "unset", "detail": "暂无公差量规。", "notes": []}
    if available.get("fail"):
        return {"state": "out-of-spec", "detail": "至少一项测量确定超出公差。", "notes": []}
    if available.get("review") or available.get("unavailable"):
        return {"state": "review", "detail": "存在需要复测或尚未取得的测量。", "notes": []}
    return {"state": "ready", "detail": "所有配置量规的区间均落入公差。", "notes": []}


def station_history(projects):
    by_station = defaultdict(list)
    for sample in projects:
        by_station[sample["station"]].append(sample)
    rows = []
    for station, samples in by_station.items():
        if not station:
            station = "未分配测站"
        rows.append({
            "station": station,
            "projects": len(samples),
            "with_images": sum(bool(sample["view_count"]) for sample in samples),
            "sample_ids": [sample["id"] for sample in samples],
            "last_at": max(sample["updated_at"] for sample in samples),
        })
    return sorted(rows, key=lambda row: row["last_at"], reverse=True)

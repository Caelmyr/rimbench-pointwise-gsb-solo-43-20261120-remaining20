from __future__ import annotations

from collections import Counter, defaultdict

from .domain import Gauge, InspectionError, MAX_GAUGES, entries, record
from .geometry import METRIC_LABELS, METRIC_UNITS, circular_coverage, projected_width, width_sweep
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
            sweep = gauge.metric == "width" and gauge.play
            if key not in sections or (sweep and "polygons" not in sections[key]):
                sections[key] = shape_section(
                    project, gauge.z, key[1], observations[gauge.z], detailed=bool(sweep),
                )
            states.append(judge(gauge, sections[key], project.calibration))
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


def judge(gauge, section, calibration=None):
    if section["metrics"] is None:
        return _missing(gauge, section["detail"], section)
    if gauge.metric == "width" and gauge.play and section.get("polygons", {}).get("outer"):
        return _judge_width_sweep(gauge, section, calibration)
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


def _judge_width_sweep(gauge, section, calibration):
    # Every direction in [direction-play, direction+play] must keep its width
    # interval inside the tolerance. The extrema over the arc are exact, so a
    # widest point in the middle of the sector is not missed.
    relative = calibration.ruler_uncertainty/calibration.scale_mm
    polygons = section["polygons"]
    center, play = gauge.direction, gauge.play
    outer = width_sweep(polygons["outer"], center, play)
    inner = width_sweep(polygons["inner"], center, play)
    shrink, grow = 1.0-relative, 1.0+relative

    def interval_at(theta):
        lower = projected_width(polygons["inner"], theta)*shrink if polygons["inner"] else 0.0
        return [max(0.0, lower), projected_width(polygons["outer"], theta)*grow]

    upper_max_dir, upper_max = outer["max"][0], outer["max"][1]*grow
    upper_min_dir, upper_min = outer["min"][0], outer["min"][1]*grow
    if inner:
        lower_min_dir, lower_min = inner["min"][0], max(0.0, inner["min"][1]*shrink)
        lower_max_dir, lower_max = inner["max"][0], max(0.0, inner["max"][1]*shrink)
    else:
        lower_min_dir, lower_min = outer["min"][0], 0.0
        lower_max_dir, lower_max = outer["max"][0], 0.0
    widest = {
        "direction": upper_max_dir,
        "value": projected_width(polygons["nominal"], upper_max_dir),
        "interval": interval_at(upper_max_dir),
    }
    narrowest = {
        "direction": lower_min_dir,
        "value": projected_width(polygons["nominal"], lower_min_dir),
        "interval": interval_at(lower_min_dir),
    }
    if lower_max > gauge.high:
        decision = "fail"
        limiting = (lower_max_dir, lower_max)
        detail = (f"方向 {limiting[0]:.2f}° 的区间下限 {limiting[1]:.4f} mm 已超出上限 "
                  f"{gauge.high:g} mm，计入测量误差仍超差。")
    elif upper_min < gauge.low:
        decision = "fail"
        limiting = (upper_min_dir, upper_min)
        detail = (f"方向 {limiting[0]:.2f}° 的区间上限 {limiting[1]:.4f} mm 已低于下限 "
                  f"{gauge.low:g} mm，计入测量误差仍超差。")
    elif lower_min >= gauge.low and upper_max <= gauge.high:
        decision = "pass"
        if gauge.high-upper_max <= lower_min-gauge.low:
            limiting = (upper_max_dir, upper_max)
        else:
            limiting = (lower_min_dir, lower_min)
        detail = f"±{play:g}° 偏转范围内各方向的宽度区间均在公差内。"
    else:
        decision = "review"
        if upper_max > gauge.high:
            limiting = (upper_max_dir, upper_max)
            detail = (f"方向 {limiting[0]:.2f}° 的区间上限 {limiting[1]:.4f} mm 越出上限 "
                      f"{gauge.high:g} mm，但未能确认超差，建议复测。")
        else:
            limiting = (lower_min_dir, lower_min)
            detail = (f"方向 {limiting[0]:.2f}° 的区间下限 {limiting[1]:.4f} mm 越出下限 "
                      f"{gauge.low:g} mm，但未能确认超差，建议复测。")
    return {
        "gauge": gauge.data(), "value": widest["value"],
        "interval": [lower_min, upper_max],
        "decision": decision, "detail": detail,
        "usable_views": section["usable_views"],
        "unit": METRIC_UNITS[gauge.metric], "metric_label": METRIC_LABELS[gauge.metric],
        "sweep": {
            "set_direction": center, "play": play,
            "range": [(center-play) % 360.0, (center+play) % 360.0],
            "max_width": widest, "min_width": narrowest,
            "limiting_direction": limiting[0], "limiting_value": limiting[1],
        },
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

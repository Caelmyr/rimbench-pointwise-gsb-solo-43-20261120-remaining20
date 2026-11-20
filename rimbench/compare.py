from __future__ import annotations

from bisect import bisect_left
import math

from .domain import InspectionError
from .geometry import METRIC_LABELS, METRIC_UNITS


def compare_profiles(anchor, current, z_shift=0):
    old_params, new_params = anchor["parameters"], current["parameters"]
    kind = new_params["metric"]
    if old_params["metric"] != kind:
        raise InspectionError("请选择相同量值的测量快照。", code="different-metric")
    if kind == "width":
        shift = abs((new_params["direction"]-old_params["direction"]) % 180)
        if min(shift, 180-shift) > 1e-5:
            raise InspectionError("指定方向宽度需要在相同观察方向下比较。", code="different-direction")
    old = anchor["samples"]
    old_heights = [sample["z"] for sample in old]
    if not old_heights or not current["samples"]:
        raise InspectionError("两个曲线都需要有轴向采样点。")
    rows = []
    for live in current["samples"]:
        source_z = live["z"]-z_shift
        baseline, source = _interpolate(old, old_heights, source_z)
        delta = None
        span = None
        if live["value"] is None:
            state = "current-gap"
        elif source != "supported":
            state = source
        else:
            delta = live["value"]-baseline["value"]
            span = _subtract(live.get("interval"), baseline.get("interval"))
            state = "comparable"
        rows.append({
            "z": live["z"], "reference_z": source_z, "reference_span": baseline["source_span"] if baseline else None,
            "current": live["value"], "reference": baseline["value"] if baseline else None,
            "delta": delta, "delta_interval": span, "state": state,
        })
    aligned = [row for row in rows if row["delta"] is not None]
    largest = max(aligned, key=lambda row: abs(row["delta"])) if aligned else None
    return {
        "metric": kind, "metric_label": METRIC_LABELS[kind], "unit": METRIC_UNITS[kind],
        "z_shift": z_shift,
        "rows": rows,
        "sample_overlap": [row["z"] for row in aligned],
        "summary": {
            "compared": len(aligned), "total": len(rows),
            "mean_delta": sum(row["delta"] for row in aligned)/len(aligned) if aligned else None,
            "max_change": {"z": largest["z"], "delta": largest["delta"]} if largest else None,
            "confirmed_changes": sum(
                1 for row in aligned if row["delta_interval"] and
                (row["delta_interval"][0] > 0 or row["delta_interval"][1] < 0)
            ),
        },
    }


def _subtract(live, earlier):
    if live is None or earlier is None:
        return None
    return [live[0]-earlier[1], live[1]-earlier[0]]


def _interpolate(rows, heights, target):
    # A measurement is interpolable only between adjacent, measured samples.
    # A skipped reference level is not equivalent to measurement at its neighbours.
    point = bisect_left(heights, target)
    for candidate in (point, point-1):
        if 0 <= candidate < len(heights) and math.isclose(heights[candidate], target, rel_tol=0, abs_tol=1e-6):
            value = rows[candidate]
            if value["value"] is None:
                return None, "reference-gap"
            return {**value, "source_span": [value["z"], value["z"]]}, "supported"
    if point <= 0 or point == len(rows):
        return None, "outside-reference"
    first, second = rows[point-1], rows[point]
    if first["value"] is None or second["value"] is None:
        return None, "reference-gap"
    share = (target-first["z"])/(second["z"]-first["z"])
    a, b = first.get("interval"), second.get("interval")
    interval = [low+(high-low)*share for low, high in zip(a, b)] if a and b else None
    return {
        "value": first["value"]+(second["value"]-first["value"])*share,
        "interval": interval, "source_span": [first["z"], second["z"]],
    }, "supported"


def compare_sections(anchor, current):
    if abs(anchor["z"]-current["z"]) > 1e-6:
        raise InspectionError("请选择同一高度的截面重测。", code="different-z")
    if not anchor.get("metrics") or not current.get("metrics"):
        return {"state": "unavailable", "metrics": {}, "centroid_shift": None}
    table = {}
    for kind, value in current["metrics"].items():
        previous = anchor["metrics"][kind]
        a = (anchor.get("intervals") or {}).get(kind)
        b = (current.get("intervals") or {}).get(kind)
        table[kind] = {
            "before": previous, "after": value, "delta": value-previous,
            "delta_interval": _subtract(b, a), "unit": METRIC_UNITS[kind],
        }
    offset = None
    if anchor.get("centroid") and current.get("centroid"):
        offsets = [now-base for base, now in zip(anchor["centroid"], current["centroid"])]
        offset = {"dx": offsets[0], "dy": offsets[1], "distance": math.hypot(*offsets)}
    return {"state": "comparable", "metrics": table, "centroid_shift": offset}

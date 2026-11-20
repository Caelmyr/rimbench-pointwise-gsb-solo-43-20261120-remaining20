from __future__ import annotations

from dataclasses import dataclass
import math

from .calibration import z_positions
from .domain import InspectionError, choice, degrees, integer, number, record
from .geometry import METRIC_LABELS, METRIC_UNITS
from .meter import healthy_range, shape_section
from .steps import analyze_steps


PROFILE_METRICS = tuple(METRIC_LABELS)
MAX_PROFILE_OBSERVATIONS = 4_000


@dataclass(frozen=True)
class ProfileRequest:
    lower: float
    upper: float
    count: int
    direction: float
    metric: str

    @classmethod
    def parse(cls, data):
        data = record(data, "轴向曲线")
        lower = number(data.get("lower"), "起始高度", -100000, 100000)
        upper = number(data.get("upper"), "结束高度", -100000, 100000)
        count = integer(data.get("count", 31), "切片数", 2, 121)
        direction = degrees(data.get("direction", 0))
        metric = choice(data.get("metric", "width"), "曲线量值", PROFILE_METRICS)
        return cls(lower, upper, count, direction, metric)

    def data(self):
        return {
            "lower": self.lower, "upper": self.upper,
            "count": self.count, "direction": self.direction, "metric": self.metric,
        }


def profile(meter, request):
    positions = z_positions(request.lower, request.upper, request.count)
    active = sum(view.included for view in meter.project.views)
    if active * request.count > MAX_PROFILE_OBSERVATIONS:
        raise InspectionError(
            "曲线抽样数与角度数乘积过大，请减少曲线采样点。",
            code="profile-observation-limit",
        )
    results = meter.observe_grid(positions)
    samples = [
        shape_section(meter.project, z, request.direction, observations, detailed=False)
        for z, observations in zip(positions, results)
    ]
    quantities = summarize(samples, request.metric)
    simplified = [simplify_section(item, request.metric) for item in samples]
    steps = analyze_steps(
        simplified, metric=request.metric,
        units=METRIC_UNITS[request.metric], direction=request.direction,
    )
    return {
        "revision": meter.project.revision,
        "project_id": meter.project.id,
        "parameters": request.data(),
        "units": METRIC_UNITS[request.metric],
        "metric_label": METRIC_LABELS[request.metric],
        "samples": simplified,
        "summary": quantities,
        "steps": steps,
    }


def simplify_section(section, metric):
    observed = section["metrics"] or {}
    return {
        "z": section["z"], "value": observed.get(metric),
        "interval": healthy_range(section, metric),
        "state": section["state"], "detail": section["detail"],
        "usable_views": section["usable_views"],
        "max_angle_gap": section["normal_coverage"]["gap"],
    }


def summarize(samples, metric):
    present = [(sample["z"], sample["metrics"][metric]) for sample in samples if sample["metrics"]]
    open_groups = []
    start = None
    previous = None
    states = {}
    for sample in samples:
        state = sample["state"]
        states[state] = states.get(state, 0)+1
        if state != "measured":
            if start is None:
                start = sample["z"]
            previous = sample["z"]
        elif start is not None:
            open_groups.append([start, previous])
            start = None
    if start is not None:
        open_groups.append([start, previous])
    result = {
        "measured": len(present), "total": len(samples), "missing_ranges": open_groups,
        "states": states, "minimum": None, "maximum": None, "variation": None,
        "length_between_valid": None,
    }
    if present:
        low = min(present, key=lambda point: point[1])
        high = max(present, key=lambda point: point[1])
        result.update(
            minimum={"z": low[0], "value": low[1]},
            maximum={"z": high[0], "value": high[1]},
            variation=high[1]-low[1],
            length_between_valid=present[-1][0]-present[0][0],
        )
    return result


def suggested_range(meter):
    axis = meter.axis()
    candidates = []
    for view in meter.project.views:
        if not view.included:
            continue
        bounds = meter.view_mask(view).summary["bounds"]
        if not bounds:
            continue
        xa, ya, xb, yb = bounds
        heights = [
            axis.from_image(x, y, view)[1]
            for x, y in ((xa, ya), (xa, yb), (xb, ya), (xb, yb))
        ]
        candidates.append((min(heights), max(heights)))
    if not candidates:
        return {"lower": None, "upper": None, "z": None, "spans": 0}
    # Use the middle observed endpoints so one severely cropped view does not
    # decide the initial range for the remaining projections.
    lower = _median([value[0] for value in candidates])
    upper = _median([value[1] for value in candidates])
    trim = min(1/axis.scale, max(0, (upper-lower)/8))
    return {
        "lower": round(lower+trim, 4), "upper": round(upper-trim, 4),
        "z": round((lower+upper)/2, 4), "spans": len(candidates),
    }


def _median(sequence):
    ordered = sorted(sequence)
    middle = len(ordered)//2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle-1]+ordered[middle])*0.5

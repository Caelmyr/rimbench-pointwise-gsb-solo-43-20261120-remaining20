from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

import numpy as np

from .calibration import z_positions
from .domain import InspectionError, choice, integer, number, record, valid_id
from .setup import lookup_view
from .silhouette import observation


MAX_REPEAT_HEIGHTS = 91
PAIR_MODES = ("repeat", "reverse")


@dataclass(frozen=True)
class RepeatRequest:
    first: str
    second: str
    mode: str
    lower: float
    upper: float
    count: int
    angle_tolerance: float
    alert_mm: float

    @classmethod
    def parse(cls, data):
        data = record(data, "投影复核")
        first = valid_id(data.get("first"), "第一张影像")
        second = valid_id(data.get("second"), "第二张影像")
        if first == second:
            raise InspectionError("请选择两张不同的采集记录。", code="same-view")
        mode = choice(data.get("mode", "repeat"), "采集核验类型", PAIR_MODES)
        lower = number(data.get("lower"), "起始高度", -100000, 100000)
        upper = number(data.get("upper"), "结束高度", -100000, 100000)
        count = integer(data.get("count", 25), "沿轴采样数", 3, MAX_REPEAT_HEIGHTS)
        angle_tolerance = number(data.get("angle_tolerance", 0.3), "角度允许差", 0.005, 4)
        alert = number(data.get("alert_mm", 0.3), "端点复核阈值", 0.001, 1000)
        return cls(first, second, mode, lower, upper, count, angle_tolerance, alert)

    def data(self):
        return {
            "first": self.first,
            "second": self.second,
            "mode": self.mode,
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "angle_tolerance": self.angle_tolerance,
            "alert_mm": self.alert_mm,
        }


def shortest_angle(original, check):
    return (check-original+180) % 360 - 180


def angle_difference(first, second, mode):
    expected = first+180 if mode == "reverse" else first
    return shortest_angle(expected, second)


def available_pairs(project):
    views = sorted((view for view in project.views if view.included), key=lambda view: (view.angle, view.title, view.id))
    matches = {mode: [] for mode in PAIR_MODES}
    for index, first in enumerate(views):
        for second in views[index+1:]:
            for mode in PAIR_MODES:
                delta = angle_difference(first.angle, second.angle, mode)
                if abs(delta) <= 4:
                    matches[mode].append({
                        "first": first.id,
                        "second": second.id,
                        "delta": delta,
                        "angles": [first.angle, second.angle],
                        "titles": [first.title, second.title],
                        "same_asset": first.asset == second.asset,
                    })
    for entries in matches.values():
        entries.sort(key=lambda row: (abs(row["delta"]), row["same_asset"], row["angles"]))
    return {"revision": project.revision, "pairs": matches}


@dataclass(frozen=True)
class ProjectionSpan:
    left: float
    right: float
    ambiguity: float

    @classmethod
    def from_observation(cls, observed, *, reflected=False):
        if observed["state"] != "usable":
            return None
        left, right = observed["lower"], observed["upper"]
        return cls(-right, -left, observed["edge_error_mm"]) if reflected else cls(
            left, right, observed["edge_error_mm"],
        )

    @property
    def span(self):
        return self.right-self.left

    @property
    def center(self):
        return (self.right+self.left)*0.5

    def data(self):
        return {"left": self.left, "right": self.right, "width": self.span, "center": self.center}


def compare_level(first, second, z, reverse, limit):
    a = ProjectionSpan.from_observation(first)
    b = ProjectionSpan.from_observation(second, reflected=reverse)
    evidence = {
        "z": z,
        "first_state": first["state"],
        "second_state": second["state"],
        "first_reason": first["reason"],
        "second_reason": second["reason"],
        "first": a.data() if a else None,
        "second": b.data() if b else None,
        "changes": None,
    }
    if not a or not b:
        state = "both-missing" if not a and not b else "first-missing" if not a else "second-missing"
        return {**evidence, "state": state}
    left = b.left-a.left
    right = b.right-a.right
    uncertainty = a.ambiguity+b.ambiguity
    bound = max(abs(left), abs(right))
    evidence.update(
        changes={
            "left": left,
            "right": right,
            "width": b.span-a.span,
            "center": b.center-a.center,
            "edge_error": uncertainty,
            "maximum": bound,
        },
        state="exceeds" if bound-uncertainty > limit else "within" if bound+uncertainty <= limit else "overlaps",
    )
    return evidence


def profile_groups(rows, key):
    sections = []
    start = None
    for row in rows:
        same = row["state"] == key
        if same and start is None:
            start = row["z"]
        if same:
            last = row["z"]
        elif start is not None:
            sections.append([start, last])
            start = None
    if start is not None:
        sections.append([start, last])
    return sections


def summary(rows):
    valid = [row for row in rows if row["changes"] is not None]
    counters = dict(Counter(row["state"] for row in rows))
    combined = {
        "total": len(rows),
        "compared": len(valid),
        "states": counters,
        "exceeds_at": profile_groups(rows, "exceeds"),
        "first_gap": profile_groups(rows, "first-missing"),
        "second_gap": profile_groups(rows, "second-missing"),
        "shared_gap": profile_groups(rows, "both-missing"),
        "maximum_edge_delta": None,
        "mean_width_delta": None,
        "rms_center_delta": None,
        "uniform_width_bias": None,
    }
    if valid:
        largest = max(valid, key=lambda row: row["changes"]["maximum"])
        widths = np.asarray([row["changes"]["width"] for row in valid])
        centers = np.asarray([row["changes"]["center"] for row in valid])
        combined.update(
            maximum_edge_delta={"z": largest["z"], "value": largest["changes"]["maximum"]},
            mean_width_delta=float(np.mean(widths)),
            rms_center_delta=math.sqrt(float(np.mean(centers**2))),
            uniform_width_bias=(
                float(np.median(widths)) if len(valid) >= 3 and
                (np.all(widths > 0) or np.all(widths < 0)) else None
            ),
        )
    return combined


def track_captures(meter, frame, view, heights):
    recipe = meter.project.segmentation
    silhouette = meter.view_mask(view)
    return [observation(silhouette, frame, recipe, view, z) for z in heights]


def repeat_report(meter, request):
    first = lookup_view(meter.project, request.first)
    second = lookup_view(meter.project, request.second)
    if not first.included or not second.included:
        raise InspectionError("请启用参与投影复核的两张影像。", code="repeat-excluded")
    if first.asset == second.asset:
        raise InspectionError("这两条记录引用同一原图，不能作为独立采集复核。", code="repeat-same-image")
    delta = angle_difference(first.angle, second.angle, request.mode)
    if abs(delta) > request.angle_tolerance:
        raise InspectionError(
            "两张影像的角度差超出所选复核方式允许的范围。",
            code="repeat-angle",
            details={"angle_delta": delta, "allowed": request.angle_tolerance},
        )
    heights = z_positions(request.lower, request.upper, request.count, maximum=MAX_REPEAT_HEIGHTS)
    frame = meter.axis()
    readings = [track_captures(meter, frame, view, heights) for view in (first, second)]
    rows = [
        compare_level(a, b, z, request.mode == "reverse", request.alert_mm)
        for z, a, b in zip(heights, *readings)
    ]
    return {
        "project_id": meter.project.id,
        "revision": meter.project.revision,
        "parameters": request.data(),
        "first": first.data(),
        "second": second.data(),
        "angle_delta_deg": delta,
        "comparison_frame": "first-physical-aspect",
        "summary": summary(rows),
        "rows": rows,
    }

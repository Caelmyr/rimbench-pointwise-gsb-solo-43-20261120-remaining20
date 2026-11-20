from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from .calibration import z_positions
from .domain import InspectionError, integer, number, record
from .meter import shape_section


MAX_CLEARANCE_SECTIONS = 81
MAX_CLEARANCE_OBSERVATIONS = 2800
FINITE_TOLERANCE = 1e-7


@dataclass(frozen=True)
class SleeveRequest:
    lower: float
    upper: float
    count: int
    center_x: float
    center_y: float
    bore: float
    minimum_gap: float

    @classmethod
    def parse(cls, data):
        data = record(data, "套环包络")
        lower = number(data.get("lower"), "起始高度", -100000, 100000)
        upper = number(data.get("upper"), "结束高度", -100000, 100000)
        count = integer(data.get("count", 21), "抽样截面数", 2, MAX_CLEARANCE_SECTIONS)
        x = number(data.get("center_x", 0), "套环圆心 X", -100000, 100000)
        y = number(data.get("center_y", 0), "套环圆心 Y", -100000, 100000)
        bore = number(data.get("bore"), "套环内径", 0.01, 100000)
        gap = number(data.get("minimum_gap", 0), "要求最小径向余量", 0, bore/2)
        return cls(lower, upper, count, x, y, bore, gap)

    def data(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "bore": self.bore,
            "minimum_gap": self.minimum_gap,
        }

    def center(self):
        return np.array([self.center_x, self.center_y])


def farthest_vertex(points, center, factor=1):
    if not points:
        return None
    coordinates = np.asarray(points, dtype=float)*factor
    offsets = coordinates-center
    radii = np.linalg.norm(offsets, axis=1)
    index = int(np.argmax(radii))
    vector = offsets[index]
    norm = float(radii[index])
    direction = float(np.degrees(np.arctan2(vector[1], vector[0])) % 360) if norm else None
    return {"radius": norm, "point": coordinates[index].tolist(), "direction": direction}


def _nearest_scale(point, center, low, high):
    numerator = float(point@center)
    denominator = float(point@point)
    factor = np.clip(numerator/denominator, low, high) if denominator else low
    return float(np.linalg.norm(point*factor-center))


def radius_bounds(polygons, center, scale_error):
    upper = None
    lower = None
    if polygons["outer"]:
        outer = [
            farthest_vertex(polygons["outer"], center, 1-scale_error),
            farthest_vertex(polygons["outer"], center, 1+scale_error),
        ]
        upper = max(outer, key=lambda row: row["radius"])
    if polygons["inner"]:
        inner = np.asarray(polygons["inner"], dtype=float)
        lower = max(_nearest_scale(point, center, 1-scale_error, 1+scale_error) for point in inner)
    return lower, upper


def evaluate_section(section, request, scale_error):
    center = request.center()
    radius = request.bore*0.5
    contour = section["polygons"]
    nominal = farthest_vertex(contour["nominal"], center)
    inner_radius, outer = radius_bounds(contour, center, scale_error)
    low_gap = radius-outer["radius"] if outer else None
    high_gap = radius-inner_radius if inner_radius is not None else None
    nominal_gap = radius-nominal["radius"] if nominal else None
    required = request.minimum_gap
    state = "no-measurement"
    if section["state"] == "measured":
        if low_gap is not None and low_gap >= required-FINITE_TOLERANCE:
            state = "fits"
        elif high_gap is not None and high_gap < required-FINITE_TOLERANCE:
            state = "interference" if high_gap < -FINITE_TOLERANCE else "insufficient-gap"
        else:
            state = "uncertain-gap"
    return {
        "z": section["z"],
        "state": state,
        "measurement": section["state"],
        "usable_views": section["usable_views"],
        "normal_gap_deg": section["normal_coverage"]["gap"],
        "nominal_gap": nominal_gap,
        "gap_lower": low_gap,
        "gap_upper": high_gap,
        "required_gap": required,
        "limiting_nominal": nominal,
        "limiting_outer": outer,
        "nominal_outline": contour["nominal"],
        "outer_outline": contour["outer"],
    }


def limiting_point(row, key):
    return {
        "z": row["z"],
        "gap": row[key],
        "state": row["state"],
        "outline": row["nominal_outline"],
        "outer_outline": row["outer_outline"],
        "point": row["limiting_nominal"] if key == "nominal_gap" else row["limiting_outer"],
    }


def minimum_gap(rows, key):
    eligible = [row for row in rows if row[key] is not None and row["measurement"] == "measured"]
    if not eligible:
        return None
    row = min(eligible, key=lambda item: item[key])
    return limiting_point(row, key)


def nonpassing_spans(rows):
    ranges = []
    for row in rows:
        if row["state"] == "fits":
            continue
        if ranges and ranges[-1]["last"] == row["index"]-1 and ranges[-1]["state"] == row["state"]:
            ranges[-1].update(last=row["index"], end_z=row["z"])
        else:
            ranges.append({
                "last": row["index"],
                "start_z": row["z"],
                "end_z": row["z"],
                "state": row["state"],
            })
    return [{"start_z": row["start_z"], "end_z": row["end_z"], "state": row["state"]} for row in ranges]


def clearance_summary(rows):
    states = dict(Counter(row["state"] for row in rows))
    if states.get("interference"):
        outcome = "interference"
    elif states.get("insufficient-gap"):
        outcome = "insufficient-gap"
    elif states.get("no-measurement") == len(rows):
        outcome = "no-measurement"
    elif states.get("fits") == len(rows):
        outcome = "fits"
    else:
        outcome = "review"
    return {
        "state": outcome,
        "count": len(rows),
        "states": states,
        "worst_nominal": minimum_gap(rows, "nominal_gap"),
        "worst_outer": minimum_gap(rows, "gap_lower"),
        "review_spans": nonpassing_spans(rows),
    }


def sleeve_report(meter, request):
    heights = z_positions(request.lower, request.upper, request.count, maximum=MAX_CLEARANCE_SECTIONS)
    active = sum(view.included for view in meter.project.views)
    if active*len(heights) > MAX_CLEARANCE_OBSERVATIONS:
        raise InspectionError("抽样截面数乘以参与影像数达到上限。", code="sleeve-observation-limit")
    observations = meter.observe_grid(heights)
    calibration = meter.project.calibration
    scale_error = calibration.ruler_uncertainty/calibration.scale_mm
    reports = []
    for index, (height, evidence) in enumerate(zip(heights, observations)):
        section = shape_section(meter.project, height, 0, evidence, detailed=True)
        row = evaluate_section(section, request, scale_error)
        reports.append({**row, "index": index})
    lean_rows = [
        {key: value for key, value in row.items() if key not in ("nominal_outline", "outer_outline", "index")}
        for row in reports
    ]
    return {
        "project_id": meter.project.id,
        "revision": meter.project.revision,
        "parameters": request.data(),
        "relative_scale_error": scale_error,
        "axial_step_mm": (request.upper-request.lower)/(request.count-1),
        "summary": clearance_summary(reports),
        "rows": lean_rows,
    }

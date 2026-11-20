from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .calibration import z_positions
from .contour import circle_change, measure_section
from .domain import InspectionError, choice, integer, number, record
from .meter import shape_section


DATUMS = ("spindle", "selected")
MAX_FORM_POSITIONS = 49
MAX_FORM_OBSERVATIONS = 2500
MIN_DATUM_POSITIONS = 3


@dataclass(frozen=True)
class FormRequest:
    lower: float
    upper: float
    count: int
    datum: str
    datum_lower: float | None
    datum_upper: float | None
    datum_count: int | None

    @classmethod
    def parse(cls, data):
        data = record(data, "轴向形位")
        lower = number(data.get("lower"), "被测段起点", -100000, 100000)
        upper = number(data.get("upper"), "被测段终点", -100000, 100000)
        count = integer(data.get("count", 15), "被测截面数", 3, MAX_FORM_POSITIONS)
        datum = choice(data.get("datum", "spindle"), "轴基准", DATUMS)
        datum_lower = None
        datum_upper = None
        datum_count = None
        if datum == "selected":
            datum_lower = number(data.get("datum_lower"), "基准段起点", -100000, 100000)
            datum_upper = number(data.get("datum_upper"), "基准段终点", -100000, 100000)
            datum_count = integer(data.get("datum_count", 7), "基准截面数", 3, MAX_FORM_POSITIONS)
            if max(lower, datum_lower) < min(upper, datum_upper):
                raise InspectionError("基准段和被测段不能具有重叠的内部区间。", code="form-overlapping-ranges")
        return cls(lower, upper, count, datum, datum_lower, datum_upper, datum_count)

    def heights(self):
        measured = z_positions(self.lower, self.upper, self.count, maximum=MAX_FORM_POSITIONS)
        datum = z_positions(
            self.datum_lower, self.datum_upper, self.datum_count,
            maximum=MAX_FORM_POSITIONS,
        ) if self.datum == "selected" else []
        return measured, datum

    def data(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "datum": self.datum,
            "datum_lower": self.datum_lower,
            "datum_upper": self.datum_upper,
            "datum_count": self.datum_count,
        }


@dataclass(frozen=True)
class CenterTrace:
    reference_z: float
    coordinates: np.ndarray
    slopes: np.ndarray
    limits: tuple | None

    @classmethod
    def spindle(cls, first, last):
        return cls((first+last)*0.5, np.zeros(2), np.zeros(2), None)

    @classmethod
    def fit(cls, centers):
        if len(centers) < MIN_DATUM_POSITIONS:
            return None
        z = np.asarray([value[0] for value in centers], dtype=float)
        xy = np.asarray([value[1] for value in centers], dtype=float)
        minimum, maximum = float(np.min(z)), float(np.max(z))
        if maximum-minimum < 1e-6:
            return None
        reference = float(np.mean(z))
        coordinates = np.mean(xy, axis=0)
        anchor = z-reference
        slopes = (anchor @ (xy-coordinates))/float(anchor @ anchor)
        return cls(reference, coordinates, slopes, (minimum, maximum))

    def at(self, z):
        return self.coordinates+(z-self.reference_z)*self.slopes

    def is_extension(self, z):
        return bool(self.limits and (z < self.limits[0]-1e-7 or z > self.limits[1]+1e-7))

    def data(self):
        return {
            "reference_z": self.reference_z,
            "coordinate": self.coordinates.tolist(),
            "slope": self.slopes.tolist(),
            "tilt_deg": math.degrees(math.atan(float(np.linalg.norm(self.slopes)))),
            "fit_range": list(self.limits) if self.limits else None,
        }


def form_scan(meter, request):
    heights, datum_heights = request.heights()
    unique = list(dict.fromkeys(datum_heights+heights))
    active = sum(view.included for view in meter.project.views)
    if len(unique)*active > MAX_FORM_OBSERVATIONS:
        raise InspectionError(
            "选定的截面数与转台角度过多，请减少抽样。",
            code="form-observation-limit",
        )
    observations = meter.observe_grid(unique)
    measured = {}
    for z, evidence in zip(unique, observations):
        section = shape_section(meter.project, z, 0, evidence, detailed=True)
        circle = measure_section(section) if section["state"] == "measured" else None
        measured[z] = section, circle

    datum_rows = [fit_row(z, *measured[z]) for z in datum_heights]
    datum = CenterTrace.spindle(heights[0], heights[-1])
    datum_result = {"kind": request.datum, "state": "spindle", "rows": datum_rows}
    if request.datum == "selected":
        datum, datum_result = selected_datum(datum_rows, datum_heights)
    inspected = [fit_row(z, *measured[z]) for z in heights]
    body_fit = CenterTrace.fit(list(centers_from(inspected)))
    inspection_rows = [
        report_row(row, measured[row["z"]][1], datum, body_fit)
        for row in inspected
    ]
    return {
        "revision": meter.project.revision,
        "project_id": meter.project.id,
        "parameters": request.data(),
        "datum": {**datum_result, "axis": datum.data() if datum else None},
        "inspection_axis": body_fit.data() if body_fit else None,
        "rows": inspection_rows,
        "summary": summarize_scan(inspection_rows, datum, body_fit),
    }


def fit_row(z, section, circle):
    state = section["state"]
    if state == "measured" and circle is None:
        state = "no-fit"
    return {
        "z": z,
        "state": state,
        "usable_views": section["usable_views"],
        "max_angle_gap": section["normal_coverage"]["gap"],
        "center": circle.center.tolist() if circle else None,
        "diameter": circle.radius*2 if circle else None,
        "form_spread": circle.boundary.extremes(circle.center).data()["spread"] if circle else None,
        "fit_rms": circle.rms if circle else None,
    }


def centers_from(rows):
    for row in rows:
        if row["center"] is not None:
            yield row["z"], row["center"]


def selected_datum(rows, heights):
    centers = list(centers_from(rows))
    required_span = (heights[-1]-heights[0])*0.60
    observed_span = centers[-1][0]-centers[0][0] if centers else 0
    details = {
        "kind": "selected",
        "state": "selected",
        "rows": rows,
        "valid_centers": len(centers),
        "required_span": required_span,
        "observed_span": observed_span,
        "maximum_residual": None,
        "rms_residual": None,
    }
    datum = CenterTrace.fit(centers)
    if datum is None or observed_span < required_span:
        details.update(
            state="datum-insufficient",
            detail="基准区至少需三个有效圆心，且首尾有效点须覆盖所选区间的六成。",
        )
        return None, details
    residual = [float(np.linalg.norm(np.asarray(center)-datum.at(z))) for z, center in centers]
    details.update(
        maximum_residual=max(residual),
        rms_residual=math.sqrt(sum(value**2 for value in residual)/len(residual)),
    )
    return datum, details


def report_row(row, circle, datum, fitted):
    result = {
        **row,
        "reference": datum.at(row["z"]).tolist() if datum else None,
        "reference_extrapolated": datum.is_extension(row["z"]) if datum else None,
        "offset": None,
        "radial_spread": None,
        "radii": None,
        "center_residual": None,
        "radial_state": "no-datum" if not datum else "no-contour",
    }
    if circle is None:
        return result
    if fitted:
        residual = circle.center-fitted.at(row["z"])
        result["center_residual"] = float(np.linalg.norm(residual))
    if datum:
        finding = circle_change(circle, datum.at(row["z"]))
        radii = finding["radii"]
        result.update(
            offset=finding["distance"],
            radial_spread=radii["spread"] if radii else None,
            radii=radii,
            radial_state=finding["state"],
        )
    return result


def missing_ranges(rows):
    spans = []
    start = None
    end = None
    for row in rows:
        if row["center"] is None:
            if start is None:
                start = row["z"]
            end = row["z"]
        elif start is not None:
            spans.append([start, end])
            start = None
    if start is not None:
        spans.append([start, end])
    return spans


def extreme(rows, field, *, smallest=False):
    valid = [row for row in rows if row[field] is not None]
    if not valid:
        return None
    select = min if smallest else max
    row = select(valid, key=lambda item: item[field])
    return {"z": row["z"], "value": row[field]}


def slope_angle(a, b):
    va = np.r_[a.slopes, 1.0]
    vb = np.r_[b.slopes, 1.0]
    return math.degrees(math.atan2(float(np.linalg.norm(np.cross(va, vb))), float(va @ vb)))


def summarize_scan(rows, datum, fitted):
    available = [row for row in rows if row["center"] is not None]
    low = extreme(rows, "diameter", smallest=True)
    high = extreme(rows, "diameter")
    radial = [row for row in rows if row["radii"] is not None]
    radius_pool = [row["radii"] for row in radial]
    sweep = None
    if radius_pool:
        inner = min(radial, key=lambda row: row["radii"]["minimum_radius"])
        outer = max(radial, key=lambda row: row["radii"]["maximum_radius"])
        sweep = {
            "value": outer["radii"]["maximum_radius"]-inner["radii"]["minimum_radius"],
            "inner_z": inner["z"],
            "outer_z": outer["z"],
        }
    residual = extreme(rows, "center_residual")
    return {
        "state": "unavailable" if not available else "complete" if len(available) == len(rows) else "partial",
        "total": len(rows),
        "measured": len(available),
        "missing": missing_ranges(rows),
        "radial_sections": len(radial),
        "reference_extensions": sum(row["reference_extrapolated"] is True for row in available),
        "diameter_min": low,
        "diameter_max": high,
        "diameter_range": high["value"]-low["value"] if low and high else None,
        "maximum_section_deviation": extreme(rows, "form_spread"),
        "maximum_axis_offset": extreme(rows, "offset"),
        "maximum_radial_spread": extreme(rows, "radial_spread"),
        "radial_sweep": sweep,
        "center_line_diameter": 2*residual["value"] if residual else None,
        "center_line_farthest_z": residual["z"] if residual else None,
        "axis_angle": slope_angle(datum, fitted) if datum and fitted else None,
    }

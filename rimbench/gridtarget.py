from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .domain import InspectionError, integer, number, record
from .gridfocus import field_focus
from .images import png_from_array
from .targets import Window, measure_target


MIN_REGISTERED = 6
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")


@dataclass(frozen=True)
class GridRequest:
    corners: tuple
    pitch_x: float
    pitch_y: float
    radius: int
    expected: float
    scale_limit: float
    residual_limit: float
    focus_limit: float

    @classmethod
    def parse(cls, doc, width, height):
        data = record(doc, "九点标板")
        anchors = []
        for name in CORNERS:
            point = record(data.get(name), "角点坐标")
            anchors.append((
                number(point.get("x"), "锚点 X", 0, width-1),
                number(point.get("y"), "锚点 Y", 0, height-1),
            ))
        pitch_x = number(data.get("pitch_x"), "横向点距", 0.01, 10000)
        pitch_y = number(data.get("pitch_y"), "纵向点距", 0.01, 10000)
        radius = integer(data.get("radius", 12), "靶点搜索半径", 4, 45)
        expected = number(data.get("expected"), "工位像素比例", 0.001, 1000000)
        scale_limit = number(data.get("scale_limit", 0.015), "容许像素比例偏差", 0.001, 0.50)
        distortion = number(data.get("residual_limit", 0.35), "容许定位残差", 0.01, 20)
        focus = number(data.get("focus_limit", 2.8), "圆靶边缘宽度限值", 0.2, 16)
        parsed = cls(tuple(anchors), pitch_x, pitch_y, radius, expected, scale_limit, distortion, focus)
        parsed.check_spacing()
        return parsed

    def positions(self):
        a, b, c, d = (np.asarray(point) for point in self.corners)
        output = []
        for row in range(3):
            v = row/2
            for column in range(3):
                u = column/2
                upper = a*(1-u)+b*u
                lower = c*(1-u)+d*u
                estimate = upper*(1-v)+lower*v
                output.append((row, column, estimate))
        return output

    def check_spacing(self):
        a, b, c, d = (np.asarray(point) for point in self.corners)
        edges = (b-a, d-b, c-d, a-c)
        products = [
            edge[0]*following[1]-edge[1]*following[0]
            for edge, following in zip(edges, edges[1:]+edges[:1])
        ]
        if any(value <= 0 for value in products):
            raise InspectionError("四个锚点需要按影像坐标中的左上、右上、左下、右下排列。", code="grid-anchor-order")
        positions = [item[2] for item in self.positions()]
        for index, first in enumerate(positions):
            for second in positions[index+1:]:
                if np.max(np.abs(first-second)) <= 2*self.radius:
                    raise InspectionError("圆靶搜索框会重叠，请调整锚点或缩小搜索半径。", code="grid-overlap")

    def data(self):
        anchors = {
            name: {"x": point[0], "y": point[1]}
            for name, point in zip(CORNERS, self.corners)
        }
        return {
            **anchors,
            "pitch_x": self.pitch_x,
            "pitch_y": self.pitch_y,
            "radius": self.radius,
            "expected": self.expected,
            "scale_limit": self.scale_limit,
            "residual_limit": self.residual_limit,
            "focus_limit": self.focus_limit,
        }


@dataclass(frozen=True)
class TargetGrid:
    design: np.ndarray
    origin: np.ndarray
    center: np.ndarray

    @classmethod
    def from_detected(cls, rows):
        nominal = np.asarray([row["nominal_mm"] for row in rows], dtype=float)
        measured = np.asarray([row["observed_px"] for row in rows], dtype=float)
        source = np.mean(nominal, axis=0)
        destination = np.mean(measured, axis=0)
        fit, _, rank, singular = np.linalg.lstsq(nominal-source, measured-destination, rcond=None)
        if rank < 2 or singular[-1] <= singular[0]*1e-7:
            return None
        return cls(fit, source, destination)

    def predict(self, nominal):
        return self.center+(np.asarray(nominal)-self.origin)@self.design

    def analysis(self):
        x_axis, y_axis = self.design
        scale_x = float(np.linalg.norm(x_axis))
        scale_y = float(np.linalg.norm(y_axis))
        if min(scale_x, scale_y) <= 1e-8:
            return None
        similarity = float(np.clip((x_axis@y_axis)/(scale_x*scale_y), -1, 1))
        return {
            "origin_px": self.predict([0, 0]).tolist(),
            "x_axis": x_axis.tolist(),
            "y_axis": y_axis.tolist(),
            "x_scale": scale_x,
            "y_scale": scale_y,
            "anisotropy": (max(scale_x, scale_y)/min(scale_x, scale_y))-1,
            "angle_between_axes": math.degrees(math.acos(similarity)),
            "x_axis_rotation": math.degrees(math.atan2(x_axis[1], x_axis[0])),
        }


def detect_targets(gray, request):
    height, width = gray.shape
    rows = []
    for row, column, guess in request.positions():
        nominal = [column*request.pitch_x, row*request.pitch_y]
        candidate = {
            "row": row,
            "column": column,
            "nominal_mm": nominal,
            "predicted_location": guess.tolist(),
            "observed_px": None,
            "fit_px": None,
            "residual_px": None,
            "difference_px": None,
        }
        try:
            window = Window.around(round(float(guess[0])), round(float(guess[1])), request.radius, width, height)
            detection = measure_target(gray, window)
        except InspectionError as issue:
            candidate.update(state=issue.code, detail=str(issue), target=None)
        else:
            candidate.update(state="found", observed_px=detection["center"], target=detection)
        rows.append(candidate)
    return rows


def scale_at_intervals(valid, request):
    grid = {(row["row"], row["column"]): row for row in valid}
    sections = []
    for row, column in grid:
        first = grid[(row, column)]
        neighbors = (
            ("x", (row, column+1), request.pitch_x),
            ("y", (row+1, column), request.pitch_y),
        )
        for name, target, pitch in neighbors:
            other = grid.get(target)
            if not other:
                continue
            step = np.asarray(other["observed_px"])-np.asarray(first["observed_px"])
            length = float(np.linalg.norm(step))
            sections.append({
                "axis": name,
                "start": [row, column],
                "end": list(target),
                "scale": length/pitch,
                "deviation": length/(pitch*request.expected)-1,
            })
    return sections


def mark_residuals(rows, fit):
    for row in rows:
        nominal = row["nominal_mm"]
        prediction = fit.predict(nominal)
        row["fit_px"] = prediction.tolist()
        if row["observed_px"] is None:
            continue
        residual = np.asarray(row["observed_px"])-prediction
        row["difference_px"] = residual.tolist()
        row["residual_px"] = float(np.linalg.norm(residual))


def inspect_grid(gray, request):
    height, width = gray.shape
    rows = detect_targets(gray, request)
    valid = [row for row in rows if row["state"] == "found"]
    intervals = scale_at_intervals(valid, request)
    focus = field_focus(gray, rows, request.focus_limit)
    result = {
        "frame": [width, height],
        "parameters": request.data(),
        "state": "insufficient",
        "detected": len(valid),
        "points": rows,
        "fit": None,
        "summary": None,
        "intervals": intervals,
        "focus": focus,
    }
    spread = (len({row["row"] for row in valid}), len({row["column"] for row in valid}))
    if len(valid) < MIN_REGISTERED or min(spread) < 3:
        return result
    grid = TargetGrid.from_detected(valid)
    fit = grid.analysis() if grid else None
    if fit is None:
        return result
    mark_residuals(rows, grid)
    largest = max(valid, key=lambda row: row["residual_px"])
    detector = float(np.sqrt(np.mean([row["residual_px"]**2 for row in valid])))
    outliers = [row for row in valid if row["residual_px"] > request.residual_limit]
    deviations = [abs(fit[axis]/request.expected-1) for axis in ("x_scale", "y_scale")]
    outside_pitch = sum(abs(row["deviation"]) > request.scale_limit for row in intervals)
    warnings = []
    if outliers:
        warnings.append("靶心定位对线性标尺的残差超过限值。")
    if max(deviations) > request.scale_limit or outside_pitch:
        warnings.append("整体或局部的像素比例偏离工位标尺。")
    if len(valid) < len(rows):
        warnings.append("部分标板圆点无法读取。")
    if focus["wide_targets"]:
        warnings.append("部分圆靶的成像过渡带超过清晰度限值。")
    result.update(
        state="review" if warnings else "checked",
        fit=fit,
        summary={
            "maximum_residual": largest["residual_px"],
            "maximum_at": [largest["row"], largest["column"]],
            "rms_residual": detector,
            "max_global_scale_deviation": max(deviations),
            "outside_scale_intervals": outside_pitch,
            "detected_but_outside_residual": len(outliers),
            "warnings": warnings,
        },
    )
    return result


def grid_fixture(width, height, scale, *, distortion=False):
    radius = max(2, min(12, int(min(width, height)*0.022)))
    pitch = min(width, height)*0.27
    origin_x = (width-1)/2-pitch
    origin_y = (height-1)/2-pitch
    tilt = 0.016*pitch
    corners = (
        (origin_x, origin_y),
        (origin_x+2*pitch, origin_y+2*tilt),
        (origin_x-2*tilt, origin_y+2*pitch),
        (origin_x+2*pitch-2*tilt, origin_y+2*pitch+2*tilt),
    )
    y, x = np.ogrid[:height, :width]
    canvas = (0.86+0.03*np.cos(x/max(width, 1)*4)-0.02*y/height).astype(np.float32)
    for row in range(3):
        for column in range(3):
            cx = origin_x+column*pitch-row*tilt
            cy = origin_y+row*pitch+column*tilt
            if distortion and row == column == 1:
                cx += radius*0.55
                cy -= radius*0.18
            d = np.hypot(x-cx, y-cy)
            coverage = np.clip(radius+0.65-d, 0, 1)
            canvas *= 1-coverage*0.85
    guess = {name: {"x": point[0], "y": point[1]} for name, point in zip(CORNERS, corners)}
    params = {
        **guess,
        "pitch_x": math.hypot(pitch, tilt)/scale,
        "pitch_y": math.hypot(pitch, tilt)/scale,
        "expected": scale,
        "radius": min(45, radius+5),
        "scale_limit": 0.015,
        "residual_limit": 0.35,
        "focus_limit": 2.8,
    }
    return png_from_array(np.rint(canvas*65535).astype(np.uint16)), params

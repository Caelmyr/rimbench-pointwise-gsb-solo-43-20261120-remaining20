from __future__ import annotations

from dataclasses import dataclass
import math

from .domain import Calibration, InspectionError, View, number


@dataclass(frozen=True)
class AxisFrame:
    origin_x: float
    origin_y: float
    down_x: float
    down_y: float
    across_x: float
    across_y: float
    scale: float
    width: int
    height: int

    @classmethod
    def from_calibration(cls, calibration: Calibration, width, height):
        dx = calibration.bottom_x-calibration.top_x
        dy = calibration.bottom_y-calibration.top_y
        length = math.hypot(dx, dy)
        if length <= 0 or width <= 0 or height <= 0:
            raise InspectionError("需要有效的画面和轴标定。")
        down_x = dx/length
        down_y = dy/length
        return cls(
            calibration.bottom_x, calibration.bottom_y,
            down_x, down_y, down_y, -down_x,
            calibration.px_per_mm, width, height,
        )

    def to_image(self, radial, z, view: View | None = None):
        x = self.origin_x + self.scale*(self.across_x*radial - self.down_x*z)
        y = self.origin_y + self.scale*(self.across_y*radial - self.down_y*z)
        if view is not None:
            x += view.shift_x
            y += view.shift_y
        return x, y

    def from_image(self, x, y, view: View | None = None):
        if view is not None:
            x -= view.shift_x
            y -= view.shift_y
        dx = (x-self.origin_x)/self.scale
        dy = (y-self.origin_y)/self.scale
        return dx*self.across_x+dy*self.across_y, -(dx*self.down_x+dy*self.down_y)

    def z_limits(self, view=None, inset=0):
        x = self.origin_x
        y = self.origin_y
        if view is not None:
            x += view.shift_x
            y += view.shift_y
        raw = _line_interval(
            x, y, -self.scale*self.down_x, -self.scale*self.down_y,
            inset, self.width-1-inset, inset, self.height-1-inset,
        )
        if raw is None:
            return None
        return tuple(round(k, 6) for k in raw)

    def radial_limits(self, z, view=None, inset=0):
        x, y = self.to_image(0, z, view)
        return _line_interval(
            x, y, self.scale*self.across_x, self.scale*self.across_y,
            inset, self.width-1-inset, inset, self.height-1-inset,
        )

    def sample_line(self, z, view=None, *, pitch_px=0.5):
        interval = self.radial_limits(z, view)
        if interval is None:
            return None
        lower, upper = interval
        steps = max(2, int(math.ceil((upper-lower)*self.scale/pitch_px))+1)
        return lower, upper, steps

    def axial_physical_range(self, views, margin):
        extents = [self.z_limits(view, margin) for view in views]
        extents = [extent for extent in extents if extent]
        if not extents:
            return None
        return min(k[0] for k in extents), max(k[1] for k in extents)

    def geometry(self, view=None):
        base = self.to_image(0, 0, view)
        axis = self.z_limits(view)
        return {
            "origin": list(base),
            "axis": [list(self.to_image(0, z, view)) for z in axis] if axis else [],
            "across_unit": [self.across_x, self.across_y],
            "up_unit": [-self.down_x, -self.down_y],
            "px_per_mm": self.scale,
        }

    def format_click(self, x, y, view=None):
        x = number(x, "影像横坐标", 0, self.width-1)
        y = number(y, "影像纵坐标", 0, self.height-1)
        radial, height = self.from_image(x, y, view)
        return {
            "x": x,
            "y": y,
            "radial_mm": round(radial, 5),
            "z_mm": round(height, 5),
        }


def _line_interval(x, y, dx, dy, left, right, top, bottom):
    if left > right or top > bottom:
        return None
    minimum = -math.inf
    maximum = math.inf
    for p, velocity, lower, upper in ((x, dx, left, right), (y, dy, top, bottom)):
        if abs(velocity) < 1e-12:
            if p < lower or p > upper:
                return None
            continue
        enter = (lower-p)/velocity
        leave = (upper-p)/velocity
        minimum = max(minimum, min(enter, leave))
        maximum = min(maximum, max(enter, leave))
        if maximum < minimum:
            return None
    return minimum, maximum


def z_positions(first, last, count, *, maximum=121):
    first = number(first, "起始高度", -100000, 100000)
    last = number(last, "结束高度", -100000, 100000)
    if not 2 <= count <= maximum:
        raise InspectionError(f"截面数需在 2–{maximum} 之间。")
    if last <= first:
        raise InspectionError("结束高度要大于起始高度。")
    if last-first < 1e-6:
        raise InspectionError("所选的轴向区间太窄。")
    gap = (last-first)/(count-1)
    return [first+i*gap if i < count-1 else last for i in range(count)]


def effective_reference_length(calibration, pixel_uncertainty):
    scale = calibration.px_per_mm
    scale_low = calibration.scale_px/(calibration.scale_mm+calibration.ruler_uncertainty)
    scale_high = calibration.scale_px/(calibration.scale_mm-calibration.ruler_uncertainty)
    return {
        "px_per_mm": scale,
        "scale_range": [scale_low, scale_high],
        "endpoint_mm": pixel_uncertainty/scale,
        "relative_scale_error": calibration.ruler_uncertainty/calibration.scale_mm,
    }


def apply_scale_envelope(metric, values, relative_error):
    valid = [value for value in values if value is not None and math.isfinite(value)]
    if not valid:
        return None
    if metric == "roundness":
        return [min(valid), max(valid)]
    exponent = 2 if metric == "area" else 1
    lower = min(valid)*(1-relative_error)**exponent
    upper = max(valid)*(1+relative_error)**exponent
    return [max(0.0, lower), upper]

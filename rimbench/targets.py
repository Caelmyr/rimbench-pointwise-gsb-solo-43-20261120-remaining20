from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy import ndimage

from .domain import Calibration, InspectionError, integer, number, record
from .images import gray_from_png, png_from_array


@dataclass(frozen=True)
class Window:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def parse(cls, document, width, height, *, minimum=9):
        data = record(document, "取景框")
        x = integer(data.get("x"), "取景框横坐标", 0, width-1)
        y = integer(data.get("y"), "取景框纵坐标", 0, height-1)
        w = integer(data.get("width"), "取景框宽度", minimum, width)
        h = integer(data.get("height"), "取景框高度", minimum, height)
        if x+w > width or y+h > height:
            raise InspectionError("取景框需要位于图像内。", code="window-outside")
        return cls(x, y, w, h)

    @classmethod
    def around(cls, x, y, half, width, height):
        x = integer(x, "中心横坐标", 0, width-1)
        y = integer(y, "中心纵坐标", 0, height-1)
        half = integer(half, "取景半径", 4, 100)
        return cls.parse(
            {"x": x-half, "y": y-half, "width": half*2+1, "height": half*2+1},
            width, height,
        )

    def slices(self):
        return slice(self.y, self.y+self.height), slice(self.x, self.x+self.width)

    def overlaps(self, other):
        return (
            self.x < other.x+other.width and other.x < self.x+self.width
            and self.y < other.y+other.height and other.y < self.y+self.height
        )

    def data(self):
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def propose_axis(png, document, width, height):
    doc = record(document, "两点标靶")
    upper = Window.parse(doc.get("upper"), width, height)
    lower = Window.parse(doc.get("lower"), width, height)
    if upper.overlaps(lower):
        raise InspectionError("两个标靶框不能重叠。")
    _check_search_size(upper)
    _check_search_size(lower)
    known_spacing = number(doc.get("spacing_mm"), "标靶中心间距", 0.01, 10000)
    ruler_error = number(doc.get("uncertainty_mm", 0.02), "已知间距误差", 0, 100)
    gray = gray_from_png(png)
    first = measure_target(gray, upper)
    second = measure_target(gray, lower)
    ax, ay = first["center"]
    bx, by = second["center"]
    gap = math.hypot(ax-bx, ay-by)
    if gap <= max(first["diameter_px"], second["diameter_px"])*1.8:
        raise InspectionError("两个靶心距离太小，无法获得稳定的像素比例。", code="target-spacing")
    result = Calibration.from_data(
        {"top_x": ax, "top_y": ay, "bottom_x": bx, "bottom_y": by,
         "scale_px": gap, "scale_mm": known_spacing, "ruler_uncertainty": ruler_error},
        width, height,
    )
    return {
        "calibration": result.data(), "targets": [first, second],
        "separation_px": gap, "tilt_deg": math.degrees(math.atan(result.slope)),
        "target_diameter_ratio": min(first["diameter_px"], second["diameter_px"])/
                                 max(first["diameter_px"], second["diameter_px"]),
    }


def _check_search_size(window):
    if window.width > 220 or window.height > 220:
        raise InspectionError("请将单个标靶框缩小到 220×220 像素以内。", code="target-window")


def measure_target(gray, window):
    patch = gray[window.slices()]
    border = np.concatenate((patch[0, :], patch[-1, :], patch[1:-1, 0], patch[1:-1, -1]))
    background = float(np.median(border))
    dark = float(np.percentile(patch, 6))
    separation = background-dark
    if separation < 0.13:
        raise InspectionError("框内没有对比明显的暗圆心。", code="target-contrast", details={"window": window.data()})
    threshold = background-separation*0.44
    components, count = ndimage.label(patch < threshold)
    values = np.bincount(components.ravel(), minlength=count+1)
    values[0] = 0
    identity = int(np.argmax(values))
    area = int(values[identity])
    if area < 9 or area > patch.size*0.68:
        raise InspectionError("标靶相对于取景框过小或过大。", code="target-size", details={"window": window.data()})
    other = values.copy()
    other[identity] = 0
    if int(other.max()) > area*0.32:
        raise InspectionError("一个标靶框内有多个显著暗目标。", code="target-ambiguous", details={"window": window.data()})
    selected = components == identity
    if selected[0].any() or selected[-1].any() or selected[:, 0].any() or selected[:, -1].any():
        raise InspectionError("暗标靶碰到取景边缘，请重新框选。", code="target-clipped", details={"window": window.data()})
    ys, xs = np.where(selected)
    xspan, yspan = xs.max()-xs.min()+1, ys.max()-ys.min()+1
    if min(xspan, yspan)/max(xspan, yspan) < 0.58:
        raise InspectionError("框中的暗目标偏细，无法作为圆靶。", code="target-shape", details={"window": window.data()})
    fill = area/(xspan*yspan)
    if fill < 0.47:
        raise InspectionError("暗目标的形状不像完整的圆靶。", code="target-shape", details={"window": window.data()})
    weight = np.maximum(background-patch[selected], 1e-5)
    cx = float(np.average(xs, weights=weight))
    cy = float(np.average(ys, weights=weight))
    diameter = 2*math.sqrt(area/math.pi)
    return {
        "window": window.data(), "center": [cx+window.x, cy+window.y],
        "diameter_px": diameter, "area_px": area, "background": background,
        "contrast": separation, "threshold": threshold,
        "fill": fill, "extent": [int(xspan), int(yspan)],
        "bounds": [int(xs.min())+window.x, int(ys.min())+window.y,
                   int(xs.max())+window.x, int(ys.max())+window.y],
    }


def synthetic_target(width, height):
    x = (width-1)*0.503
    top = height*0.26
    bottom = height*0.74
    tilt = min(width*0.03, max(0.1, (bottom-top)*0.05))
    radius = max(3.0, min(width, height)*0.035)
    cy, cx = np.ogrid[:height, :width]
    dist1 = np.hypot(cx-x, cy-top)
    dist2 = np.hypot(cx-x-tilt, cy-bottom)
    coverage = np.clip((radius-np.minimum(dist1, dist2)+0.7)/1.4, 0, 1)
    shade = (0.87+0.026*np.cos(cx/max(width, 1)*3.9))*np.ones((height, 1))
    pixels = np.round((shade*(1-0.86*coverage))*255).astype(np.uint8)
    return png_from_array(pixels), {
        "upper": [x, top, radius], "lower": [x+tilt, bottom, radius],
        "spacing_px": math.hypot(tilt, bottom-top),
    }

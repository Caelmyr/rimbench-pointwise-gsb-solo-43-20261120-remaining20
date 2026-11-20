from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy import ndimage

from .domain import InspectionError, integer, number, record
from .images import png_from_array
from .targets import Window


MAX_FIELD_PIXELS = 350_000
MAX_REGIONS = 64
FLOOR = 0.20
CEILING = 0.995


@dataclass(frozen=True)
class FieldRequest:
    window: Window
    drift_limit: float
    visible_fraction: float
    minimum_region: int

    @classmethod
    def parse(cls, data, width, height):
        data = record(data, "光场复检")
        source = data.get("window")
        window = Window.parse(source, width, height, minimum=12) if source else Window(0, 0, width, height)
        threshold = number(data.get("drift_limit", 0.045), "允许的相对偏差", 0.005, 0.45)
        valid = number(data.get("visible_fraction", 0.5), "最小有效画面比例", 0.05, 1.0)
        area = integer(data.get("minimum_region", 16), "最小连续异常像素数", 4, 10000)
        return cls(window, threshold, valid, area)

    def data(self):
        return {
            "window": self.window.data(),
            "drift_limit": self.drift_limit,
            "visible_fraction": self.visible_fraction,
            "minimum_region": self.minimum_region,
        }


@dataclass(frozen=True)
class FieldGrid:
    captured: np.ndarray
    reference: np.ndarray
    stride: int

    @classmethod
    def sample(cls, source, reference, window):
        y, x = window.slices()
        stride = max(1, math.ceil(math.sqrt(window.width*window.height/MAX_FIELD_PIXELS)))
        steps_y = slice(y.start, y.stop, stride)
        steps_x = slice(x.start, x.stop, stride)
        return cls(source[steps_y, steps_x], reference[steps_y, steps_x], stride)

    def masks(self):
        return {
            "dark": (self.reference < FLOOR) | (self.captured < FLOOR),
            "bright": (self.reference > CEILING) | (self.captured > CEILING),
        }

    def valid(self):
        masks = self.masks()
        return ~(masks["dark"] | masks["bright"])

    def normalize(self, visible):
        ratio = np.divide(
            self.captured, self.reference,
            out=np.zeros(self.captured.shape, dtype=np.float32),
            where=visible,
        )
        levels = ratio[visible]
        gain = float(np.median(levels))
        scaled = np.zeros(ratio.shape, dtype=np.float32)
        scaled[visible] = ratio[visible]/gain - 1
        return scaled, gain


def quadrant_layout(shape):
    rows, columns = shape
    ymiddle, xmiddle = rows//2, columns//2
    return [
        ("top-left", (slice(0, ymiddle), slice(0, xmiddle))),
        ("top-right", (slice(0, ymiddle), slice(xmiddle, columns))),
        ("bottom-left", (slice(ymiddle, rows), slice(0, xmiddle))),
        ("bottom-right", (slice(ymiddle, rows), slice(xmiddle, columns))),
    ]


def quadrants(grid, residual, visible, drift_limit):
    slices = quadrant_layout(grid.captured.shape)
    rows = []
    for name, position in slices:
        segment = visible[position]
        weights = residual[position][segment]
        data = {
            "quadrant": name,
            "captured_median": float(np.median(grid.captured[position])),
            "reference_median": float(np.median(grid.reference[position])),
            "valid_fraction": float(np.mean(segment)),
            "median_deviation": None,
            "local_drift": None,
        }
        if weights.size:
            data.update(
                median_deviation=float(np.median(weights)),
                local_drift=float(np.mean(np.abs(weights) > drift_limit)),
            )
        rows.append(data)
    return rows


def window_bounds(window, stride, row_slice, col_slice):
    first_x = window.x+col_slice.start*stride
    first_y = window.y+row_slice.start*stride
    last_x = min(window.x+window.width, window.x+col_slice.stop*stride)
    last_y = min(window.y+window.height, window.y+row_slice.stop*stride)
    return [first_x, first_y, last_x-first_x, last_y-first_y]


def patches(mask, residual, request, stride, kind):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return [], 0
    masses = np.bincount(labels.ravel(), minlength=count+1)
    minimum = math.ceil(request.minimum_region/(stride*stride))
    candidates = np.flatnonzero(masses[1:] >= minimum)+1
    order = candidates[np.argsort(-masses[candidates], kind="stable")]
    boxes = ndimage.find_objects(labels)
    records = []
    for identity in order[:MAX_REGIONS]:
        roi = boxes[identity-1]
        local = labels[roi] == identity
        values = residual[roi][local]
        positions = np.argwhere(local)
        cy, cx = np.mean(positions, axis=0)
        records.append({
            "type": kind,
            "bounds": window_bounds(request.window, stride, *roi),
            "center": [
                float(request.window.x+(roi[1].start+cx)*stride),
                float(request.window.y+(roi[0].start+cy)*stride),
            ],
            "sample_count": int(masses[identity]),
            "mean_deviation": float(np.mean(values)),
            "max_absolute_deviation": float(np.max(np.abs(values))),
        })
    return records, len(candidates)


def residual_distribution(residual, visible, limit):
    values = residual[visible]
    centers = np.array([-4, -2, -1, -0.5, 0, 0.5, 1, 2, 4])*limit
    histogram, edges = np.histogram(values, bins=np.r_[-np.inf, centers, np.inf])
    return {
        "low_fraction": float(np.mean(values < -limit)),
        "high_fraction": float(np.mean(values > limit)),
        "median_absolute_deviation": float(np.median(np.abs(values))),
        "p05": float(np.quantile(values, 0.05)),
        "p95": float(np.quantile(values, 0.95)),
        "bins": [
            {"lower": float(a) if np.isfinite(a) else None,
             "upper": float(b) if np.isfinite(b) else None,
             "count": int(value)}
            for a, b, value in zip(edges[:-1], edges[1:], histogram)
        ],
    }


def overlay(grid, residual, visible, regions, limit, window):
    rgba = np.zeros((*grid.captured.shape, 4), dtype=np.uint8)
    omitted = ~visible
    weak = residual < -limit
    strong = residual > limit
    rgba[visible] = [43, 173, 151, 18]
    rgba[omitted] = [101, 105, 115, 130]
    rgba[weak] = [61, 127, 225, 215]
    rgba[strong] = [228, 108, 55, 215]
    for region in regions:
        px, py, width, height = region["bounds"]
        first_x = (px-window.x)//grid.stride
        first_y = (py-window.y)//grid.stride
        last_x = first_x+math.ceil(width/grid.stride)-1
        last_y = first_y+math.ceil(height/grid.stride)-1
        color = [250, 250, 249, 240]
        rgba[first_y, first_x:last_x+1] = color
        rgba[last_y, first_x:last_x+1] = color
        rgba[first_y:last_y+1, first_x] = color
        rgba[first_y:last_y+1, last_x] = color
    return png_from_array(rgba)


def compare_fields(source, reference, request, *, render=False):
    if source.shape != reference.shape:
        raise InspectionError("两张空场图画幅不一致。", code="field-size")
    grid = FieldGrid.sample(source, reference, request.window)
    masks = grid.masks()
    visible = grid.valid()
    valid_fraction = float(np.mean(visible))
    blank = np.zeros(grid.captured.shape, dtype=np.float32)
    result = {
        "state": "insufficient",
        "parameters": request.data(),
        "stride": grid.stride,
        "sample_count": int(grid.captured.size),
        "valid_fraction": valid_fraction,
        "dark_fraction": float(np.mean(masks["dark"])),
        "saturated_fraction": float(np.mean(masks["bright"])),
        "gain": None,
        "residual": None,
        "quadrants": [],
        "regions": [],
        "region_count": 0,
    }
    if valid_fraction < request.visible_fraction:
        return overlay(grid, blank, visible, [], request.drift_limit, request.window) if render else result

    residual, gain = grid.normalize(visible)
    dim = visible & (residual < -request.drift_limit)
    bright = visible & (residual > request.drift_limit)
    low, count_low = patches(dim, residual, request, grid.stride, "dimmer")
    high, count_high = patches(bright, residual, request, grid.stride, "brighter")
    regions = sorted(low+high, key=lambda row: (-row["sample_count"], -row["max_absolute_deviation"]))[:MAX_REGIONS]
    if render:
        return overlay(grid, residual, visible, regions, request.drift_limit, request.window)
    result.update(
        state="localized-drift" if regions else "global-gain" if abs(gain-1) > request.drift_limit else "stable",
        gain=gain,
        residual=residual_distribution(residual, visible, request.drift_limit),
        quadrants=quadrants(grid, residual, visible, request.drift_limit),
        region_count=count_low+count_high,
        regions=regions,
    )
    return result


def field_fixture(reference, variable):
    height, width = reference.shape
    rows, columns = np.ogrid[:height, :width]
    multiplier = np.full(reference.shape, 0.90, dtype=np.float32)
    if variable:
        shadow = np.exp(-(((columns-width*0.33)/(width*0.12))**2+((rows-height*0.32)/(height*0.16))**2))
        gleam = np.exp(-(((columns-width*0.76)/(width*0.07))**2+((rows-height*0.70)/(height*0.12))**2))
        multiplier += 0.13*gleam-0.19*shadow
    luma = np.clip(reference*multiplier, 0, 1)
    return png_from_array(np.rint(luma*65535).astype(np.uint16))

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

import numpy as np
from scipy.ndimage import map_coordinates

from .domain import InspectionError, integer, number, record
from .setup import lookup_view


MAX_AUDIT_VIEWS = 24
PENUMBRA_STEP = 0.25
MIN_SIGNAL_CONTRAST = 0.12
BACKGROUND_THRESHOLD = 0.18
LUMA_MIN = 0.005
LUMA_MAX = 0.995


@dataclass(frozen=True)
class EdgeRequest:
    z: float
    radius: int
    view: str | None

    @classmethod
    def parse(cls, data):
        data = record(data, "边缘质检条件")
        position = number(data.get("z", 22), "截面高度", -100000, 100000)
        radius = integer(data.get("radius", 8), "端点两侧检查长度", 4, 24)
        view = data.get("view") or None
        if view is not None and not isinstance(view, str):
            raise InspectionError("影像标识无效。")
        return cls(position, radius, view)

    def data(self):
        return {"z": self.z, "radius": self.radius, "view": self.view}


@dataclass(frozen=True)
class EdgeStrip:
    steps: np.ndarray
    pixels: np.ndarray
    raw: np.ndarray
    compensated: np.ndarray
    reference: np.ndarray | None

    @classmethod
    def extract(cls, frame, view, z, edge, side, radius, gray, reference, normalized):
        length = int(radius/PENUMBRA_STEP)
        steps = np.arange(-length, length+1)*PENUMBRA_STEP
        outward = -1 if side == "left" else 1
        distances = edge+outward*steps/frame.scale
        center_x, center_y = frame.to_image(0, z, view)
        x = center_x+distances*frame.scale*frame.across_x
        y = center_y+distances*frame.scale*frame.across_y
        if np.any((x < 0) | (x > frame.width-1) | (y < 0) | (y > frame.height-1)):
            return None
        xy = np.array([y, x])
        values = sample(gray, xy)
        compensation = sample(normalized, xy)
        ref = sample(reference, xy) if reference is not None else None
        return cls(steps, np.column_stack((x, y)), values, compensation, ref)

    def data(self):
        rows = []
        for index, (distance, px, raw, corrected) in enumerate(zip(
            self.steps, self.pixels, self.raw, self.compensated,
        )):
            rows.append({
                "distance_px": float(distance),
                "x": float(px[0]),
                "y": float(px[1]),
                "raw": float(raw),
                "normalized": float(corrected),
                "flat": float(self.reference[index]) if self.reference is not None else None,
            })
        return rows


def sample(image, xy):
    return map_coordinates(image, xy, order=1, mode="nearest", prefilter=False)


def plateau(strip):
    radius = float(strip.steps[-1])
    dark = strip.steps <= -0.60*radius
    light = strip.steps >= 0.60*radius
    low = float(np.median(strip.raw[dark]))
    high = float(np.median(strip.raw[light]))
    dark_spread = float(np.quantile(strip.raw[dark], 0.9)-np.quantile(strip.raw[dark], 0.1))
    light_spread = float(np.quantile(strip.raw[light], 0.9)-np.quantile(strip.raw[light], 0.1))
    ref_min = float(np.min(strip.reference)) if strip.reference is not None else None
    return {
        "shadow_level": low,
        "light_level": high,
        "contrast": high-low,
        "shadow_spread": dark_spread,
        "light_spread": light_spread,
        "shadow_clipped": float(np.mean(strip.raw[dark] <= LUMA_MIN)),
        "light_clipped": float(np.mean(strip.raw[light] >= LUMA_MAX)),
        "minimum_reference": ref_min,
    }


def crossings(steps, luma, threshold):
    candidates = []
    for a, b, before, after in zip(steps[:-1], steps[1:], luma[:-1], luma[1:]):
        if before == after:
            continue
        if min(before, after) <= threshold < max(before, after):
            t = (threshold-before)/(after-before)
            candidates.append((float(a+(b-a)*t), bool(after > before)))
    return candidates


def nearest_rise(candidates, anchor=0.0):
    rising = [point for point, goes_up in candidates if goes_up]
    return min(rising, key=lambda point: abs(point-anchor)) if rising else None


def estimate_transition(strip, tone):
    low = tone["shadow_level"]
    span = tone["contrast"]
    if span < MIN_SIGNAL_CONTRAST:
        return {"state": "low-contrast"}
    curves = [crossings(strip.steps, strip.raw, low+span*level) for level in (0.1, 0.5, 0.9)]
    center = nearest_rise(curves[1])
    if center is None:
        return {"state": "no-transition"}
    left = [point for point, rising in curves[0] if rising and point <= center]
    right = [point for point, rising in curves[2] if rising and point >= center]
    if not left or not right:
        return {"state": "incomplete-transition", "midpoint_px": center}
    low_cross = max(left)
    high_cross = min(right)
    internal_reversals = sum(
        low_cross < point < high_cross and not rise
        for stream in curves for point, rise in stream
    )
    return {
        "state": "unstable-transition" if len(curves[1]) != 1 or internal_reversals else "measured",
        "midpoint_px": center,
        "ten_percent_px": low_cross,
        "ninety_percent_px": high_cross,
        "width_px": high_cross-low_cross,
        "mid_level_crossings": len(curves[1]),
        "internal_reversals": internal_reversals,
    }


def edge_flags(tone, transition, uncertainty):
    issues = []
    if tone["minimum_reference"] is not None and tone["minimum_reference"] < BACKGROUND_THRESHOLD:
        issues.append("unlit-field")
    if tone["shadow_clipped"] >= 0.5 or tone["light_clipped"] >= 0.5:
        issues.append("clipped-tone")
    if max(tone["shadow_spread"], tone["light_spread"]) > 0.12:
        issues.append("uneven-plateau")
    if transition["state"] != "measured":
        issues.append(transition["state"])
    width = transition.get("width_px")
    if width is not None and width > max(1.5, uncertainty*2):
        issues.append("wide-edge")
    return issues


def inspect_edge(meter, view, shape, frame, z, endpoint, side, radius, *, detail):
    gray = meter.gray(view.asset)
    ref_asset = meter.project.reference
    flat = meter.gray(ref_asset) if ref_asset else None
    strip = EdgeStrip.extract(frame, view, z, endpoint, side, radius, gray, flat, shape.threshold_image)
    if strip is None:
        return {
            "side": side,
            "state": "outside-window",
            "endpoint_mm": endpoint,
            "flags": ["outside-window"],
        }
    tone = plateau(strip)
    transition = estimate_transition(strip, tone)
    flags = edge_flags(tone, transition, meter.project.segmentation.ambiguity_px)
    output = {
        "side": side,
        "state": "review" if flags else "measured",
        "endpoint_mm": endpoint,
        "endpoint_px": list(frame.to_image(endpoint, z, view)),
        "tone": tone,
        "transition": transition,
        "flags": flags,
    }
    if detail:
        output["trace"] = strip.data()
    return output


def inspect_capture(meter, frame, view, observation, request):
    if observation["state"] != "usable":
        return {
            "view_id": view.id,
            "title": view.title,
            "angle": view.angle,
            "state": observation["state"],
            "reason": observation["reason"],
            "edges": [],
        }
    shape = meter.view_mask(view)
    edges = [
        inspect_edge(meter, view, shape, frame, request.z, observation[key], side,
                     request.radius, detail=request.view is not None)
        for side, key in (("left", "lower"), ("right", "upper"))
    ]
    return {
        "view_id": view.id,
        "title": view.title,
        "angle": view.angle,
        "state": "measured" if all(edge["state"] == "measured" for edge in edges) else "review",
        "reason": observation["reason"],
        "edges": edges,
    }


def edge_report(meter, request):
    frame = meter.axis()
    if request.view:
        selected = lookup_view(meter.project, request.view)
        observation = meter.inspect_view(selected, request.z)["observation"]
        rows = [inspect_capture(meter, frame, selected, observation, request)]
        omitted = 0
    else:
        ordered = sorted((view for view in meter.project.views if view.included), key=lambda view: (view.angle, view.id))
        picked = ordered[:MAX_AUDIT_VIEWS]
        by_id = {
            row["view_id"]: row for row in meter.observations_at(request.z)
        } if len(ordered) <= MAX_AUDIT_VIEWS else {
            view.id: meter.inspect_view(view, request.z)["observation"] for view in picked
        }
        rows = [inspect_capture(meter, frame, view, by_id[view.id], request) for view in picked]
        omitted = len(ordered)-len(picked)
    return {
        "project_id": meter.project.id,
        "revision": meter.project.revision,
        "parameters": request.data(),
        "px_per_mm": frame.scale,
        "reference": meter.project.reference,
        "states": dict(Counter(row["state"] for row in rows)),
        "flags": dict(Counter(flag for row in rows for edge in row["edges"] for flag in edge["flags"])),
        "max_measured_width": largest_width(rows),
        "omitted_views": omitted,
        "rows": rows,
    }


def largest_width(rows):
    candidates = [
        (row, edge, edge["transition"]["width_px"])
        for row in rows for edge in row["edges"]
        if edge.get("transition", {}).get("width_px") is not None
    ]
    if not candidates:
        return None
    view, edge, width = max(candidates, key=lambda item: item[2])
    if not math.isfinite(width):
        return None
    return {"angle": view["angle"], "view_id": view["view_id"], "side": edge["side"], "width_px": width}

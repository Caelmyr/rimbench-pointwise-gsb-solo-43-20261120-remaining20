from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy import ndimage

from .drift import disabled as drift_disabled
from .drift import estimate as drift_estimate
from .drift import no_reference as drift_no_reference
from .images import image_stats, png_from_array


@dataclass(frozen=True)
class Silhouette:
    mask: np.ndarray
    threshold_image: np.ndarray
    summary: dict
    drift: object | None = None


def extract(gray, settings, exposure, reference=None, decision=None):
    source = gray/exposure
    if reference is not None and decision is not None and decision.model is not None:
        corrected = np.maximum(reference*decision.gain(gray.shape), 0.10)
    else:
        corrected = reference
    if corrected is not None:
        ratio = source/np.maximum(corrected, 0.10)
        unlit = corrected < 0.18
    else:
        levels = image_stats(gray)["quantiles"]
        gain = max(0.24, levels[3]/exposure)
        ratio = source/gain
        unlit = np.zeros(gray.shape, dtype=bool)
    foreground = (ratio < settings.cutoff) & ~unlit
    if settings.close_radius:
        pad = settings.close_radius
        expanded = np.pad(foreground, pad, mode="edge")
        closed = ndimage.binary_closing(expanded, structure=_disk(pad))
        foreground = closed[pad:-pad, pad:-pad]
    mask, materials = _main_component(foreground, settings.min_area)
    stats = image_stats(gray)
    occupied = float(np.mean(mask))
    frame_contact = _frame_contact(mask, settings.inset)
    warnings = []
    if not materials["kept"]:
        warnings.append("未检测到足够大的连通主体。")
    if occupied > 0.81:
        warnings.append("暗区覆盖超过画面八成，请检查曝光和构图。")
    if frame_contact:
        warnings.append("主体碰到影像保护边距，部分截线可能无效。")
    if materials["next_area"] > materials["kept"]*0.20:
        warnings.append("发现其他大片暗区，只保留面积最大的连通主体。")
    if float(np.mean(unlit)) > 0.02:
        warnings.append("空场中有未照亮区域。")
    contrast = _contrast(gray, mask, unlit)
    if contrast is not None and contrast < 0.18:
        warnings.append("物体与背景反差较低。")
    return Silhouette(
        mask, np.asarray(ratio, dtype=np.float32),
        {
            "valid": bool(materials["kept"] and occupied < 0.90),
            "occupied": round(occupied, 5),
            "frame_contact": frame_contact,
            "visible_pixels": materials["kept"],
            "component_count": materials["count"],
            "bounds": materials["bounds"],
            "contrast": contrast,
            "histogram": stats["histogram"],
            "quantiles": stats["quantiles"],
            "warnings": warnings,
            "drift": decision.data() if decision is not None else None,
        },
        decision,
    )


def decide(gray, settings, exposure, reference):
    """Choose the background that normalizes this frame and record why."""
    if reference is None:
        return drift_no_reference()
    if not settings.compensate_drift:
        return drift_disabled()
    return drift_estimate(gray/exposure, reference)


def _disk(radius):
    offsets = np.arange(-radius, radius+1)
    return offsets[:, None]**2+offsets[None, :]**2 <= radius**2


def _main_component(foreground, minimum):
    labels, count = ndimage.label(foreground, structure=np.ones((3, 3), dtype=bool))
    sizes = np.bincount(labels.ravel(), minlength=count+1)
    sizes[0] = 0
    keep = int(np.argmax(sizes))
    total = int(sizes[keep])
    enough = total >= minimum
    mask = labels == keep if enough else np.zeros(foreground.shape, dtype=bool)
    bounds = None
    if enough:
        rows = np.flatnonzero(np.any(mask, axis=1))
        cols = np.flatnonzero(np.any(mask, axis=0))
        bounds = [int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])]
    sizes[keep] = 0
    return mask, {"kept": total if enough else 0, "next_area": int(sizes.max()),
                  "count": int(np.count_nonzero(sizes >= minimum))+int(enough), "bounds": bounds}


def _frame_contact(mask, inset):
    ny, nx = mask.shape
    trim_x = min(inset+1, nx//3)
    trim_y = min(inset+1, ny//3)
    return [
        name for name, pixels in (
            ("left", mask[:, :trim_x]), ("right", mask[:, -trim_x:]),
            ("top", mask[:trim_y]), ("bottom", mask[-trim_y:]),
        ) if np.any(pixels)
    ]


def _contrast(gray, mask, unlit):
    inside = gray[mask]
    outside = gray[~mask & ~unlit]
    if inside.size and outside.size:
        foreground = float(np.median(inside[::max(1, inside.size//80_000)]))
        background = float(np.median(outside[::max(1, outside.size//80_000)]))
        return round(background-foreground, 5)
    return None


def observation(silhouette, axis, settings, view, z):
    view_info = {
        "view_id": view.id, "angle": view.angle, "title": view.title, "z": z,
        "drift": _drift_state(silhouette),
    }
    if not view.included:
        return {**view_info, "state": "excluded", "reason": "本角度未参与截面。"}
    if not silhouette.summary["valid"]:
        return {**view_info, "state": "image-rejected", "reason": "影像未检出可靠的计量主体。"}
    line = axis.sample_line(z, view)
    inner = axis.radial_limits(z, view, settings.inset)
    if line is None or inner is None:
        return {**view_info, "state": "outside", "reason": "截线超出画面保护范围。"}
    lower, upper, count = line
    radial = np.linspace(lower, upper, count)
    base = axis.to_image(0, z, view)
    xs = base[0]+radial*axis.scale*axis.across_x
    ys = base[1]+radial*axis.scale*axis.across_y
    xy = np.array([ys, xs])
    sampled = ndimage.map_coordinates(
        silhouette.mask.astype(np.uint8, copy=False), xy, order=0, mode="nearest", prefilter=False,
    ).astype(bool)
    runs = _runs(sampled)
    if not runs:
        return {**view_info, "state": "empty", "reason": "该高度无主体轮廓。"}
    lo, hi = max(runs, key=lambda pair: pair[1]-pair[0])
    step_px = (upper-lower)*axis.scale/max(1, count-1)
    if (hi-lo+1)*step_px < settings.min_span:
        return {**view_info, "state": "too-narrow", "reason": "连续暗部短于有效轮廓宽度。"}
    left_edge = _edge(radial, lo, -1)
    right_edge = _edge(radial, hi, 1)
    if left_edge <= inner[0] or right_edge >= inner[1]:
        return {**view_info, "state": "clipped", "reason": "主体截线的端点碰到画面边距。"}
    substantial = [(a, b) for a, b in runs if (b-a+1)*step_px >= settings.min_span]
    # A detached run is treated as ambiguous even if the longest run is obvious.
    if len(substantial) > 1:
        return {**view_info, "state": "disconnected", "reason": "截线与主体有多段明显交集。"}
    return {
        **view_info,
        "state": "usable", "reason": "两侧轮廓端点均位于图像以内。",
        "lower": round(float(left_edge), 7), "upper": round(float(right_edge), 7),
        "endpoints": [list(axis.to_image(left_edge, z, view)), list(axis.to_image(right_edge, z, view))],
        "span_px": round(float((right_edge-left_edge)*axis.scale), 4),
        "edge_error_mm": max(settings.ambiguity_px/axis.scale, step_px/(2*axis.scale)),
    }


def _drift_state(silhouette):
    summary = silhouette.summary.get("drift")
    if not summary:
        return None
    return {key: summary.get(key) for key in ("state", "reason", "gain_range")}


def _runs(sampled):
    edges = np.diff(np.pad(sampled.astype(np.int8), (1, 1)))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)-1))


def _edge(radial, index, direction):
    neighbor = index+direction
    value = radial[index]
    if 0 <= neighbor < len(radial):
        return (value+radial[neighbor])*0.5
    return value


def overlay_png(silhouette):
    mask = silhouette.mask
    contour = mask & ~ndimage.binary_erosion(mask)
    canvas = np.zeros((*mask.shape, 4), dtype=np.uint8)
    canvas[mask] = [4, 175, 145, 68]
    canvas[contour] = [55, 233, 192, 255]
    return png_from_array(canvas)


def ratio_preview(silhouette, maximum=512):
    array = silhouette.threshold_image
    factor = max(1, int(math.ceil(max(array.shape)/maximum)))
    clipped = np.clip(array[::factor, ::factor]*225, 0, 255)
    return png_from_array(np.rint(clipped).astype(np.uint8))

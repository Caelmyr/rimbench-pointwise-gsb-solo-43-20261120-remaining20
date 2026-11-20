from __future__ import annotations

import math

import numpy as np

from .edgecheck import MIN_SIGNAL_CONTRAST, crossings, nearest_rise, sample


SPOKES = 16
MIN_DOT_RADIUS = 3
MIN_SPOKES = 8
MIN_ORIENTATION_POINTS = 3
FOCUS_SAMPLE = 0.2


def fit_transition(distances, raw, expected, radius):
    inner = raw[distances < radius*0.66]
    outer = raw[distances > radius*1.38]
    low, high = float(np.median(inner)), float(np.median(outer))
    contrast = high-low
    if contrast < MIN_SIGNAL_CONTRAST:
        return None, "contrast"
    marks = [
        crossings(distances, raw, low+fraction*contrast)
        for fraction in (0.1, 0.5, 0.9)
    ]
    midpoint = nearest_rise(marks[1], expected)
    if midpoint is None or abs(midpoint-expected) > radius*0.30:
        return None, "ambiguous-edge"
    rising_nearby = [value for value, goes_up in marks[1] if goes_up and abs(value-expected) < radius*0.40]
    if len(rising_nearby) != 1:
        return None, "ambiguous-edge"
    earlier = [point for point, rising in marks[0] if rising and point < midpoint]
    later = [point for point, rising in marks[2] if rising and point > midpoint]
    if not earlier or not later:
        return None, "incomplete-edge"
    a, b = max(earlier), min(later)
    if a < radius*0.56 or b > radius*1.50:
        return None, "incomplete-edge"
    return {
        "ten_percent": a,
        "midpoint": midpoint,
        "ninety_percent": b,
        "width_px": b-a,
        "contrast": contrast,
    }, "measured"


def spoke_samples(gray, center, radius):
    height, width = gray.shape
    count = math.ceil(radius*1.12/FOCUS_SAMPLE)+1
    samples = np.linspace(radius*0.50, radius*1.62, count)
    for index in range(SPOKES):
        bearing = index*360/SPOKES
        radians = math.radians(bearing)
        x = center[0]+samples*math.cos(radians)
        y = center[1]+samples*math.sin(radians)
        if np.any((x < 0) | (x > width-1) | (y < 0) | (y > height-1)):
            yield {"angle": bearing, "state": "outside-image", "edge": None}
            continue
        profile = sample(gray, np.array([y, x]))
        logistic, state = fit_transition(samples, profile, radius, radius)
        yield {"angle": bearing, "state": state, "edge": logistic}


def measured_quantiles(rows):
    widths = [row["edge"]["width_px"] for row in rows]
    return {
        "count": len(widths),
        "minimum": min(widths),
        "median": float(np.median(widths)),
        "p90": float(np.quantile(widths, 0.9)),
        "maximum": max(widths),
    }


def orientation_summary(angles):
    orientations = {}
    for name, axis in (("horizontal", 0), ("vertical", 90)):
        compatible = [
            row for row in angles
            if abs((row["angle"]-axis+90) % 180-90) <= 22.5
        ]
        orientations[name] = measured_quantiles(compatible) if len(compatible) >= MIN_ORIENTATION_POINTS else None
    horizontal = orientations["horizontal"]
    vertical = orientations["vertical"]
    ratio = None
    if horizontal and vertical and min(horizontal["median"], vertical["median"]) > 1e-8:
        ratio = max(horizontal["median"], vertical["median"])/min(horizontal["median"], vertical["median"])
    return {**orientations, "width_ratio": ratio}


def dot_focus(gray, target, limit):
    if target is None:
        return {"state": "no-target", "angles": [], "widths": None, "directions": None}
    radius = target["diameter_px"]*0.5
    if radius < MIN_DOT_RADIUS:
        return {"state": "dot-too-small", "angles": [], "widths": None, "directions": None}
    angles = list(spoke_samples(gray, target["center"], radius))
    valid = [row for row in angles if row["state"] == "measured"]
    spread = measured_quantiles(valid) if len(valid) >= MIN_SPOKES else None
    planes = orientation_summary(valid) if spread else None
    state = "insufficient-spokes"
    if spread:
        state = "wide-edge" if spread["p90"] > limit else "measured"
    return {
        "state": state,
        "angles": angles,
        "widths": spread,
        "directions": planes,
    }


def field_focus(gray, points, limit):
    focus_points = []
    for row in points:
        evidence = dot_focus(gray, row["target"], limit)
        row["focus"] = evidence
        if evidence["widths"]:
            focus_points.append(row)
    incomplete = len(points)-len(focus_points)
    details = {
        "state": "insufficient",
        "measured_targets": len(focus_points),
        "target_count": len(points),
        "limit_px": limit,
        "wide_targets": 0,
        "max_target_p90": None,
        "max_target_at": None,
        "center_width": None,
        "outer_median_width": None,
        "outer_to_center": None,
    }
    if not focus_points:
        return details
    corner = [row["focus"]["widths"]["median"] for row in focus_points if row["row"] != 1 or row["column"] != 1]
    center = next((row["focus"]["widths"]["median"] for row in focus_points if row["row"] == 1 and row["column"] == 1), None)
    corner_median = float(np.median(corner)) if corner else None
    worst = max(focus_points, key=lambda row: row["focus"]["widths"]["p90"])
    wide = sum(row["focus"]["state"] == "wide-edge" for row in focus_points)
    details.update(
        state="review" if incomplete or wide else "checked",
        wide_targets=wide,
        max_target_p90=worst["focus"]["widths"]["p90"],
        max_target_at=[worst["row"], worst["column"]],
        center_width=center,
        outer_median_width=corner_median,
        outer_to_center=corner_median/center if center and corner_median else None,
    )
    return details

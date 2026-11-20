from __future__ import annotations

from dataclasses import dataclass

import math

import numpy as np
from scipy import ndimage

from .images import png_from_array


# Pixels only count as background evidence when both the morning reference and
# the current frame are clearly lit and their ratio stays inside a plausible
# illumination change.  Object shadows and specular highlights fall outside.
MIN_REFERENCE = 0.28
MIN_SOURCE = 0.30
UNLIT_REFERENCE = 0.18
SATURATED = 0.995
RATIO_LOW = 0.55
RATIO_HIGH = 1.90
LOCAL_FRACTION = 0.40
TILE_FRACTION = 0.50
TILE_SPREAD = 0.10
TILE_RESIDUAL = 0.10
TILE_SUSPICIOUS = 0.035
MIN_SIDE_TILES = 3
MIN_SIDE_ROWS = 2
MIN_SIDE_PIXELS = 0.0012
FIT_LIMIT = 0.06
GAIN_LIMITS = (0.50, 2.00)
SIDE_NAMES = {"left": "左侧", "right": "右侧"}
TERMS = (0, 1, 2, 3, 4, 5)


@dataclass(frozen=True)
class Drift:
    state: str
    reason: str
    model: dict | None
    summary: dict

    def gain(self, shape):
        if self.model is None:
            return None
        return _surface(self.model["coefficients"], tuple(self.model["terms"]), shape)

    def data(self):
        return {
            key: self.summary.get(key) for key in (
                "state", "reason", "gain_range", "evidence_pixels", "residual_rms",
                "sides", "missing_sides", "suspicious_tiles",
            )
        }


def disabled():
    summary = {
        "state": "compensation-off",
        "reason": "轮廓配方未启用背光漂移补偿，按原空场归一。",
    }
    return Drift("compensation-off", summary["reason"], None, summary)


def no_reference():
    summary = {
        "state": "compensation-unavailable",
        "reason": "尚未设置空场参考，无法估计背光漂移，本张按原配方处理。",
    }
    return Drift("compensation-unavailable", summary["reason"], None, summary)


def estimate(source, reference):
    """Fit a slowly varying illumination gain from confirmed background only."""
    if source.shape != reference.shape:
        return _unavailable("当前角度影像与空场画幅不一致，未采用漂移补偿。")
    height, width = source.shape
    if min(height, width) < 24:
        return _unavailable("画面过小，无法估计背光漂移。")
    tile = int(np.clip(min(height, width)/12, 16, 96))
    ratio = source/np.maximum(reference, 0.10)
    dark = source < MIN_SOURCE
    near_object = ndimage.binary_dilation(dark, iterations=2)
    evidence = (
        (reference >= MIN_REFERENCE) & (reference <= SATURATED)
        & (source >= MIN_SOURCE) & (source <= SATURATED)
        & (ratio >= RATIO_LOW) & (ratio <= RATIO_HIGH)
        & ~(reference < UNLIT_REFERENCE) & ~near_object
    )
    local = ndimage.uniform_filter(evidence.astype(np.float32), size=_odd(max(3, tile//2)))
    evidence &= local >= LOCAL_FRACTION
    if not np.any(evidence):
        return _unavailable(_empty_reason(reference, source, ratio))
    log_ratio = np.log(np.clip(ratio, 1e-4, None))
    columns = max(1, math.ceil(width/tile))
    rows = max(1, math.ceil(height/tile))
    xs = np.linspace(0, width, columns+1)
    ys = np.linspace(0, height, rows+1)
    splits = _row_splits(dark, ys, rows, width)
    tiles = _tile_estimates(log_ratio, evidence, xs, ys, splits)
    side_stats = _side_stats(tiles, height*width)
    missing = [name for name in ("left", "right") if not side_stats[name]["available"]]
    if missing:
        return _unavailable(
            _missing_reason(missing, source), tiles=_used_tiles(tiles),
            sides=side_stats, missing_sides=missing,
        )
    fit = _fit(tiles, width, height)
    if fit is None:
        return _unavailable(
            "可用背景不足以稳定估计亮度变化，未启用漂移补偿；本张按原配方处理。",
            tiles=_used_tiles(tiles),
            sides=side_stats,
        )
    coefficients, terms, residuals = fit
    for tile_entry, residual in zip([tile for tile in tiles if tile["used"]], residuals):
        deviation = abs(residual)
        if deviation > TILE_RESIDUAL:
            tile_entry["used"] = False
            tile_entry["rejected"] = "residual"
        elif deviation > TILE_SUSPICIOUS:
            tile_entry["anomaly"] = round(float(deviation), 5)
    side_stats = _side_stats(tiles, height*width)
    missing = [name for name in ("left", "right") if not side_stats[name]["available"]]
    if missing:
        return _unavailable(
            _missing_reason(missing, source), tiles=_used_tiles(tiles),
            sides=side_stats, missing_sides=missing,
        )
    fit = _fit(tiles, width, height, robust=False)
    if fit is None:
        return _unavailable("可用背景不足以稳定估计亮度变化，未启用漂移补偿。", sides=side_stats)
    coefficients, terms, residuals = fit
    rms = float(np.sqrt(np.mean(np.square(residuals)))) if residuals.size else 0.0
    if not math.isfinite(rms) or rms > FIT_LIMIT:
        return _unavailable(
            "背景亮度差偏离缓慢变化，未启用漂移补偿；本张按原配方处理。",
            tiles=_used_tiles(tiles), sides=side_stats,
        )
    probe = _surface(coefficients, terms, (65, 65))
    low, high = float(np.min(probe)), float(np.max(probe))
    if low < GAIN_LIMITS[0] or high > GAIN_LIMITS[1]:
        return _unavailable(
            "估计的亮度变化超出可用范围，未启用漂移补偿；本张按原配方处理。",
            tiles=_used_tiles(tiles), sides=side_stats,
        )
    summary = {
        "state": "compensated",
        "reason": "用两侧已确认背景估计缓慢亮度变化，本张按补偿后的归一图计算。",
        "gain_range": [round(low, 5), round(high, 5)],
        "evidence_pixels": int(np.count_nonzero(evidence)),
        "residual_rms": round(rms, 5),
        "sides": side_stats,
        "missing_sides": [],
        "suspicious_tiles": sum(1 for tile in tiles if tile.get("anomaly")),
        "tiles": _used_tiles(tiles),
        "model": {"terms": list(terms), "coefficients": [float(value) for value in coefficients]},
    }
    return Drift("compensated", summary["reason"], summary["model"], summary)


def _unavailable(reason, *, tiles=(), sides=None, missing_sides=()):
    # Fallback frames keep their original normalization; only the evidence map
    # shows which background tiles were usable and which side was missing.
    summary = {
        "state": "compensation-unavailable",
        "reason": reason,
        "gain_range": None,
        "sides": sides or {},
        "suspicious_tiles": sum(1 for tile in tiles if tile.get("anomaly")),
        "missing_sides": list(missing_sides),
        "tiles": list(tiles),
        "model": None,
    }
    return Drift("compensation-unavailable", reason, None, summary)


def _missing_reason(missing, source):
    phrase = "两侧都" if len(missing) == 2 else SIDE_NAMES[missing[0]]
    if float(np.mean(source >= SATURATED)) > 0.05:
        cause = "大片过曝，"
    elif float(np.mean(source < MIN_SOURCE)) > 0.30:
        cause = "大半画面变暗，"
    else:
        cause = ""
    return f"{phrase}{cause}没有可信的背景参照，未启用漂移补偿；本张按原配方处理。"


def _empty_reason(reference, source, ratio):
    if float(np.mean(source >= SATURATED)) > 0.05:
        return "当前角度图有大片过曝，背景参照不可信，未启用漂移补偿；本张按原配方处理。"
    if float(np.mean((reference >= MIN_REFERENCE) & (ratio < RATIO_LOW))) > 0.10:
        return "背景整体明显变暗，看不出缓慢亮度变化，未启用漂移补偿；本张按原配方处理。"
    return "当前角度影像没有可确认的背景区域，未启用漂移补偿；本张按原配方处理。"


def _odd(value):
    value = max(3, int(value))
    return value if value % 2 else value+1


def _row_splits(dark, ys, rows, width):
    splits = []
    for row in range(rows):
        band = dark[int(ys[row]):int(ys[row+1])]
        object_columns = np.flatnonzero(np.mean(band, axis=0) > 0.45) if band.size else np.array([], dtype=int)
        splits.append(float(object_columns[0]+object_columns[-1])/2 if object_columns.size else width/2)
    return splits


def _tile_estimates(log_ratio, evidence, xs, ys, splits):
    # The side a tile belongs to is decided per row: the object does not sit on
    # a fixed column, so a plain mid-frame split would mislabel one flank.
    tiles = []
    for row in range(len(ys)-1):
        for column in range(len(xs)-1):
            x0, x1 = int(xs[column]), int(xs[column+1])
            y0, y1 = int(ys[row]), int(ys[row+1])
            area = (x1-x0)*(y1-y0)
            if area <= 0:
                continue
            selected = evidence[y0:y1, x0:x1]
            count = int(np.count_nonzero(selected))
            fraction = count/area
            center = (x0+x1)/2
            entry = {
                "bounds": [x0, y0, x1-x0, y1-y0],
                "row": row, "column": column,
                "side": "left" if center < splits[row] else "right",
                "samples": count, "fraction": round(fraction, 4),
                "gain": None, "spread": None, "used": False,
            }
            if fraction >= TILE_FRACTION:
                values = log_ratio[y0:y1, x0:x1][selected]
                median = float(np.median(values))
                spread = float(np.quantile(values, 0.9)-np.quantile(values, 0.1))
                entry["gain"] = round(math.exp(median), 5)
                entry["spread"] = round(spread, 5)
                entry["used"] = (
                    math.isfinite(spread) and spread <= TILE_SPREAD
                    and RATIO_LOW*1.05 <= math.exp(median) <= RATIO_HIGH*0.95
                )
            tiles.append(entry)
    return tiles


def _used_tiles(tiles):
    return [tile for tile in tiles if tile["used"]]


def _side_stats(tiles, pixels):
    stats = {}
    minimum = max(240.0, MIN_SIDE_PIXELS*pixels)
    for name in ("left", "right"):
        chosen = [tile for tile in tiles if tile["used"] and tile["side"] == name]
        evidence = sum(tile["samples"] for tile in chosen)
        rows = len({tile["row"] for tile in chosen})
        stats[name] = {
            "tiles": len(chosen), "rows": rows, "evidence_pixels": evidence,
            "available": len(chosen) >= MIN_SIDE_TILES and rows >= MIN_SIDE_ROWS and evidence >= minimum,
        }
    return stats


def _fit(tiles, width, height, robust=True):
    chosen = [tile for tile in tiles if tile["used"]]
    if len(chosen) < 2:
        return None
    u = np.array([(tile["bounds"][0]+tile["bounds"][2]/2)/width*2-1 for tile in chosen])
    v = np.array([(tile["bounds"][1]+tile["bounds"][3]/2)/height*2-1 for tile in chosen])
    values = np.array([math.log(tile["gain"]) for tile in chosen])
    weights = np.array([math.sqrt(tile["samples"]) for tile in chosen])
    terms = list(TERMS)
    while len(terms) > 3:
        design = _design(terms, u, v)
        if np.linalg.matrix_rank(design*weights[:, None]) >= len(terms):
            break
        terms = terms[:-1]
    design = _design(terms, u, v)
    if np.linalg.matrix_rank(design*weights[:, None]) < len(terms):
        return None
    coefficients = _weighted(design, values, weights)
    if robust:
        for _ in range(3):
            residual = values-design@coefficients
            scale = 1.4826*float(np.median(np.abs(residual-np.median(residual))))+0.01
            share = np.minimum(1.0, 2.5*scale/np.maximum(np.abs(residual), 1e-9))
            coefficients = _weighted(design, values, weights*np.sqrt(share))
    residual = values-design@coefficients
    return coefficients, tuple(terms), residual


def _weighted(design, values, weights):
    scaled = design*weights[:, None]
    return np.linalg.lstsq(scaled, values*weights, rcond=None)[0]


def _design(terms, u, v):
    columns = []
    for term in terms:
        if term == 0:
            columns.append(np.ones_like(u))
        elif term == 1:
            columns.append(u)
        elif term == 2:
            columns.append(v)
        elif term == 3:
            columns.append(u*u)
        elif term == 4:
            columns.append(u*v)
        else:
            columns.append(v*v)
    return np.column_stack(columns)


def _surface(coefficients, terms, shape):
    height, width = shape
    u = (np.arange(width, dtype=np.float32)+0.5)/width*2-1
    v = (np.arange(height, dtype=np.float32)+0.5)/height*2-1
    field = np.zeros(shape, dtype=np.float32)
    for term, coefficient in zip(terms, coefficients):
        coefficient = float(coefficient)
        if term == 0:
            field += coefficient
        elif term == 1:
            field += coefficient*u[None, :]
        elif term == 2:
            field += coefficient*v[:, None]
        elif term == 3:
            field += coefficient*(u*u)[None, :]
        elif term == 4:
            field += coefficient*v[:, None]*u[None, :]
        else:
            field += coefficient*(v*v)[:, None]
    np.clip(field, -1.5, 1.5, out=field)
    return np.exp(field)


def overlay_png(summary, shape):
    """Translucent gain tint plus the tiles that carried background evidence."""
    height, width = shape
    factor = min(1.0, 960/max(width, height))
    canvas = np.zeros((max(1, round(height*factor)), max(1, round(width*factor)), 4), dtype=np.uint8)
    if not summary:
        return png_from_array(canvas)
    state = summary.get("state")
    if state == "compensated" and summary.get("model"):
        model = summary["model"]
        tint = _surface(model["coefficients"], tuple(model["terms"]), (65, 65))
        sample = np.empty(canvas.shape[:2], dtype=np.float32)
        rows = np.minimum((np.arange(canvas.shape[0])*65//max(1, canvas.shape[0])), 64)
        columns = np.minimum((np.arange(canvas.shape[1])*65//max(1, canvas.shape[1])), 64)
        sample = tint[rows][:, columns]
        logarithm = np.log(sample)
        strength = np.clip(np.abs(logarithm)/0.30, 0, 1)
        canvas[..., 0] = np.where(logarithm > 0, 235, 62)
        canvas[..., 1] = np.where(logarithm > 0, 150, 122)
        canvas[..., 2] = np.where(logarithm > 0, 66, 214)
        canvas[..., 3] = np.rint(28+150*strength).astype(np.uint8)
        colour = (72, 232, 172, 235)
    elif state == "compensation-unavailable":
        colour = (238, 176, 78, 235)
    else:
        return png_from_array(canvas)
    for tile in summary.get("tiles") or ():
        x, y, w, h = tile["bounds"]
        if tile.get("anomaly"):
            colour = (255, 214, 92, 240)
        _outline(
            canvas,
            round(x*factor), round(y*factor),
            max(1, round((x+w)*factor)), max(1, round((y+h)*factor)),
            colour, max(1, round(2*factor)),
        )
    return png_from_array(canvas)


def _outline(canvas, x0, y0, x1, y1, colour, line):
    height, width = canvas.shape[:2]
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    if x0 >= x1 or y0 >= y1:
        return
    line = max(1, line)
    canvas[y0:min(y1, y0+line), x0:x1] = colour
    canvas[max(y0, y1-line):y1, x0:x1] = colour
    canvas[y0:y1, x0:min(x1, x0+line)] = colour
    canvas[y0:y1, max(x0, x1-line):x1] = colour

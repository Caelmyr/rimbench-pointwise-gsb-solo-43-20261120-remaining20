from __future__ import annotations

import math

import numpy as np

from .domain import InspectionError, integer
from .images import png_from_array


def analyze(gray, grid=8, reference=None):
    grid = integer(grid, "背光网格密度", 3, 16)
    height, width = gray.shape
    horizontal = np.linspace(0, width, grid+1, dtype=int)
    vertical = np.linspace(0, height, grid+1, dtype=int)
    cells = []
    for row in range(grid):
        for col in range(grid):
            x0, x1 = int(horizontal[col]), int(horizontal[col+1])
            y0, y1 = int(vertical[row]), int(vertical[row+1])
            values = gray[y0:y1, x0:x1]
            baseline = reference[y0:y1, x0:x1] if reference is not None else None
            cells.append(_inspect_cell(values, baseline, [x0, y0, x1-x0, y1-y0], row, col))
    medians = [cell["median"] for cell in cells]
    largest = max(cells, key=lambda cell: cell["median"])
    smallest = min(cells, key=lambda cell: cell["median"])
    center = float(np.median(medians))
    return {
        "frame": [width, height], "grid": grid, "cells": cells,
        "summary": {
            "median": center, "uniformity": min(medians)/max(max(medians), 1e-6),
            "brightest_cell": [largest["row"], largest["column"]],
            "darkest_cell": [smallest["row"], smallest["column"]],
            "median_absolute_deviation": float(np.median(np.abs(np.asarray(medians)-center))),
            "dark_cells": sum(cell["classification"] == "dark" for cell in cells),
            "saturated_cells": sum(cell["classification"] == "saturated" for cell in cells),
            "dirty_cells": sum(cell["classification"] == "local-shadow" for cell in cells),
            "reference_drift": _reference_drift(cells),
        },
    }


def _inspect_cell(values, baseline, bounds, row, col):
    stride = max(1, math.ceil(math.sqrt(values.size/30_000)))
    pixels = values[::stride, ::stride]
    quantile = np.quantile(pixels, [0.05, 0.5, 0.95])
    dark = float(np.mean(pixels < 0.27))
    saturated = float(np.mean(pixels > 0.993))
    field = "even"
    if float(quantile[1]) < 0.43:
        field = "dark"
    elif saturated > 0.25:
        field = "saturated"
    elif dark > 0.03 or float(quantile[1]-quantile[0]) > 0.18:
        field = "local-shadow"
    delta = None
    if baseline is not None:
        other = baseline[::stride, ::stride]
        delta = float(np.median(pixels-other))
    return {
        "bounds": bounds, "row": row, "column": col,
        "p05": float(quantile[0]), "median": float(quantile[1]), "p95": float(quantile[2]),
        "dark_fraction": dark, "saturated_fraction": saturated,
        "classification": field, "reference_delta": delta,
    }


def _reference_drift(cells):
    candidates = [cell["reference_delta"] for cell in cells if cell["reference_delta"] is not None]
    if not candidates:
        return None
    return {
        "median": float(np.median(candidates)),
        "min": min(candidates), "max": max(candidates),
        "outside_five_percent_cells": sum(abs(value) > 0.05 for value in candidates),
    }


def heatmap(analysis, ramp="median"):
    if ramp not in ("median", "delta", "shadow"):
        raise InspectionError("背光叠层模式无效。")
    width, height = analysis["frame"]
    scale = min(1.0, 960/max(width, height))
    mapped = np.zeros((max(1, round(height*scale)), max(1, round(width*scale)), 4), dtype=np.uint8)
    for cell in analysis["cells"]:
        x, y, w, h = cell["bounds"]
        color = _color(cell, ramp)
        mapped[round(y*scale):round((y+h)*scale), round(x*scale):round((x+w)*scale)] = color
    return png_from_array(mapped)


def _color(cell, ramp):
    if ramp == "delta":
        difference = cell["reference_delta"]
        if difference is None:
            return [0, 0, 0, 0]
        strength = min(1.0, abs(difference)/0.2)
        return [230, 111, 60, round(40+176*strength)] if difference > 0 else [45, 126, 218, round(40+176*strength)]
    if ramp == "shadow":
        return {
            "dark": [207, 78, 69, 194],
            "local-shadow": [233, 167, 71, 171],
            "saturated": [109, 84, 190, 175],
            "even": [43, 173, 151, 82],
        }[cell["classification"]]
    light = cell["median"]
    return [round((1-light)*220+30), round(light*147+36), round(light*143+34), 153]

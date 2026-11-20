from __future__ import annotations

from dataclasses import replace
import math

import numpy as np

from .domain import Calibration, Gauge, Note, Project, View
from .images import ingest, png_from_array


W = 520
H = 382
AXIS_UPPER = (258.0, 50.0)
AXIS_LOWER = (263.0, 313.0)
SCALE = 5.5
ANGLES = (0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5, 202.5, 247.5, 292.5, 337.5)


def make_demo(kind):
    names = {
        "stepped": ("阶梯衬套 B-017", "转台 A", "合成演示"),
        "eccentric": ("偏心导套 C-002", "转台 B", "合成演示"),
    }
    product, station, operator = names[kind]
    project = Project.create(product, station, operator, sample=True)
    config = Calibration(*AXIS_UPPER, *AXIS_LOWER, SCALE*20, 20, 0.028)
    reference = _make_reference()
    background, encoded_ref = ingest(png_from_array(_as_u16(reference)))
    views = []
    assets = [(background, encoded_ref)]
    for index, angle in enumerate(ANGLES):
        dx = 1.8 * math.sin(index*1.7)
        dy = 1.2 * math.cos(index*1.15)
        fixture = index in ((10,) if kind == "stepped" else (6, 10))
        raw = _make_capture(reference, kind, angle, dx, dy, fixture, index)
        asset, png = ingest(png_from_array(_as_u16(raw)))
        view = View.create(asset.sha256, f"{angle:g}° 背光采集", angle, shift_x=dx, shift_y=dy)
        if fixture:
            view = replace(view, included=False)
        views.append(view)
        assets.append((asset, png))
    project = replace(
        project, width=W, height=H, reference=background.sha256, calibration=config,
        views=tuple(views), gauges=_gauges(kind),
        notes=(Note.create({"author": "样件资料", "text": _provenance(kind)}),),
    )
    return project, assets


def _make_reference():
    y, x = np.mgrid[:H, :W]
    lamp = 0.91 - 0.11*((x-W/2)/(W/2))**2 - 0.065*((y-H/2)/(H/2))**2
    band = 0.018*np.cos(x/40)*np.cos(y/70)
    return np.asarray(lamp+band, dtype=np.float32)


def _planes():
    dx = AXIS_LOWER[0]-AXIS_UPPER[0]
    dy = AXIS_LOWER[1]-AXIS_UPPER[1]
    length = math.hypot(dx, dy)
    return dx/length, dy/length


def _make_capture(reference, kind, angle, dx, dy, fixture, index):
    y, x = np.mgrid[:H, :W]
    ox = x-AXIS_LOWER[0]-dx
    oy = y-AXIS_LOWER[1]-dy
    u, v = _planes()
    rad = (ox*v-oy*u)/SCALE
    z = -(ox*u+oy*v)/SCALE
    low, high = _projection(kind, angle, z)
    radial = np.minimum((rad-low)*SCALE, (high-rad)*SCALE)
    axial = np.minimum((z-1.0)*SCALE, (44.8-z)*SCALE)
    coverage = np.clip((np.minimum(radial, axial)+0.9)/1.8, 0, 1)
    if fixture:
        intrusion = (x > W*0.56) & (x < W*0.61) & (y > H*0.68) & (y < H*0.96)
        coverage = np.maximum(coverage, intrusion*0.88)
    rng = np.random.default_rng(9043+index)
    gain = 0.97+0.012*math.sin(index)
    ambient = 0.046+0.011*np.cos(y/65)
    source = gain*reference*(1-0.93*coverage)+ambient*coverage
    source += rng.normal(0, 0.004, size=reference.shape)
    source[((x == 62) | (x == 70)) & (y >= 300) & (y <= 302)] -= 0.45
    return source


def _projection(kind, angle, z):
    theta = math.radians(angle)
    if kind == "stepped":
        radius = np.where(z < 12, 12.8, np.where(z > 34, 10.65, 15.45))
        radius += np.clip((z-13)*0.028, 0, 0.36)
        width = radius*np.sqrt((1.06*math.cos(theta))**2+(0.94*math.sin(theta))**2)
        shift = (0.55+z*0.014)*math.cos(theta)-0.35*math.sin(theta)
    else:
        taper = np.clip((z-15)/28, 0, 1)
        phase = theta-0.38
        width = (14.4-2.9*taper)*np.sqrt((1.11*math.cos(phase))**2+(0.88*math.sin(phase))**2)
        shift = (2.1+0.04*z)*math.cos(theta)-(0.75+0.016*z)*math.sin(theta)
    return shift-width, shift+width


def _as_u16(image):
    return np.rint(np.clip(image, 0, 1)*65535).astype(np.uint16)


def _gauges(kind):
    if kind == "stepped":
        settings = [
            {"title": "中段 X 向尺寸", "metric": "width", "z": 22, "direction": 0,
             "nominal": 33, "low": 32.2, "high": 33.8},
            {"title": "上口短轴", "metric": "min_feret", "z": 40, "direction": 0,
             "nominal": 20.5, "low": 20.1, "high": 21.1},
            {"title": "底部截面积", "metric": "area", "z": 6, "direction": 0,
             "nominal": 500, "low": 470, "high": 520},
        ]
    else:
        settings = [
            {"title": "导套基面 X 向", "metric": "width", "z": 10, "direction": 0,
             "nominal": 31.5, "low": 30, "high": 33},
            {"title": "顶端包络", "metric": "max_feret", "z": 42, "direction": 0,
             "nominal": 26, "low": 25.4, "high": 26.7},
        ]
    return tuple(Gauge.from_data(data) for data in settings)


def _provenance(kind):
    if kind == "stepped":
        return "合成阶梯衬套的固定背光投影。中央段较宽，一张带夹具侵入的角度默认关闭。"
    return "合成偏心导套的固定背光投影，带偏移与上段轻微收口。两张带夹具侵入的角度默认关闭。"

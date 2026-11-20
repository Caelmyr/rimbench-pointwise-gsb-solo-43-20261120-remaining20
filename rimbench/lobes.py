from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .calibration import z_positions
from .contour import PHASE_STEPS, measure_section
from .domain import InspectionError, integer, number, record
from .meter import shape_section


MIN_ORDER = 2
MAX_ORDER = 12
MAX_LEVELS = 47
MAX_OBSERVATIONS = 2000


@dataclass(frozen=True)
class LobeRequest:
    lower: float
    upper: float
    count: int
    order: int
    amplitude_floor: float

    @classmethod
    def parse(cls, data):
        data = record(data, "周向形状跟踪")
        lower = number(data.get("lower"), "起始高度", -100000, 100000)
        upper = number(data.get("upper"), "结束高度", -100000, 100000)
        count = integer(data.get("count", 21), "轴向截面数", 2, MAX_LEVELS)
        order = integer(data.get("order", 2), "形状周期数", MIN_ORDER, MAX_ORDER)
        floor = number(data.get("amplitude_floor", 0.03), "径向起伏最低幅值", 0.001, 10)
        return cls(lower, upper, count, order, floor)

    def data(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "order": self.order,
            "amplitude_floor": self.amplitude_floor,
        }


def wave_operator(signal):
    values = np.asarray(signal, dtype=float)
    centered = values - float(np.mean(values))
    return np.fft.rfft(centered)*(2/values.size)


def summarize_harmonics(signal, order):
    periodic = wave_operator(signal)
    target = periodic[order]
    period = 360/order
    strength = float(abs(target))
    angle = float((-math.degrees(np.angle(target))/order) % period)
    all_orders = np.abs(periodic[MIN_ORDER:MAX_ORDER+1])
    peak = int(np.argmax(all_orders))+MIN_ORDER
    residual = np.asarray(signal)-np.mean(signal)
    phase_grid = np.deg2rad(np.linspace(0, 360, len(signal), endpoint=False))
    expected = np.real(target*np.exp(1j*phase_grid*order))
    numerator = float(np.sum((residual-expected)**2))
    denominator = float(np.sum(residual**2))
    contribution = max(0.0, 1-numerator/denominator) if denominator > 1e-15 else 0.0
    return {
        "amplitude": strength,
        "phase": angle,
        "dominant_order": peak,
        "mode_amplitudes": [float(value) for value in all_orders],
        "mode_power_fraction": contribution,
    }


def section_mode(section, request):
    radius = None
    output = {
        "z": section["z"],
        "state": section["state"],
        "radius": None,
        "amplitude": None,
        "phase": None,
        "phase_continued": None,
        "mode_power_fraction": None,
        "mode_amplitudes": [],
        "dominant_order": None,
        "normal_gap": section["normal_coverage"]["gap"],
    }
    if section["state"] != "measured":
        return output
    circle = measure_section(section)
    if circle is None:
        return {**output, "state": "no-fit"}
    radius = circle.radius
    minimum_samples = request.order*3
    if output["normal_gap"] > 360/minimum_samples:
        return {**output, "state": "undersampled", "radius": radius}
    orbit = circle.boundary.ray_profile(circle.center, count=PHASE_STEPS)
    if not orbit or any(point["radius"] is None for point in orbit):
        return {**output, "state": "no-fit"}
    modes = summarize_harmonics([point["radius"] for point in orbit], request.order)
    state = "measured"
    if modes["amplitude"] < request.amplitude_floor:
        modes["phase"] = None
        state = "phase-weak"
    return {**output, **modes, "state": state, "radius": radius}


def phase_and_spans(rows, order):
    period = 360/order
    tracks = []
    previous = None
    for row in rows:
        phase = row["phase"]
        if phase is None:
            previous = None
            continue
        if previous is None:
            row["phase_continued"] = phase
            tracks.append([row])
        else:
            step = (phase-previous+period/2) % period-period/2
            row["phase_continued"] = previous+step
            tracks[-1].append(row)
        previous = row["phase_continued"]
    return [
        {
            "first_z": group[0]["z"],
            "last_z": group[-1]["z"],
            "count": len(group),
            "phase_change": group[-1]["phase_continued"]-group[0]["phase_continued"] if len(group) > 1 else None,
        }
        for group in tracks
    ]


def analyze_lobes(meter, request):
    heights = z_positions(request.lower, request.upper, request.count, maximum=MAX_LEVELS)
    active = sum(view.included for view in meter.project.views)
    if len(heights)*active > MAX_OBSERVATIONS:
        raise InspectionError("周向形状跟踪的角度和截面组合过多。", code="lobe-observation-limit")
    evidence = meter.observe_grid(heights)
    rows = [
        section_mode(shape_section(meter.project, z, 0, readings, detailed=True), request)
        for z, readings in zip(heights, evidence)
    ]
    spans = phase_and_spans(rows, request.order)
    measured = [row for row in rows if row["amplitude"] is not None]
    strongest = max(measured, key=lambda row: row["amplitude"]) if measured else None
    return {
        "project_id": meter.project.id,
        "revision": meter.project.revision,
        "parameters": request.data(),
        "summary": {
            "total": len(rows),
            "measured": len(measured),
            "with_phase": sum(row["phase"] is not None for row in rows),
            "phase_period": 360/request.order,
            "phase_spans": spans,
            "max_amplitude": {"z": strongest["z"], "value": strongest["amplitude"]} if strongest else None,
        },
        "rows": rows,
    }

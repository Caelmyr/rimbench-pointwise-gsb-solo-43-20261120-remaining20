from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading

import numpy as np

from .calibration import AxisFrame, apply_scale_envelope, effective_reference_length
from .domain import InspectionError, Project, View
from .geometry import (
    active_support, bounded_polygon, center, circular_gap, clash_witness,
    metrics, polygon_list, support_planes,
)
from .images import gray_from_png, reference_faults
from .silhouette import Silhouette, decide, extract, observation


class ImageCache:
    def __init__(self, limit=112_000_000):
        self.entries = OrderedDict()
        self.limit = limit
        self.size = 0
        self.lock = threading.RLock()

    def acquire(self, key, build, measure):
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][0]
        value = build()
        weight = int(measure(value))
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][0]
            if weight <= self.limit:
                while self.entries and self.size+weight > self.limit:
                    _, (_, drop) = self.entries.popitem(last=False)
                    self.size -= drop
                self.entries[key] = (value, weight)
                self.size += weight
        return value


@dataclass
class Meter:
    project: Project
    load: object
    cache: ImageCache

    def axis(self):
        p = self.project
        if not p.calibration:
            raise InspectionError("请先添加计量影像并设置轴和比例。", code="no-geometry")
        return AxisFrame.from_calibration(p.calibration, p.width, p.height)

    def gray(self, asset):
        return self.cache.acquire(
            ("gray", asset), lambda: gray_from_png(self.load(asset)[1]), lambda value: value.nbytes,
        )

    def view_mask(self, view):
        recipe = self.project.segmentation
        reference = self.project.reference

        def build():
            source = self.gray(view.asset)
            flat = self.gray(reference) if reference else None
            decision = decide(source, recipe, view.exposure, flat)
            return extract(source, recipe, view.exposure, flat, decision)

        return self.cache.acquire(
            ("mask", view.asset, reference, view.exposure, recipe),
            build,
            lambda value: value.mask.nbytes+value.threshold_image.nbytes,
        )

    def axis_summary(self):
        frame = self.axis()
        active = [view for view in self.project.views if view.included]
        limits = frame.axial_physical_range(active, self.project.segmentation.inset)
        return {
            "frame": frame.geometry(),
            "range": list(limits) if limits else None,
            "uncertainty": effective_reference_length(
                self.project.calibration, self.project.segmentation.ambiguity_px,
            ),
        }

    def inspect_view(self, view: View, z):
        shape = self.view_mask(view)
        frame = self.axis()
        return {
            "view": view.data(), "image": shape.summary,
            "axis": frame.geometry(view),
            "observation": observation(shape, frame, self.project.segmentation, view, z),
            "asset": self.load(view.asset)[0].data(),
        }

    def inspect_reference(self):
        asset = self.project.reference
        if not asset:
            return None
        stats, warnings = reference_faults(self.gray(asset))
        return {"asset": self.load(asset)[0].data(), "stats": stats, "warnings": warnings}

    def observations_at(self, z):
        return self.observe_grid((z,))[0]

    def observe_grid(self, positions):
        axis = self.axis()
        settings = self.project.segmentation
        results = [[] for _ in positions]
        for view in self.project.views:
            if not view.included:
                for z, group in zip(positions, results):
                    group.append({"view_id": view.id, "angle": view.angle, "title": view.title,
                                  "z": z, "state": "excluded", "reason": "本角度未参与截面。"})
                continue
            silhouette = self.view_mask(view)
            for z, group in zip(positions, results):
                group.append(observation(silhouette, axis, settings, view, z))
        return results

    def section(self, z, direction=0):
        return shape_section(
            self.project, z, direction, self.observations_at(z), detailed=True,
        )


def shape_section(project, z, direction, observations, *, detailed):
    planes = support_planes(observations)
    gap = circular_gap((plane.angle for plane in planes))
    used = sum(obs["state"] == "usable" for obs in observations)
    normal = None
    inside = None
    outside = None
    diagnostic = None
    if used == 0:
        phase = "unseen"
        detail = "这一高度没有有效的测量角度。"
    elif gap["gap"] >= 180-1e-6:
        phase = "unbounded"
        detail = "有效支撑未闭合，截面尺寸暂不可用。"
    else:
        normal = bounded_polygon(planes)
        outside = bounded_polygon(planes, envelope=1)
        if normal is None:
            if outside is None:
                phase = "conflict"
                detail = "不同角度的轮廓互相冲突。"
            else:
                phase = "borderline"
                detail = "标称截面无法闭合，只有放宽端点误差后的外包络。"
            diagnostic = clash_witness(planes)
        else:
            phase = "measured"
            detail = "已求得该高度的凸轮廓截面。"
            inside = bounded_polygon(planes, envelope=-1)
    nominal = metrics(normal, direction)
    inner_metrics = metrics(inside, direction)
    outer_metrics = metrics(outside, direction)
    limits = _metric_ranges(project.calibration, nominal, inner_metrics, outer_metrics)
    detected = {
        "z": z,
        "direction": direction,
        "state": phase,
        "detail": detail,
        "usable_views": used,
        "normal_coverage": gap,
        "metrics": nominal,
        "intervals": limits,
        "centroid": np.round(center(normal), 7).tolist() if normal is not None else None,
    }
    if detailed:
        detected.update(
            polygons={"nominal": polygon_list(normal), "inner": polygon_list(inside), "outer": polygon_list(outside)},
            observations=observations,
            active_support=active_support(normal, planes),
            diagnostic=diagnostic,
        )
    return detected


def _metric_ranges(calibration, nominal, inner, outer):
    if not nominal or not outer:
        return None
    out = {}
    relative = calibration.ruler_uncertainty/calibration.scale_mm
    for name, value in nominal.items():
        if name == "roundness":
            out[name] = None
            continue
        endpoints = [inner[name] if inner else 0.0, value, outer[name]]
        out[name] = apply_scale_envelope(name, endpoints, relative)
    return out


def healthy_range(section, metric):
    values = section.get("intervals") or {}
    bounds = values.get(metric)
    return bounds if bounds and all(math.isfinite(v) for v in bounds) else None

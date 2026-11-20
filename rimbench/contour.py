from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy.optimize import least_squares

from .geometry import polygon_list


MIN_PERIMETER = 1e-7
PHASE_STEPS = 144
QUADRATURE = np.array([
    0.06943184420297371,
    0.33000947820757187,
    0.6699905217924281,
    0.9305681557970262,
])
QUADRATURE_WEIGHTS = np.array([
    0.17392742256872692,
    0.32607257743127307,
    0.32607257743127307,
    0.17392742256872692,
])


@dataclass(frozen=True)
class Boundary:
    vertices: np.ndarray
    segments: np.ndarray
    lengths: np.ndarray
    perimeter: float

    @classmethod
    def from_section(cls, section):
        raw = section["polygons"]["nominal"]
        if raw is None or len(raw) < 3:
            return None
        vertices = np.asarray(raw, dtype=float)
        segments = np.roll(vertices, -1, axis=0)-vertices
        lengths = np.linalg.norm(segments, axis=1)
        good = lengths > MIN_PERIMETER
        if np.count_nonzero(good) < 3:
            return None
        vertices = vertices[good]
        segments = segments[good]
        lengths = lengths[good]
        perimeter = float(np.sum(lengths))
        return cls(vertices, segments, lengths, perimeter)

    def quadrature(self):
        points = self.vertices[:, None, :] + self.segments[:, None, :] * QUADRATURE[None, :, None]
        weights = self.lengths[:, None] * QUADRATURE_WEIGHTS[None, :] / self.perimeter
        return points.reshape(-1, 2), weights.ravel()

    def contains(self, point):
        left = cross2(self.segments, np.asarray(point)-self.vertices)
        tolerance = MIN_PERIMETER*self.perimeter
        return bool(np.all(left >= -tolerance) or np.all(left <= tolerance))

    def extremes(self, point):
        offset = self.vertices-np.asarray(point, dtype=float)
        square = self.lengths*self.lengths
        share = -np.sum(offset*self.segments, axis=1)/square
        clamped = np.clip(share, 0, 1)
        nearest = self.vertices + self.segments*clamped[:, None]
        distances = np.linalg.norm(nearest-point, axis=1)
        farthest = np.linalg.norm(offset, axis=1)
        lo = int(np.argmin(distances))
        hi = int(np.argmax(farthest))
        return RadialExtremes(
            float(distances[lo]),
            float(farthest[hi]),
            nearest[lo].tolist(),
            self.vertices[hi].tolist(),
        )

    def ray_profile(self, point, phase=0.0, count=PHASE_STEPS):
        if not self.contains(point):
            return None
        angles = np.linspace(phase, phase+360, count, endpoint=False)
        radians = np.deg2rad(angles)
        rays = np.column_stack((np.cos(radians), np.sin(radians)))
        starts = self.vertices-np.asarray(point, dtype=float)
        lines = np.broadcast_to(self.segments, (count, len(starts), 2))
        directions = rays[:, None, :]
        denominator = cross2(directions, lines)
        intersects = np.abs(denominator) > MIN_PERIMETER
        safe = np.where(intersects, denominator, 1)
        along = cross2(starts, self.segments)/safe
        segment = cross2(starts, directions)/safe
        forward = intersects & (along > MIN_PERIMETER)
        within = (segment >= -MIN_PERIMETER) & (segment <= 1+MIN_PERIMETER)
        distances = np.min(np.where(forward & within, along, np.inf), axis=1)
        return [
            {"angle": float(angle % 360), "radius": float(radius) if math.isfinite(radius) else None}
            for angle, radius in zip(angles, distances)
        ]


@dataclass(frozen=True)
class RadialExtremes:
    minimum: float
    maximum: float
    minimum_point: list
    maximum_point: list

    def data(self):
        return {
            "minimum_radius": self.minimum,
            "maximum_radius": self.maximum,
            "spread": self.maximum-self.minimum,
            "minimum_point": self.minimum_point,
            "maximum_point": self.maximum_point,
        }


@dataclass(frozen=True)
class SectionCircle:
    center: np.ndarray
    radius: float
    rms: float
    boundary: Boundary

    def data(self):
        return {
            "center": self.center.tolist(),
            "radius": self.radius,
            "diameter": self.radius*2,
            "rms": self.rms,
            "datum": self.boundary.extremes(self.center).data(),
            "polygon": polygon_list(self.boundary.vertices),
        }


def cross2(a, b):
    return a[..., 0]*b[..., 1]-a[..., 1]*b[..., 0]


def fit_circle(boundary):
    points, weights = boundary.quadrature()
    origin = np.average(points, weights=weights, axis=0)
    centered = points-origin
    scale = float(np.sqrt(np.average(np.sum(centered**2, axis=1), weights=weights)))
    if scale <= MIN_PERIMETER:
        return None
    normalized = centered/scale
    a, b = normalized.T
    design = np.column_stack((2*a, 2*b, np.ones(points.shape[0])))
    sqrt_weight = np.sqrt(weights)
    fit, _, rank, singular = np.linalg.lstsq(
        design*sqrt_weight[:, None],
        (a*a+b*b)*sqrt_weight,
        rcond=None,
    )
    if rank < 3 or singular[-1] < singular[0]*1e-5:
        return None

    def residual(values):
        return (np.linalg.norm(normalized-values[:2], axis=1)-values[2])*sqrt_weight

    offset = np.clip(fit[:2], -2.9, 2.9)
    initial_radius = np.average(np.linalg.norm(normalized-offset, axis=1), weights=weights)
    result = least_squares(
        residual,
        [offset[0], offset[1], min(initial_radius, 4.9)],
        bounds=([-3, -3, 1e-7], [3, 3, 5]),
        max_nfev=45,
    )
    if not result.success:
        return None
    center = origin+result.x[:2]*scale
    if not boundary.contains(center):
        return None
    radius = float(result.x[2]*scale)
    rms = float(np.linalg.norm(result.fun)*scale)
    return SectionCircle(center, radius, rms, boundary)


def measure_section(section):
    boundary = Boundary.from_section(section)
    if boundary is None:
        return None
    return fit_circle(boundary)


def circle_change(circle, center):
    offset = circle.center-center
    if not circle.boundary.contains(center):
        return {
            "state": "datum-outside",
            "offset": offset.tolist(),
            "distance": float(np.linalg.norm(offset)),
            "radii": None,
        }
    return {
        "state": "available",
        "offset": offset.tolist(),
        "distance": float(np.linalg.norm(offset)),
        "radii": circle.boundary.extremes(center).data(),
    }


def radial_summary(circle, profile, datum_center):
    samples = []
    datum_profile = circle.boundary.ray_profile(datum_center)
    for index, sample in enumerate(profile):
        radius = sample["radius"]
        datum_radius = datum_profile[index]["radius"] if datum_profile else None
        samples.append({
            "angle": sample["angle"],
            "radius": radius,
            "circle_residual": radius-circle.radius if radius is not None else None,
            "datum_radius": datum_radius,
            "datum_residual": datum_radius-circle.radius if datum_radius is not None else None,
        })
    return samples


def section_form(meter, z, datum_x=0, datum_y=0):
    section = meter.section(z)
    state = section["state"]
    datum = np.asarray([datum_x, datum_y], dtype=float)
    result = {
        "revision": meter.project.revision,
        "z": z,
        "state": state,
        "detail": section["detail"],
        "requested_datum": datum.tolist(),
        "origin": None,
        "fitted_circle": None,
        "polar": [],
        "usable_views": section["usable_views"],
        "normal_coverage": section["normal_coverage"],
    }
    if state != "measured":
        return result
    circle = measure_section(section)
    if circle is None:
        result.update(state="no-fit", detail="该截面不能稳定拟合基准圆。")
        return result
    shape = circle.boundary.ray_profile(circle.center)
    if shape is None:
        result.update(state="no-fit", detail="不能在拟合圆心周围取得连续极坐标轮廓。")
        return result
    result.update(
        origin=circle_change(circle, datum),
        fitted_circle=circle.data(),
        polar=radial_summary(circle, shape, datum),
        detail="从重建凸轮廓计算圆拟合、径向残差与指定圆心的径向极差。",
    )
    return result

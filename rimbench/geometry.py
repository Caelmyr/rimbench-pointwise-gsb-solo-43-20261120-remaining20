from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math

import numpy as np
from scipy.spatial import ConvexHull, QhullError


EQUATIONS_EPSILON = 1e-8
METRIC_UNITS = {
    "width": "mm", "max_feret": "mm", "min_feret": "mm",
    "equivalent_diameter": "mm", "area": "mm²", "roundness": "",
}
METRIC_LABELS = {
    "width": "指定方向宽度", "max_feret": "最大卡尺径", "min_feret": "最小卡尺径",
    "equivalent_diameter": "等面积直径", "area": "截面积", "roundness": "圆度",
}


@dataclass(frozen=True)
class HalfPlane:
    nx: float
    ny: float
    offset: float
    error: float
    view_id: str
    side: str
    angle: float

    def coefficient(self, envelope=0):
        return self.nx, self.ny, self.offset+envelope*self.error

    def for_data(self):
        return {
            "normal": [self.nx, self.ny],
            "offset": self.offset,
            "uncertainty": self.error,
            "view_id": self.view_id,
            "side": self.side,
            "angle": self.angle,
        }


def normal(angle):
    radians = math.radians(angle)
    return math.cos(radians), math.sin(radians)


def support_planes(observations):
    planes = []
    for observed in observations:
        if observed["state"] != "usable":
            continue
        angle = observed["angle"]
        nx, ny = normal(angle)
        uncertainty = observed["edge_error_mm"]
        key = observed["view_id"]
        planes.extend((
            HalfPlane(nx, ny, observed["upper"], uncertainty, key, "right", angle%360),
            HalfPlane(-nx, -ny, -observed["lower"], uncertainty, key, "left", (angle+180)%360),
        ))
    return planes


def circular_gap(angles, period=360):
    ordered = sorted(set(round(x % period, 6) % period for x in angles))
    if not ordered:
        return {"gap": float(period), "from": None, "to": None, "distinct": 0}
    pairs = zip(ordered, [*ordered[1:], ordered[0]+period])
    first, second = max(pairs, key=lambda pair: pair[1]-pair[0])
    return {"gap": round(second-first, 6), "from": first, "to": second%period, "distinct": len(ordered)}


def bounded_polygon(planes, envelope=0):
    if len(planes) < 3 or circular_gap((plane.angle for plane in planes))["gap"] >= 180-1e-6:
        return None
    # Centring the initial bounding square on an actual pair intersection improves
    # numerical precision for parts far from the calibrated rotation axis.
    anchor, bound = _anchor_and_bound(planes, envelope)
    if anchor is None:
        return None
    x, y = anchor
    polygon = np.array(
        [[x-bound, y-bound], [x+bound, y-bound], [x+bound, y+bound], [x-bound, y+bound]],
        dtype=np.float64,
    )
    for plane in planes:
        polygon = _clip(polygon, plane.coefficient(envelope))
        if len(polygon) < 3:
            return None
    polygon = _unique_cyclic(polygon)
    if len(polygon) < 3 or abs(signed_area(polygon)) <= EQUATIONS_EPSILON:
        return None
    return polygon


def _anchor_and_bound(planes, envelope):
    anchor = None
    radius = 0.0
    pairs = list(combinations(planes, 2))
    if not pairs:
        return anchor, radius
    coefficients = [plane.coefficient(envelope) for plane in planes]
    matrix = np.array(coefficients, dtype=np.float64)
    upper, lower = np.triu_indices(len(matrix), 1)
    p, q = matrix[upper], matrix[lower]
    determinant = p[:, 0]*q[:, 1]-p[:, 1]*q[:, 0]
    keep = np.abs(determinant) > 1e-10
    if np.any(keep):
        p, q, determinant = p[keep], q[keep], determinant[keep]
        xs = (p[:, 2]*q[:, 1]-p[:, 1]*q[:, 2])/determinant
        ys = (p[:, 0]*q[:, 2]-p[:, 2]*q[:, 0])/determinant
        left, right = min(xs), max(xs)
        bottom, top = min(ys), max(ys)
        anchor = ((left+right)/2, (bottom+top)/2)
        radius = max(right-left, top-bottom)/2+1.0
    return anchor, radius


def _clip(vertices, coefficient):
    x, y, offset = coefficient
    lags = vertices @ np.array([x, y])-offset
    result = []
    for index, stop in enumerate(vertices):
        start = vertices[index-1]
        begin = lags[index-1]
        end = lags[index]
        started_inside = begin <= EQUATIONS_EPSILON
        ends_inside = end <= EQUATIONS_EPSILON
        if started_inside != ends_inside:
            fraction = max(0.0, min(1.0, begin/(begin-end)))
            result.append(start+fraction*(stop-start))
        if ends_inside:
            result.append(stop)
    return np.asarray(result, dtype=np.float64)


def _unique_cyclic(vertices):
    if len(vertices) < 2:
        return vertices
    gaps = np.linalg.norm(vertices-np.roll(vertices, 1, axis=0), axis=1)
    return vertices[gaps > EQUATIONS_EPSILON]


def signed_area(polygon):
    points = polygon-polygon[0]
    shifted = np.roll(points, -1, axis=0)
    return float(np.sum(points[:, 0]*shifted[:, 1]-points[:, 1]*shifted[:, 0])*0.5)


def perimeter(polygon):
    return float(np.linalg.norm(np.roll(polygon, -1, axis=0)-polygon, axis=1).sum())


def center(polygon):
    origin = polygon[0]
    points = polygon-origin
    rotated = np.roll(points, -1, axis=0)
    weights = points[:, 0]*rotated[:, 1]-points[:, 1]*rotated[:, 0]
    return np.sum((points+rotated)*weights[:, None], axis=0)/(3*weights.sum())+origin


def projected_width(polygon, direction):
    coordinates = polygon @ np.asarray(normal(direction))
    return float(np.max(coordinates)-np.min(coordinates))


def feret_diameters(polygon):
    vectors = np.roll(polygon, -1, axis=0)-polygon
    norms = np.linalg.norm(vectors, axis=1)
    edge_normals = np.stack((vectors[:, 1], -vectors[:, 0]), axis=1)
    valid = norms > EQUATIONS_EPSILON
    axes = edge_normals[valid] / norms[valid, None]
    projections = polygon @ axes.T
    widths = projections.max(axis=0)-projections.min(axis=0)
    distances = polygon[:, None, :] - polygon[None, :, :]
    diameter = float(np.sqrt(np.max(np.sum(distances**2, axis=2))))
    return float(widths.min()), diameter


def metrics(polygon, direction):
    if polygon is None:
        return None
    area = abs(signed_area(polygon))
    boundary = perimeter(polygon)
    minimum, maximum = feret_diameters(polygon)
    values = {
        "width": projected_width(polygon, direction),
        "max_feret": maximum,
        "min_feret": minimum,
        "equivalent_diameter": 2*math.sqrt(area/math.pi),
        "area": area,
        "roundness": min(1.0, 4*math.pi*area/(boundary*boundary)),
    }
    return values


def active_support(polygon, planes):
    if polygon is None:
        return []
    result = []
    for plane in planes:
        signed = (polygon @ np.array((plane.nx, plane.ny)))-plane.offset
        count = int(np.count_nonzero(np.abs(signed) < 1e-6))
        if count >= 2:
            result.append(plane.for_data())
    return result


def polygon_list(polygon):
    if polygon is None:
        return []
    return np.round(polygon, 7).tolist()


def clash_witness(planes):
    """Return the largest conflict between effectively parallel opposing supports."""
    worst = None
    for first, second in combinations(planes, 2):
        if first.nx*second.nx+first.ny*second.ny > -0.999999999:
            continue
        overlap = first.offset+second.offset
        if overlap >= 0 or (worst and overlap >= worst["overlap"]):
            continue
        worst = {"first": first.for_data(), "second": second.for_data(), "overlap": overlap}
    return worst


def circular_coverage(views):
    active = [view for view in views if view.included]
    gap = circular_gap((view.angle for view in active), period=180)
    repeated = []
    for first, second in combinations(active, 2):
        raw = abs((first.angle-second.angle) % 180)
        if min(raw, 180-raw) < 0.5:
            repeated.append([first.id, second.id])
    return {"angle_coverage": gap, "overlap_pairs": repeated, "included_count": len(active)}

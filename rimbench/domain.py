from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
from typing import Any
from uuid import uuid4


MAX_PIXELS = 6_000_000
MAX_IMAGE_BYTES = 18 * 1024 * 1024
MAX_VIEWS = 48
MAX_GAUGES = 24
MAX_NOTES = 200
MAX_SNAPSHOTS = 40
MAX_PROJECTS = 80


class InspectionError(ValueError):
    def __init__(self, message, *, status=400, code="invalid-input", details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid4().hex


def valid_id(value, label="ID"):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise InspectionError(f"{label}无效。")
    return value


def record(value, label="数据"):
    if not isinstance(value, dict):
        raise InspectionError(f"{label}必须是对象。")
    return value


def entries(value, label, limit):
    if not isinstance(value, list):
        raise InspectionError(f"{label}必须是列表。")
    if len(value) > limit:
        raise InspectionError(f"{label}最多 {limit} 项。")
    return value


def label(value, field, limit=80, *, empty=False):
    if not isinstance(value, str):
        raise InspectionError(f"{field}必须是文字。")
    value = value.strip()
    if (not value and not empty) or len(value) > limit:
        raise InspectionError(f"{field}长度需为 {'0' if empty else '1'}–{limit} 字符。")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise InspectionError(f"{field}含无法显示的控制字符。")
    return value


def number(value, field, minimum, maximum):
    if isinstance(value, bool):
        raise InspectionError(f"{field}需要数字。")
    try:
        parsed = float(value)
    except (TypeError, OverflowError, ValueError) as exc:
        raise InspectionError(f"{field}需要数字。") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise InspectionError(f"{field}需在 {minimum:g}–{maximum:g} 范围内。")
    return parsed


def integer(value, field, minimum, maximum):
    parsed = number(value, field, minimum, maximum)
    if not parsed.is_integer():
        raise InspectionError(f"{field}需要整数。")
    return int(parsed)


def flag(value, field):
    if not isinstance(value, bool):
        raise InspectionError(f"{field}必须为开或关。")
    return value


def choice(value, field, options):
    if value not in options:
        raise InspectionError(f"{field}选项无效。")
    return value


def degrees(value):
    return number(value, "转台角度", -36000, 36000) % 360.0


@dataclass(frozen=True)
class Asset:
    sha256: str
    width: int
    height: int
    pixels: str
    byte_size: int
    stored_at: str

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class View:
    id: str
    asset: str
    title: str
    angle: float
    included: bool
    shift_x: float
    shift_y: float
    exposure: float

    @classmethod
    def create(cls, asset, title, angle, *, shift_x=0, shift_y=0):
        return cls(
            new_id(), asset, label(title, "影像名称", 120), degrees(angle),
            True, number(shift_x, "横向位移", -1000, 1000),
            number(shift_y, "纵向位移", -1000, 1000), 1.0,
        )

    @classmethod
    def from_data(cls, source):
        return cls(
            valid_id(source["id"], "角度影像ID"), source["asset"],
            label(source["title"], "影像名称", 120), degrees(source["angle"]),
            flag(source["included"], "参与重建"),
            number(source["shift_x"], "横向位移", -1000, 1000),
            number(source["shift_y"], "纵向位移", -1000, 1000),
            number(source["exposure"], "曝光补偿", 0.35, 3),
        )

    def revised(self, partial):
        merged = self.data()
        for field in ("title", "angle", "included", "shift_x", "shift_y", "exposure"):
            if field in partial:
                merged[field] = partial[field]
        return self.from_data(merged)

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class Segmentation:
    cutoff: float = 0.59
    min_area: int = 36
    close_radius: int = 2
    ambiguity_px: float = 0.8
    min_span: int = 4
    inset: int = 2
    compensate_drift: bool = False

    @classmethod
    def from_data(cls, data):
        d = {**asdict(cls()), **record(data, "轮廓配方")}
        return cls(
            number(d["cutoff"], "暗部阈值", 0.09, 0.94),
            integer(d["min_area"], "最小主体像素数", 1, 10000),
            integer(d["close_radius"], "轮廓闭合半径", 0, 8),
            number(d["ambiguity_px"], "端点误差像素", 0.1, 6),
            integer(d["min_span"], "最短连续轮廓宽度", 2, 120),
            integer(d["inset"], "画面保护边距", 1, 25),
            flag(d["compensate_drift"], "背光漂移补偿"),
        )

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class Calibration:
    top_x: float
    top_y: float
    bottom_x: float
    bottom_y: float
    scale_px: float
    scale_mm: float
    ruler_uncertainty: float

    @classmethod
    def default(cls, width, height):
        return cls((width-1)/2, height*0.2, (width-1)/2, height*0.8, 30.0, 10.0, 0.02)

    @classmethod
    def from_data(cls, data, width, height):
        data = record(data, "样件轴标定")
        result = cls(
            number(data.get("top_x"), "轴上点横坐标", 0, width-1),
            number(data.get("top_y"), "轴上点纵坐标", 0, height-1),
            number(data.get("bottom_x"), "轴下点横坐标", 0, width-1),
            number(data.get("bottom_y"), "轴下点纵坐标", 0, height-1),
            number(data.get("scale_px"), "标尺像素", 2, max(width, height)*2),
            number(data.get("scale_mm"), "标尺毫米", 0.01, 10000),
            number(data.get("ruler_uncertainty", 0.02), "标尺误差", 0, 100),
        )
        if result.bottom_y - result.top_y < 4:
            raise InspectionError("轴下点必须比轴上点至少低 4 像素。")
        if abs(result.slope) > 0.35:
            raise InspectionError("轴倾角过大，请摆正相机后重新标定。")
        if result.ruler_uncertainty >= result.scale_mm:
            raise InspectionError("标尺误差必须小于标尺长度。")
        return result

    @property
    def px_per_mm(self):
        return self.scale_px / self.scale_mm

    @property
    def slope(self):
        return (self.bottom_x-self.top_x)/(self.bottom_y-self.top_y)

    def data(self):
        return asdict(self)


NORMAL_METRICS = ("width", "max_feret", "min_feret", "equivalent_diameter", "area", "roundness")


@dataclass(frozen=True)
class Gauge:
    id: str
    title: str
    metric: str
    z: float
    direction: float
    nominal: float
    low: float
    high: float

    @classmethod
    def from_data(cls, data, identity=None):
        data = record(data, "公差量规")
        out = cls(
            identity or new_id(), label(data.get("title"), "量规名称"),
            choice(data.get("metric"), "测量项目", NORMAL_METRICS),
            number(data.get("z"), "截面高度", -100000, 100000),
            degrees(data.get("direction", 0)),
            number(data.get("nominal"), "标称值", 0, 1e12),
            number(data.get("low"), "下限", 0, 1e12),
            number(data.get("high"), "上限", 0, 1e12),
        )
        if not out.low <= out.nominal <= out.high:
            raise InspectionError("下限、标称值和上限必须按顺序排列。")
        return out

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class Note:
    id: str
    author: str
    text: str
    kind: str
    view: str | None
    z: float | None
    created_at: str

    @classmethod
    def create(cls, data):
        data = record(data, "记录")
        view = data.get("view")
        height = data.get("z")
        return cls(
            new_id(), label(data.get("author", "检验员"), "记录者", 32),
            label(data.get("text"), "记录内容", 1000),
            choice(data.get("kind", "observation"), "记录类型", ("observation", "review", "hold")),
            valid_id(view, "影像ID") if view is not None else None,
            number(height, "截面高度", -100000, 100000) if height is not None else None,
            utc_now(),
        )

    def data(self):
        return asdict(self)


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    station: str
    operator: str
    created_at: str
    updated_at: str
    revision: int
    width: int
    height: int
    calibration: Calibration | None
    segmentation: Segmentation
    reference: str | None
    views: tuple[View, ...]
    gauges: tuple[Gauge, ...]
    notes: tuple[Note, ...]
    sample: bool = False

    @classmethod
    def create(cls, title, station="", operator="", *, sample=False):
        stamp = utc_now()
        return cls(
            new_id(), label(title, "样件名称"), label(station, "测站", empty=True),
            label(operator, "检验人", empty=True), stamp, stamp,
            0, 0, 0, None, Segmentation(), None, (), (), (), sample,
        )

    @classmethod
    def from_data(cls, data):
        width = data["width"]
        height = data["height"]
        calibration = data.get("calibration")
        return cls(
            data["id"], data["title"], data["station"], data["operator"],
            data["created_at"], data["updated_at"], data["revision"], width, height,
            Calibration.from_data(calibration, width, height) if calibration else None,
            Segmentation.from_data(data["segmentation"]), data.get("reference"),
            tuple(View.from_data(v) for v in data["views"]),
            tuple(Gauge.from_data(g, g["id"]) for g in data["gauges"]),
            tuple(Note(**n) for n in data["notes"]),
            data.get("sample", False),
        )

    def changed(self, **fields):
        return replace(self, revision=self.revision+1, updated_at=utc_now(), **fields)

    def with_frame(self, width, height):
        if self.width:
            if (width, height) != (self.width, self.height):
                raise InspectionError(
                    f"影像尺寸应为 {self.width} × {self.height}，实际为 {width} × {height}。",
                    code="frame-size",
                )
            return self
        return replace(self, width=width, height=height, calibration=Calibration.default(width, height))

    def view(self, identity):
        return next((v for v in self.views if v.id == identity), None)

    def data(self):
        return asdict(self)

    def summary(self):
        return {
            "id": self.id, "title": self.title, "station": self.station,
            "operator": self.operator, "created_at": self.created_at,
            "updated_at": self.updated_at, "revision": self.revision,
            "width": self.width, "height": self.height, "view_count": len(self.views),
            "included_count": sum(view.included for view in self.views),
            "gauge_count": len(self.gauges), "sample": self.sample,
        }


def check_revision(value, actual):
    expected = integer(value, "检验版本", 0, 2**31-1)
    if expected != actual:
        raise InspectionError(
            "样件已在另一处更新，请刷新后重试。",
            status=409,
            code="stale-revision",
            details={"actual_revision": actual},
        )

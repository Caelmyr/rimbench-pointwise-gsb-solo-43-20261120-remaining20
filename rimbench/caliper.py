"""卡尺复检对零。

卡尺表只记录各高度的横向宽度，而卡尺靠山零位与转台零位可能相差一个
固定的高度补偿。本模块只允许整组读数沿高度平移（不修改宽度、不拉伸高度），
在给定补偿范围内寻找能让卡尺宽度落入转台方向宽度区间的补偿。

判定原则：
* 对齐后高度落在转台连续测量段内的读数才“可比”，宽度在区间内即相符；
* 对齐后落在缺测区或测量区间之外的读数只记录不可比原因，不参与胜负；
* 候选补偿必须让最多读数参与比较，且可比读数全部相符——避开难对上的
  点并不能让一个候选获胜；
* 直筒、重复台阶等让多段补偿都成立时，结论为“不能定零位”。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math

from .domain import InspectionError
from .geometry import METRIC_UNITS
from .profile import ProfileRequest, profile


MAX_CALIPER_ROWS = 2000
CALIPER_BYTES = 256 * 1024
MATCH_TOLERANCE = 1e-9
JOIN_TOLERANCE = 1e-6
STATE_MATCH = "caliper-match"
STATE_MISMATCH = "caliper-mismatch"
STATE_GAP = "caliper-gap"
STATE_OUTSIDE = "caliper-outside"
STATE_REASONS = {
    STATE_MATCH: "卡尺宽度落在转台方向宽度区间内。",
    STATE_MISMATCH: "对齐后高度有转台测量，但卡尺宽度不在区间内。",
    STATE_GAP: "对齐后高度落在转台缺测区，不能判定。",
    STATE_OUTSIDE: "对齐后高度超出转台测量区间，不能判定。",
}
DECISION_LABELS = {
    "determined": "可以定零位",
    "ambiguous": "还不能定零位",
    "no-fit": "没有可行补偿",
    "insufficient": "可比读数不足",
}


@dataclass(frozen=True)
class CaliperReading:
    index: int
    z: float
    width: float


def parse_caliper_csv(payload):
    """解析毫米单位的高度、宽度两列 CSV；允许带表头，逗号或分号分隔。"""
    if len(payload) > CALIPER_BYTES:
        raise InspectionError("卡尺 CSV 超过允许的大小。", status=413, code="caliper-file-limit")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = payload.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise InspectionError("卡尺 CSV 需要使用 UTF-8 或 GB18030 编码。", code="caliper-encoding") from exc
    readings = []
    seen_heights = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        delimiter = ";" if ";" in line and "," not in line else ","
        cells = [cell.strip() for cell in line.split(delimiter) if cell.strip() != ""]
        if len(cells) < 2:
            raise InspectionError(f"第 {line_no} 行需要高度和宽度两列。", code="caliper-columns")
        try:
            z = float(cells[0])
            width = float(cells[1])
        except ValueError:
            # 首个非数字行视为表头，其余行必须是数值。
            if not readings:
                continue
            raise InspectionError(f"第 {line_no} 行的高度或宽度不是数字。", code="caliper-numeric")
        if not math.isfinite(z) or not math.isfinite(width) or width < 0:
            raise InspectionError(f"第 {line_no} 行数值无效，宽度不能为负。", code="caliper-numeric")
        if any(abs(z-previous) < MATCH_TOLERANCE for previous in seen_heights):
            raise InspectionError(f"第 {line_no} 行的高度与前面的读数重复。", code="caliper-duplicate-height")
        seen_heights.add(z)
        readings.append(CaliperReading(len(readings)+1, z, width))
    if not readings:
        raise InspectionError("卡尺 CSV 中没有可读取的高度、宽度数据。", code="caliper-empty")
    if len(readings) > MAX_CALIPER_ROWS:
        raise InspectionError(f"卡尺读数最多 {MAX_CALIPER_ROWS} 行。", code="caliper-limit")
    return readings


def build_curve_request(readings, direction, shift_limit):
    """按卡尺读数的覆盖高度和补偿范围确定转台轴向曲线的取样范围。"""
    heights = [item.z for item in readings]
    lower = min(heights)-shift_limit
    upper = max(heights)+shift_limit
    if upper-lower < 1e-6:
        raise InspectionError("卡尺高度范围过窄，无法建立轴向曲线。", code="caliper-flat-range")
    span = max(heights)-min(heights)
    if span <= 0:
        count = 61
    else:
        # 让转台采样间距不大于卡尺读数间距的一半，避免台阶被采样点绕开。
        step = span / max(1, (len(readings)-1) * 2)
        count = min(121, max(31, int(math.ceil((upper-lower)/step))+1))
    return ProfileRequest(lower, upper, count, direction, "width")


def caliper_profile(meter, readings, direction, shift_limit):
    request = build_curve_request(readings, direction, shift_limit)
    curve = profile(meter, request)
    measured = [sample for sample in curve["samples"] if sample["value"] is not None and sample["interval"]]
    if not measured:
        raise InspectionError("所选方向在该高度范围内没有可用的转台测量。", code="caliper-no-reference")
    pitch = _pitch(measured)
    segments = _measured_segments(measured, pitch)
    return curve, segments, pitch


def _pitch(measured):
    gaps = sorted(
        b["z"]-a["z"] for a, b in zip(measured, measured[1:]) if b["z"] > a["z"]
    )
    return gaps[len(gaps)//2] if gaps else 1.0


def _measured_segments(measured, pitch):
    """把相邻的已测量采样点合并为连续测量段，段间即缺测区。"""
    segments = []
    current = []
    previous_z = None
    for sample in measured:
        if current and abs(sample["z"]-previous_z) > pitch*(1+1e-6)+1e-9:
            segments.append(current)
            current = []
        current.append(sample)
        previous_z = sample["z"]
    if current:
        segments.append(current)
    return segments


def _coverage_intervals(reading, segments, shift_limit):
    """该读数“可比”（对齐高度落在某测量段内）的补偿区间。"""
    out = []
    for segment in segments:
        left = max(-shift_limit, segment[0]["z"]-reading.z)
        right = min(shift_limit, segment[-1]["z"]-reading.z)
        if right >= left:
            out.append((left, right))
    return _merge_intervals(out)


def _halfline(slope, constant):
    """求解 slope*t+constant <= 0 的闭区间半直线。"""
    if abs(slope) < 1e-15:
        return [(-math.inf, math.inf)] if constant <= MATCH_TOLERANCE else []
    root = -constant/slope
    return [(-math.inf, root)] if slope > 0 else [(root, math.inf)]


def _bound_halfline(slope, constant, slab):
    """近零斜率按常数边界处理，避免浮点噪声制造数值伪根。"""
    span = slab[1]-slab[0]
    if span <= 0 or abs(slope)*span <= 1e-10:
        return [(-math.inf, math.inf)] if constant <= MATCH_TOLERANCE else []
    return _halfline(slope, constant)


def _intersect_unions(first, second, slab):
    """两个区间并集在 slab 内的交，按端点切分后逐开区间验证。"""
    points = [slab[0], slab[1]]
    for start, end in (*first, *second):
        if math.isfinite(start):
            points.append(max(slab[0], min(slab[1], start)))
        if math.isfinite(end):
            points.append(max(slab[0], min(slab[1], end)))
    points = sorted(set(points))
    out = []
    for left, right in zip(points, points[1:]):
        if right-left < 1e-12:
            continue
        middle = (left+right)/2
        if _in_union(first, middle) and _in_union(second, middle):
            out.append((left, right))
    return _merge_intervals(out)


def _in_union(intervals, value):
    return any(left-MATCH_TOLERANCE <= value <= right+MATCH_TOLERANCE for left, right in intervals)


def _merge_intervals(intervals, tolerance=JOIN_TOLERANCE):
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for left, right in ordered:
        if left <= merged[-1][1]+tolerance:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return [(left, right) for left, right in merged]


def reading_match_intervals(reading, segments, shift_limit):
    """该读数“宽度相符”的全部补偿区间，是其可比区间的子集。

    孤立单点测量只在唯一补偿上可比，贡献零宽区间；相邻已测量采样之间
    的区间宽度随高度线性变化（与曲线插值只在相邻测量间进行的原则一致）。
    """
    domain = (-shift_limit, shift_limit)
    matches = []
    for segment in segments:
        z0, z1 = segment[0]["z"], segment[-1]["z"]
        overlap = (max(-shift_limit, z0-reading.z), min(shift_limit, z1-reading.z))
        if overlap[1] < overlap[0]:
            continue
        if len(segment) < 2:
            # 孤立测量点只在唯一补偿上可比；宽度落在该点区间时贡献零宽可行点。
            point = z0-reading.z
            lo, hi = segment[0]["interval"]
            if domain[0]-MATCH_TOLERANCE <= point <= domain[1]+MATCH_TOLERANCE and lo-MATCH_TOLERANCE <= reading.width <= hi+MATCH_TOLERANCE:
                matches.append((max(domain[0], point), min(domain[1], point)))
            continue
        for left_sample, right_sample in zip(segment, segment[1:]):
            z_a, z_b = left_sample["z"], right_sample["z"]
            slab = (max(overlap[0], z_a-reading.z), min(overlap[1], z_b-reading.z))
            if slab[1] < slab[0]:
                continue
            lo_a, hi_a = left_sample["interval"]
            lo_b, hi_b = right_sample["interval"]
            slope_lo = (lo_b-lo_a)/(z_b-z_a)
            const_lo = lo_a-slope_lo*(z_a-reading.z)
            slope_hi = (hi_b-hi_a)/(z_b-z_a)
            const_hi = hi_a-slope_hi*(z_a-reading.z)
            # 相符条件 lo(delta) <= w <= hi(delta)，两条半直线都写成 表达式 <= 0：
            # w - hi(delta) <= 0 且 lo(delta) - w <= 0。
            below_hi = _bound_halfline(-slope_hi, reading.width-const_hi, slab)
            above_lo = _bound_halfline(slope_lo, const_lo-reading.width, slab)
            matches.extend(_intersect_unions(below_hi, above_lo, slab))
    return _merge_intervals([
        (max(domain[0], left), min(domain[1], right)) for left, right in matches
    ])


def align_caliper(readings, segments, pitch, shift_limit):
    """在补偿范围内扫描，返回对零结论与每个读数的核对结果。"""
    # 数值缝隙（远小于一个采样间距）视为同一连续可行段。
    merge_gap = max(JOIN_TOLERANCE, pitch*1e-6)
    coverage_sets = []
    match_sets = []
    events = {}

    def collect(intervals, add_index, remove_index):
        for left, right in intervals:
            bucket = events.setdefault(left, [0, 0, 0, 0])
            bucket[add_index] += 1
            bucket = events.setdefault(right, [0, 0, 0, 0])
            bucket[remove_index] += 1

    for reading in readings:
        coverage = _coverage_intervals(reading, segments, shift_limit)
        matches = reading_match_intervals(reading, segments, shift_limit)
        coverage_sets.append(coverage)
        match_sets.append(matches)
        collect(coverage, 0, 1)
        collect(matches, 2, 3)

    breakpoints = sorted(events)
    slabs = []
    active_coverage = 0
    active_matches = 0
    previous = -shift_limit
    for point in breakpoints:
        if point > previous+1e-12 and active_coverage:
            slabs.append([previous, point, active_coverage, active_matches])
        adds_c, removes_c, adds_m, removes_m = events[point]
        # 闭区间在端点处：先不移除，统计该点的真实覆盖，再进入下一开区间。
        point_coverage = active_coverage+adds_c
        point_matches = active_matches+adds_m
        if point_coverage:
            slabs.append([point, point, point_coverage, point_matches])
        active_coverage = max(0, active_coverage-removes_c)+adds_c
        active_matches = max(0, active_matches-removes_m)+adds_m
        previous = point
    if previous < shift_limit-1e-12 and active_coverage:
        slabs.append([previous, shift_limit, active_coverage, active_matches])
    n_max = max((slab[2] for slab in slabs), default=0)

    feasible = _merge_intervals([
        (left, right) for left, right, coverage, matches in slabs
        if coverage == n_max and coverage-matches == 0
    ], tolerance=merge_gap)
    candidates = [_candidate_summary(component, segments, pitch, readings) for component in feasible]
    decision, detail = _decision(feasible, n_max, len(readings), pitch, shift_limit)
    representative = _representative_delta(feasible, slabs, n_max)
    rows = [evaluate_reading(item, segments, pitch, representative) for item in readings]
    return {
        "decision": decision,
        "decision_label": DECISION_LABELS[decision],
        "detail": detail,
        "shift_limit": shift_limit,
        "representative_shift": representative,
        "pitch": pitch,
        "n_max": n_max,
        "total_readings": len(readings),
        "candidates": candidates,
        "rows": rows,
        "unit": METRIC_UNITS["width"],
        "states": STATE_REASONS,
    }


def _decision(feasible, n_max, total, pitch, shift_limit):
    if n_max <= 1:
        return "insufficient", (
            f"补偿范围内最多只有 {n_max} 个读数能与转台测量相比，重叠读数不足，无法据此确定零位。"
        )
    if not feasible:
        return "no-fit", (
            f"在 ±{shift_limit:g} mm 补偿范围内，最充分的对齐也只能让 {n_max}/{total} 个读数参与比较，"
            "且其中存在宽度不符；没有任何整组平移能让全部可比读数相符。"
        )
    if len(feasible) > 1:
        return "ambiguous", (
            f"存在 {len(feasible)} 段互不相连的可行补偿，直筒或重复台阶使多个零位都能成立，"
            "暂时不能确定卡尺靠山零位。"
        )
    left, right = feasible[0]
    width = right-left
    if width <= MATCH_TOLERANCE:
        return "determined", (
            f"唯一可行补偿为 {left:.4g} mm，共有 {n_max}/{total} 个读数参与判定且全部相符。"
        )
    if width > pitch+MATCH_TOLERANCE:
        return "ambiguous", (
            f"可行补偿区间宽 {width:.4g} mm，大于转台采样间距 {pitch:.4g} mm，"
            "该段轮廓没有足够的高度特征区分零位，暂时不能确定卡尺靠山零位。"
        )
    return "determined", (
        f"唯一可行补偿在 {left:.4g} 至 {right:.4g} mm 之间，"
        f"共有 {n_max}/{total} 个读数参与判定且全部相符。"
    )


def _representative_delta(feasible, slabs, n_max):
    if feasible:
        widest = max(feasible, key=lambda component: component[1]-component[0])
        return (widest[0]+widest[1])/2
    eligible = [slab for slab in slabs if slab[2] == n_max]
    if not eligible:
        return 0.0
    return (eligible[0][0]+eligible[-1][1])/2


def _candidate_summary(component, segments, pitch, readings):
    left, right = component
    midpoint = (left+right)/2
    rows = [evaluate_reading(item, segments, pitch, midpoint) for item in readings]
    counts = {state: sum(row["state"] == state for row in rows) for state in STATE_REASONS}
    return {
        "shift_low": left, "shift_high": right, "shift": midpoint,
        "comparable": counts[STATE_MATCH]+counts[STATE_MISMATCH],
        "matches": counts[STATE_MATCH], "mismatches": counts[STATE_MISMATCH],
        "gaps": counts[STATE_GAP], "outside": counts[STATE_OUTSIDE],
    }


def _classify(segments, pitch, z_aligned):
    """返回 (状态, (value, interval, span))；状态为 inside/gap/outside。"""
    starts = [segment[0]["z"] for segment in segments]
    overall_low = segments[0][0]["z"]-pitch*1e-6
    overall_high = segments[-1][-1]["z"]+pitch*1e-6
    if z_aligned < overall_low or z_aligned > overall_high:
        return "outside", None
    position = bisect_right(starts, z_aligned)-1
    if position < 0:
        return "outside", None
    segment = segments[position]
    first, last = segment[0]["z"], segment[-1]["z"]
    if z_aligned < first-pitch*1e-6 or z_aligned > last+pitch*1e-6:
        return "gap", None
    if z_aligned <= first:
        sample = segment[0]
        return "inside", (sample["value"], list(sample["interval"]), [first, first])
    if z_aligned >= last:
        sample = segment[-1]
        return "inside", (sample["value"], list(sample["interval"]), [last, last])
    for left_sample, right_sample in zip(segment, segment[1:]):
        z_a, z_b = left_sample["z"], right_sample["z"]
        if z_a <= z_aligned <= z_b:
            share = (z_aligned-z_a)/(z_b-z_a)
            value = left_sample["value"]+(right_sample["value"]-left_sample["value"])*share
            interval = [a+(b-a)*share for a, b in zip(left_sample["interval"], right_sample["interval"])]
            return "inside", (value, interval, [z_a, z_b])
    return "gap", None


def evaluate_reading(reading, segments, pitch, delta):
    aligned_z = reading.z+delta
    row = {
        "index": reading.index, "caliper_z": reading.z, "caliper_width": reading.width,
        "aligned_z": aligned_z, "shift": delta,
        "reference_value": None, "reference_interval": None, "reference_span": None,
        "delta_width": None, "state": STATE_OUTSIDE, "reason": STATE_REASONS[STATE_OUTSIDE],
    }
    kind, located = _classify(segments, pitch, aligned_z)
    if kind == "outside":
        return row
    if kind == "gap":
        row.update(state=STATE_GAP, reason=STATE_REASONS[STATE_GAP])
        return row
    value, interval, span = located
    row.update(
        reference_value=value, reference_interval=interval, reference_span=span,
        delta_width=reading.width-value,
    )
    if interval[0]-MATCH_TOLERANCE <= reading.width <= interval[1]+MATCH_TOLERANCE:
        row.update(state=STATE_MATCH, reason=STATE_REASONS[STATE_MATCH])
    else:
        row.update(state=STATE_MISMATCH, reason=STATE_REASONS[STATE_MISMATCH])
    return row


def run_alignment(meter, readings, direction, shift_limit):
    curve, segments, pitch = caliper_profile(meter, readings, direction, shift_limit)
    result = align_caliper(readings, segments, pitch, shift_limit)
    result.update(
        project_id=meter.project.id,
        revision=meter.project.revision,
        direction=direction,
        reference_parameters=curve["parameters"],
        measured_segments=[[segment[0]["z"], segment[-1]["z"]] for segment in segments],
        metric_label="指定方向宽度",
    )
    return result

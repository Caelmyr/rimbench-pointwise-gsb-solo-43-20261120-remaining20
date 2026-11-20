from __future__ import annotations

from .documents import STATES, _csv_fields, raw_json


AUDIT_STATES = {
    **STATES,
    "review": "需复核",
    "checked": "检查完成",
    "fits": "满足余量",
    "interference": "轮廓干涉",
    "insufficient-gap": "余量不足",
    "uncertain-gap": "余量区间跨过限值",
    "no-measurement": "截面缺测",
    "datum-outside": "基准点在轮廓外",
    "datum-insufficient": "基准有效覆盖不足",
    "no-fit": "无法稳定拟合",
    "undersampled": "角度抽样不足",
    "phase-weak": "相位信号不足",
    "within": "差值落在阈值内",
    "overlaps": "差值区间跨过阈值",
    "exceeds": "差值超出阈值",
    "first-missing": "第一张缺测",
    "second-missing": "第二张缺测",
    "both-missing": "两张缺测",
    "stable": "整幅稳定",
    "global-gain": "整幅亮度偏移",
    "localized-drift": "局部区域异常",
    "insufficient": "有效像素或靶点不足",
    "dimmer": "相对变暗",
    "brighter": "相对变亮",
    "dot-too-small": "靶像过小",
    "wide-edge": "边缘过渡过宽",
    "insufficient-spokes": "有效径向采样不足",
}


def status(value):
    return AUDIT_STATES.get(value, value)


def round_sheet(result):
    yield ["高度 (mm)", "角度 (°)", "拟合中心半径 (mm)", "径向残差 (mm)", "指定中心半径 (mm)", "指定中心径向残差 (mm)"]
    for point in result["polar"]:
        yield [result["z"], point["angle"], point["radius"], point["circle_residual"], point["datum_radius"], point["datum_residual"]]


def axial_sheet(result):
    yield ["用途", "高度 (mm)", "状态", "已用角度", "圆心 X (mm)", "圆心 Y (mm)", "拟合直径 (mm)",
           "拟合 RMS (mm)", "拟合圆心径向极差 (mm)", "基准 X (mm)", "基准 Y (mm)", "基准外推",
           "到基准轴的截线偏移 (mm)", "基准径向极差 (mm)", "到拟合中心线的残差 (mm)", "径向状态"]
    for kind, rows in (("基准段", result["datum"]["rows"]), ("被测段", result["rows"])):
        for row in rows:
            center = row["center"] or [None, None]
            datum = row.get("reference") or [None, None]
            yield [
                kind, row["z"], status(row["state"]), row["usable_views"], *center,
                row["diameter"], row["fit_rms"], row["form_spread"], *datum, row.get("reference_extrapolated"),
                row.get("offset"), row.get("radial_spread"), row.get("center_residual"), status(row.get("radial_state")),
            ]


def clearance_sheet(result):
    yield ["高度 (mm)", "截面状态", "套环判定", "名义余量 (mm)", "余量下界 (mm)", "余量上界 (mm)",
           "要求余量 (mm)", "限制点 X (mm)", "限制点 Y (mm)", "限制点方位 (°)", "角度缺口 (°)"]
    for row in result["rows"]:
        point = row["limiting_outer"] or {}
        xy = point.get("point", [None, None])
        yield [
            row["z"], status(row["measurement"]), status(row["state"]), row["nominal_gap"],
            row["gap_lower"], row["gap_upper"], row["required_gap"], *xy, point.get("direction"), row["normal_gap_deg"],
        ]


def lobe_sheet(result):
    yield ["高度 (mm)", "状态", "拟合半径 (mm)", "指定周期幅值 (mm)", "相位 (°)", "连续相位 (°)",
           "周期数", "主要周期数估计", "功率占比", "最大法线缺口 (°)"]
    for row in result["rows"]:
        yield [
            row["z"], status(row["state"]), row["radius"], row["amplitude"], row["phase"], row["phase_continued"],
            result["parameters"]["order"], row["dominant_order"], row["mode_power_fraction"], row["normal_gap"],
        ]


def edge_sheet(result):
    yield ["影像", "角度 (°)", "判读", "侧别", "端点 (mm)", "暗部", "亮部", "反差", "过渡宽 (px)",
           "过渡中点对分割端点的偏移 (px)", "半高穿越次数", "质检标记"]
    for row in result["rows"]:
        if not row["edges"]:
            yield [row["title"], row["angle"], status(row["state"]), None, None, None, None, None, None, None, None, row["reason"]]
        for edge in row["edges"]:
            tone = edge.get("tone", {})
            transition = edge.get("transition", {})
            yield [
                row["title"], row["angle"], status(edge["state"]), "左" if edge["side"] == "left" else "右",
                edge["endpoint_mm"], tone.get("shadow_level"), tone.get("light_level"), tone.get("contrast"),
                transition.get("width_px"), transition.get("midpoint_px"), transition.get("mid_level_crossings"), ", ".join(edge["flags"]),
            ]
    traces = [(row, edge) for row in result["rows"] for edge in row["edges"] if edge.get("trace")]
    if traces:
        yield []
        yield ["影像", "侧别", "离轮廓的距离 (px)", "原图 X (px)", "原图 Y (px)", "实测亮度", "补偿亮度", "空场亮度"]
    for row, edge in traces:
        for sample in edge["trace"]:
            yield [
                row["title"], "左" if edge["side"] == "left" else "右", sample["distance_px"],
                sample["x"], sample["y"], sample["raw"], sample["normalized"], sample["flat"],
            ]


def repeat_sheet(result):
    yield ["高度 (mm)", "状态", "第一张状态", "第二张状态", "第一张左端 (mm)", "第一张右端 (mm)",
           "第二张对齐左端 (mm)", "第二张对齐右端 (mm)", "左端差 (mm)", "右端差 (mm)",
           "宽度差 (mm)", "中心差 (mm)", "合并端点误差 (mm)", "角度误差 (°)"]
    for row in result["rows"]:
        first = row["first"] or {}
        second = row["second"] or {}
        changes = row["changes"] or {}
        yield [
            row["z"], status(row["state"]), status(row["first_state"]), status(row["second_state"]),
            first.get("left"), first.get("right"), second.get("left"), second.get("right"),
            changes.get("left"), changes.get("right"), changes.get("width"), changes.get("center"),
            changes.get("edge_error"), result["angle_delta_deg"],
        ]


def field_sheet(result):
    yield ["状态", status(result["state"]), "整体增益", result["gain"], "有效比例", result["valid_fraction"],
           "过暗比例", result["dark_fraction"], "饱和比例", result["saturated_fraction"], "采样像素间隔", result["stride"]]
    yield ["区域", "偏差类型", "左上 X (px)", "左上 Y (px)", "宽度 (px)", "高度 (px)", "采样数", "相对偏差均值", "绝对偏差峰值"]
    for index, row in enumerate(result["regions"], start=1):
        yield [index, status(row["type"]), *row["bounds"], row["sample_count"], row["mean_deviation"], row["max_absolute_deviation"]]


def grid_sheet(result):
    yield ["状态", status(result["state"]), "检测圆点", result["detected"]]
    yield ["行", "列", "名义 X (mm)", "名义 Y (mm)", "实测 X (px)", "实测 Y (px)", "残差 (px)",
           "定位状态", "清晰度状态", "径向过渡宽度中值 (px)", "过渡宽度 P90 (px)", "有效径向数"]
    for row in result["points"]:
        focus = row["focus"]
        transitions = focus["widths"] or {}
        yield [
            row["row"]+1, row["column"]+1, *row["nominal_mm"], *(row["observed_px"] or [None, None]),
            row["residual_px"], row.get("detail", status(row["state"])),
            status(focus["state"]), transitions.get("median"), transitions.get("p90"), transitions.get("count"),
        ]
    yield ["相邻方向", "起点", "终点", "像素比例 (px/mm)", "相对偏差"]
    for row in result["intervals"]:
        start, end = ([str(value+1) for value in row[key]] for key in ("start", "end"))
        yield [row["axis"], "/".join(start), "/".join(end), row["scale"], row["deviation"]]


SHEETS = {
    "round": round_sheet,
    "form": axial_sheet,
    "clearance": clearance_sheet,
    "lobes": lobe_sheet,
    "edges": edge_sheet,
    "repeat": repeat_sheet,
    "field": field_sheet,
    "grid": grid_sheet,
}


def export_data(kind, result, format_name, *, archive=None):
    if format_name == "json":
        return raw_json(archive or result), "application/json"
    header = []
    if archive:
        summary = archive["summary"]
        header.append(["核验记录", summary["title"], "记录版本", summary["revision"], "时间", summary["when"]])
    elif "revision" in result:
        header.append(["样件版本", result["revision"]])
    payload = _csv_fields([*header, *SHEETS[kind](result)])
    return payload, "text/csv; charset=utf-8"

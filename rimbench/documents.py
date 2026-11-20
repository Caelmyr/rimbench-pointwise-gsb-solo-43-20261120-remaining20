from __future__ import annotations

import csv
from html import escape
from io import StringIO
import json
import math
import re
from xml.etree.ElementTree import Element, SubElement, tostring

from .geometry import METRIC_LABELS, METRIC_UNITS
from .snapshots import METHOD


SVG_NS = "http://www.w3.org/2000/svg"
STATES = {
    "measured": "已测量", "borderline": "边界", "unbounded": "未闭合", "conflict": "冲突", "unseen": "缺少影像",
    "usable": "有效", "excluded": "已排除", "clipped": "触边", "outside": "超出画面",
    "empty": "未见主体", "too-narrow": "轮廓过窄", "disconnected": "分段", "image-rejected": "影像不可用",
    "pass": "合格", "fail": "超差", "review": "待复测", "unavailable": "暂无结果",
    "comparable": "可比较", "current-gap": "本次缺测", "reference-gap": "参考缺测", "outside-reference": "不在复测重叠区",
}


def safe_filename(title, stem, suffix):
    clean = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "-", title).strip(". -")
    return f"{clean[:72] or 'rimbench'}-{stem}.{suffix}"


def raw_json(document):
    return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _csv_fields(rows):
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def _cell(value):
    # User labels may be opened in a spreadsheet after export.
    if isinstance(value, str) and value.lstrip()[:1] in ("=", "+", "-", "@"):
        return "'"+value
    if isinstance(value, float):
        return format(value, ".9g")
    return value


def _bound(values, index):
    return values[index] if values is not None else None


def profile_csv(result):
    params = result["parameters"]
    unit = result["units"]
    rows = [["物理高度 (mm)", result["metric_label"], "单位", "区间下限", "区间上限",
             "有效角度", "法线最大缺口 (°)", "采样状态", "方向 (°)", "版本"]]
    for row in result["samples"]:
        low = _bound(row["interval"], 0)
        high = _bound(row["interval"], 1)
        rows.append([row["z"], row["value"], unit, low, high, row["usable_views"],
                     row["max_angle_gap"], STATES.get(row["state"], row["state"]), params["direction"], result["revision"]])
    return _csv_fields(rows)


def comparisons_csv(result):
    rows = [["本次高度 (mm)", "参考高度 (mm)", "本次值", "参考值", "差值", "量值单位",
             "差值区间下限", "差值区间上限", "参考采样起点 (mm)", "参考采样终点 (mm)", "比对状态"]]
    for row in result["rows"]:
        low = _bound(row["delta_interval"], 0)
        high = _bound(row["delta_interval"], 1)
        first = _bound(row["reference_span"], 0)
        last = _bound(row["reference_span"], 1)
        rows.append([row["z"], row["reference_z"], row["current"], row["reference"],
                     row["delta"], result["unit"], low, high, first, last, STATES[row["state"]]])
    return _csv_fields(rows)


def gauges_csv(result):
    rows = [["量规", "量值", "高度 (mm)", "方向 (°)", "下限", "标称", "上限", "单位",
             "实测值", "区间下限", "区间上限", "判定", "判定依据", "样件版本"]]
    for row in result["rows"]:
        gauge = row["gauge"]
        rows.append([
            gauge["title"], row["metric_label"], gauge["z"], gauge["direction"],
            gauge["low"], gauge["nominal"], gauge["high"], row["unit"],
            row["value"], _bound(row["interval"], 0), _bound(row["interval"], 1),
            STATES[row["decision"]], row["detail"], result["revision"],
        ])
    return _csv_fields(rows)


def evidence_csv(section):
    rows = [["影像", "转台角 (°)", "截面 (mm)", "影像状态", "左端 (mm)", "右端 (mm)",
             "左端 x (px)", "左端 y (px)", "右端 x (px)", "右端 y (px)",
             "端点误差 (mm)", "左侧活跃支撑", "右侧活跃支撑", "判定依据"]]
    live = {(plane["view_id"], plane["side"]) for plane in section["active_support"]}
    for row in section["observations"]:
        a, b = row.get("endpoints", [[None, None], [None, None]])
        left = "是" if (row["view_id"], "left") in live else "否"
        right = "是" if (row["view_id"], "right") in live else "否"
        rows.append([row["title"], row["angle"], row["z"], STATES[row["state"]], row.get("lower"), row.get("upper"),
                     *a, *b, row.get("edge_error_mm"), left, right, row["reason"]])
    return _csv_fields(rows)


def section_svg(section, *, print_scale=False, include_uncertainty=True, width=630, height=440):
    families = section["polygons"]
    all_points = [point for polygon in families.values() for point in polygon]
    if not all_points:
        svg = Element("svg", {"xmlns": SVG_NS, "viewBox": f"0 0 {width} {height}"})
        _svg_text(svg, width/2, height/2, section["detail"], anchor="middle", fill="#647477")
        return tostring(svg, encoding="unicode")
    xs, ys = zip(*all_points)
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_span = max(0.1, x_max-x_min)
    y_span = max(0.1, y_max-y_min)
    if print_scale:
        canvas_width_mm = min(190, max(40, x_span+24))
        canvas_height_mm = min(270, max(40, y_span+24))
        zoom = min(1.0, (canvas_width_mm-16)/x_span, (canvas_height_mm-16)/y_span)
        width, height = canvas_width_mm, canvas_height_mm
        attributes = {"width": f"{width:g}mm", "height": f"{height:g}mm", "data-print-scale": f"{zoom:.6g}"}
        caption_size = 3.0
        line_size = 0.25
    else:
        zoom = min((width-100)/x_span, (height-85)/y_span)
        attributes = {"width": str(width), "height": str(height)}
        caption_size = 13.0
        line_size = 1.4
    attributes.update(xmlns=SVG_NS, viewBox=f"0 0 {width:g} {height:g}", role="img", **{"aria-label": "轮廓截面图"})
    svg = Element("svg", attributes)
    _tag(svg, "title").text = f"{section['z']:.3f} mm 截面"
    _tag(svg, "rect", x=0, y=0, width=width, height=height, fill="#f5f8f7", rx=line_size*6)
    mid_x, mid_y = (x_min+x_max)/2, (y_min+y_max)/2

    def project(x, y):
        return width/2+(x-mid_x)*zoom, height/2-(y-mid_y)*zoom

    _grid(svg, width, height, zoom, project, caption_size, line_size)
    if include_uncertainty:
        _polygon(svg, families["outer"], project, "#dce9ee", "#5a7993", line_size, dash=line_size*3)
    _polygon(svg, families["nominal"], project, "#b4e0d3", "#1c695e", line_size*1.4)
    if include_uncertainty:
        _polygon(svg, families["inner"], project, "none", "#3a9481", line_size, dash=line_size*2)
    if section.get("centroid"):
        x, y = project(*section["centroid"])
        _tag(svg, "circle", cx=x, cy=y, r=caption_size*0.27, fill="#1f5b4f", stroke="#ffffff",
             stroke_width=line_size)
    if print_scale:
        _svg_text(svg, caption_size*1.2, height-caption_size*1.1, f"{zoom:.3g}∶1  ·  打印应禁用页面缩放",
                  size=caption_size, fill="#294c4b")
    return tostring(svg, encoding="unicode")


def _tag(parent, tag, **attrs):
    return SubElement(parent, tag, {key.replace("_", "-"): str(value) for key, value in attrs.items()})


def _svg_text(svg, x, y, text, *, anchor="start", fill="#273f3e", size=13):
    node = _tag(svg, "text", x=x, y=y, text_anchor=anchor, fill=fill,
                font_size=size, font_family="system-ui,sans-serif")
    node.text = str(text)
    return node


def _polygon(svg, polygon, project, fill, stroke, weight, *, dash=None):
    if not polygon:
        return
    points = " ".join(f"{x:.4f},{y:.4f}" for x, y in (project(*point) for point in polygon))
    appearance = {"fill": fill, "stroke": stroke, "stroke_width": weight}
    if dash:
        appearance["stroke_dasharray"] = dash
    _tag(svg, "polygon", points=points, **appearance)


def _grid(svg, width, height, scale, project, font, stroke):
    origin_x, origin_y = project(0, 0)
    period = _round_step(font*4/scale)
    first_x = math.ceil(-origin_x/(scale*period))*period
    last_x = (width-origin_x)/scale
    first_y = math.ceil((origin_y-height)/(scale*period))*period
    last_y = origin_y/scale
    value = first_x
    while value < last_x:
        px, _ = project(value, 0)
        if font < px < width-font:
            _tag(svg, "line", x1=px, y1=font, x2=px, y2=height-font*2, stroke="#cedad7", stroke_width=stroke*0.45)
            _svg_text(svg, px, height-font*0.8, _precision(value), size=font*0.68, anchor="middle")
        value += period
    value = first_y
    while value < last_y:
        _, py = project(0, value)
        if font < py < height-font*2.2:
            _tag(svg, "line", x1=font, y1=py, x2=width-font, y2=py, stroke="#cedad7", stroke_width=stroke*0.45)
            _svg_text(svg, width-font*0.65, py-font*0.2, _precision(value), size=font*0.68, anchor="end")
        value += period
    if font < origin_x < width-font:
        _tag(svg, "line", x1=origin_x, y1=font, x2=origin_x, y2=height-font*2.2, stroke="#839c96", stroke_width=stroke*0.75)
    if font < origin_y < height-font*2.2:
        _tag(svg, "line", x1=font, y1=origin_y, x2=width-font, y2=origin_y, stroke="#839c96", stroke_width=stroke*0.75)
    _svg_text(svg, width-font, font*1.35, "mm", size=font*0.75, anchor="end")


def _round_step(value):
    power = 10**math.floor(math.log10(max(value, 1e-6)))
    residue = value/power
    factor = next(goal for goal in (1, 2, 5, 10) if goal >= residue)
    return power*factor


def _precision(value):
    return f"{value:.5g}" if abs(value) > 1e-10 else "0"


def _human(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def _table(headings, rows):
    head = "".join(f"<th scope='col'>{escape(str(v))}</th>" for v in headings)
    body = "".join("<tr>"+"".join(f"<td>{escape(str(v))}</td>" for v in row)+"</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def html_report(project, section, gauges, *, captured_at=None, snapshot_name=None, curve=None):
    section_name = snapshot_name or f"{project['title']} · 截面检验"
    metrics = section["metrics"] or {}
    ranges = section["intervals"] or {}
    values = [
        [METRIC_LABELS[key], _human(value), METRIC_UNITS[key], _format_interval(ranges.get(key))]
        for key, value in metrics.items()
    ]
    readings = [
        [row["gauge"]["title"], _human(row["value"]), row["unit"], _format_interval(row["interval"]),
         STATES[row["decision"]], row["detail"]] for row in gauges["rows"]
    ]
    observations = [
        [row["title"], f"{row['angle']:g}°", STATES.get(row["state"], row["state"]), row["reason"]]
         for row in section["observations"]
    ]
    outline = section_svg(section, print_scale=True)
    scalars = _table(["量值", "结果", "单位", "区间"], values)
    tolerance = _table(["量规", "结果", "单位", "区间", "判定", "依据"], readings)
    acquisition = _table(["影像", "角度", "轮廓状态", "原因"], observations)
    geometry = project["calibration"]
    scale = geometry["scale_px"]/geometry["scale_mm"]
    basis = escape(f"{project['width']}×{project['height']} px · {scale:.4g} px/mm · {METHOD}")
    date = escape(captured_at or project["updated_at"])
    title = escape(section_name)
    z = f"{section['z']:.4g}"
    direction = f"{section['direction']:.4g}"
    subject = escape(project["title"])
    operator = escape(project["operator"] or "—")
    station = escape(project["station"] or "—")
    sample_badge = "<p>合成演示数据</p>" if project.get("sample") else ""
    curve_html = _curve_summary(curve) if curve else ""
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font:14px system-ui,sans-serif;color:#1e3535;margin:36px auto;padding:0 24px;max-width:920px}}
h1{{font-size:30px;margin:0 0 12px}}h2{{font-size:19px;margin:28px 0 8px}}
p{{margin:8px 0}}table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
td,th{{text-align:left;padding:7px;border-bottom:1px solid #c8d7d5}}
th{{font-weight:650;color:#1b594d}}svg{{max-width:100%;margin:14px auto;display:block}}
footer{{margin:25px 0;color:#54716f}}@media print{{body{{margin:0;padding:0}}table{{break-inside:auto}}tr{{break-inside:avoid}}}}
</style><body>
<header><h1>{title}</h1><p>{subject} · {station} · {operator} · 版本 {project['revision']}</p>
<p><time>{date}</time> · {basis}</p>{sample_badge}</header>
<main><h2>截面 {z} mm · 方向 {direction}°</h2><p>{escape(section['detail'])}</p>
{outline}{scalars}{curve_html}
<h2>公差判读</h2>{tolerance}<h2>分角度证据</h2>{acquisition}</main>
<footer>截面来自不透明物体的外轮廓支撑，仅描述凸包络。端点区间采用当前配方的像素容差和标尺误差。</footer>
</body></html>""".encode("utf-8")


def _format_interval(values):
    return f"{_human(values[0])} – {_human(values[1])}" if values is not None else "—"


def _curve_summary(curve):
    summary = curve["summary"]
    rows = [
        [curve["metric_label"], curve["units"], summary["measured"], summary["total"],
         _human(summary["variation"]), _human(summary["length_between_valid"])]
    ]
    return "<h2>轴向范围概要</h2>"+_table(
        ["量值", "单位", "已测量", "采样数", "极差", "首末有效点间距 (mm)"], rows,
    )

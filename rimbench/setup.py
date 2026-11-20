from __future__ import annotations

from dataclasses import replace

from .domain import (
    Calibration, InspectionError, MAX_NOTES, MAX_VIEWS, Note, Project,
    Segmentation, View, label, record, valid_id,
)
from .images import file_title, gray_from_png, ingest, reference_faults


def create_project(document):
    data = record(document, "样件")
    return Project.create(data.get("title"), data.get("station", ""), data.get("operator", ""))


def edit_project(project, data):
    data = record(data, "样件信息")
    title = label(data.get("title", project.title), "样件名称")
    station = label(data.get("station", project.station), "测站", empty=True)
    operator = label(data.get("operator", project.operator), "检验人", empty=True)
    if (title, station, operator) == (project.title, project.station, project.operator):
        return project
    return project.changed(title=title, station=station, operator=operator)


def edit_calibration(project, data):
    if not project.width:
        raise InspectionError("需要先添加采集图像。", code="no-frame")
    data = record(data, "轴标定")
    before = project.calibration
    new_calibration = Calibration.from_data({**before.data(), **data}, project.width, project.height)
    if before == new_calibration:
        return project
    return project.changed(calibration=new_calibration)


def edit_recipe(project, data):
    before = project.segmentation
    after = Segmentation.from_data({**before.data(), **record(data, "轮廓配方")})
    if before == after:
        return project
    if project.width and after.inset*2 >= min(project.width, project.height)-2:
        raise InspectionError("影像保护边距不能盖住整个取景范围。")
    return project.changed(segmentation=after)


def prep_capture(payload, filename, angle, title=None):
    meta, png = ingest(payload)
    view = View.create(meta.sha256, title or file_title(filename), angle)
    return (meta, png), view


def add_views(project, group):
    if len(project.views)+len(group) > MAX_VIEWS:
        raise InspectionError(f"每个样件最多添加 {MAX_VIEWS} 张角度影像。", status=409)
    if not group:
        return project
    working = project
    views = list(project.views)
    for asset, view in group:
        working = working.with_frame(asset.width, asset.height)
        views.append(view)
    return working.changed(views=tuple(views))


def modify_view(project, identity, data):
    existing = lookup_view(project, identity)
    changed = existing.revised(record(data, "角度影像"))
    if changed == existing:
        return project
    views = tuple(changed if view.id == identity else view for view in project.views)
    return project.changed(views=views)


def remove_view(project, identity):
    lookup_view(project, identity)
    current = tuple(view for view in project.views if view.id != identity)
    notes = tuple(replace(note, view=None) if note.view == identity else note for note in project.notes)
    return project.changed(views=current, notes=notes)


def lookup_view(project, identity):
    valid_id(identity, "角度影像ID")
    view = project.view(identity)
    if view is None:
        raise InspectionError("找不到此角度影像。", status=404, code="missing-view")
    return view


def prep_reference(payload):
    meta, png = ingest(payload)
    stats, warnings = reference_faults(gray_from_png(png))
    if stats["quantiles"][2] < 0.44 or stats["dark_fraction"] > 0.06:
        raise InspectionError(
            "这张影像不能用作空场参考。",
            code="reference-unsuitable", details={"checks": warnings},
        )
    return (meta, png), warnings


def set_reference(project, asset):
    if project.reference == asset.sha256:
        return project
    return project.with_frame(asset.width, asset.height).changed(reference=asset.sha256)


def clear_reference(project):
    if project.reference is None:
        return project
    return project.changed(reference=None)


def append_note(project, data):
    if len(project.notes) >= MAX_NOTES:
        raise InspectionError(f"此样件最多保留 {MAX_NOTES} 条记录。", status=409)
    note = Note.create(data)
    if note.view:
        lookup_view(project, note.view)
    return project.changed(notes=(*project.notes, note))


def remove_note(project, identity):
    valid_id(identity, "记录ID")
    retained = tuple(note for note in project.notes if note.id != identity)
    if len(retained) == len(project.notes):
        raise InspectionError("找不到此记录。", status=404, code="missing-note")
    return project.changed(notes=retained)

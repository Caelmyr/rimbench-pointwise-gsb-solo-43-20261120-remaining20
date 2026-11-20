from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import threading

from flask import Flask, Response, current_app, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from . import audit, audit_docs, documents, fieldcheck, geometry, gridtarget, lighting, quality, setup
from .compare import compare_profiles, compare_sections
from .demo import make_demo
from .domain import (
    InspectionError, MAX_IMAGE_BYTES, MAX_VIEWS, check_revision,
    choice, degrees, integer, number, record,
)
from .exchange import MAX_ARCHIVE_BYTES, build_bundle, open_bundle
from .images import crop_png, gray_from_png, ingest, thumbnail
from .meter import ImageCache, Meter
from .profile import ProfileRequest, profile, suggested_range
from .repeats import available_pairs
from .silhouette import overlay_png, ratio_preview
from .snapshots import capture, public_snapshot
from .store import Store
from .targets import Window, propose_axis, synthetic_target


STATIC = Path(__file__).resolve().parents[1]/"static"
VIEWS = ("original", "overlay", "normalized")


def create_app(directory=None):
    app = Flask("rimbench", static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_ARCHIVE_BYTES+1024*1024
    app.config["MAX_FORM_PARTS"] = 160
    root = directory or os.environ.get("RIMBENCH_DATA", ".rimbench")
    app.extensions["store"] = Store(root)
    app.extensions["image_cache"] = ImageCache()
    app.extensions["imaging_slots"] = threading.BoundedSemaphore(2)

    @app.get("/")
    def home():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        return {"service": "RimBench", "ok": True, "projects": _store().ping()}

    @app.errorhandler(InspectionError)
    def expected_failure(error):
        return jsonify(error={"message": str(error), "code": error.code, "details": error.details}), error.status

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_):
        return jsonify(error={"message": "请求体超过工作站接收上限。", "code": "request-too-large", "details": {}}), 413

    @app.after_request
    def response_policy(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    register_projects(app)
    register_reference(app)
    register_captures(app)
    register_measurements(app)
    register_gauges(app)
    register_archive(app)
    register_inspection(app)
    return app


def _store():
    return current_app.extensions["store"]


def _json():
    return record(request.get_json(silent=True), "请求")


def _one_upload(name="image", maximum=MAX_IMAGE_BYTES):
    capture = request.files.get(name)
    if not capture or not capture.filename:
        raise InspectionError("请选择要上传的文件。", code="file-required")
    content = capture.stream.read(maximum+1)
    if len(content) > maximum:
        raise InspectionError("文件超过允许的大小。", status=413, code="file-limit")
    return capture.filename, content


def _multipart_arguments(label):
    try:
        parsed = json.loads(request.form.get("parameters", "{}"))
    except (TypeError, ValueError) as exc:
        raise InspectionError(f"{label}参数无法读取。") from exc
    return record(parsed, label)


def _in_meter(identity, action):
    cache = current_app.extensions["image_cache"]
    limiter = current_app.extensions["imaging_slots"]
    if not limiter.acquire(timeout=15):
        raise InspectionError("其他测量正在处理，请稍后重试。", status=503, code="imaging-busy")
    try:
        return _store().read_project_images(identity, lambda project, load: action(Meter(project, load, cache)))
    finally:
        limiter.release()


def _api_project(project):
    value = project.data()
    value.update(coverage=geometry.circular_coverage(project.views), summary=project.summary())
    return value


def _change(identity, fn):
    data = _json()
    project = _store().change(identity, data.get("revision"), lambda p: fn(p, data))
    return _api_project(project)


def _query_geometry():
    return (
        number(request.args.get("z", 20), "截面高度", -100000, 100000),
        degrees(request.args.get("direction", 0)),
    )


def _download(payload, title, stem, ext, mimetype):
    return send_file(
        BytesIO(payload), mimetype=mimetype, as_attachment=True,
        download_name=documents.safe_filename(title, stem, ext), max_age=0,
    )


def _png(data, *, conditional=False):
    if not conditional:
        return Response(data, mimetype="image/png")
    response = send_file(BytesIO(data), mimetype="image/png", max_age=0, etag=False, conditional=False)
    return response


def register_projects(app):
    @app.get("/api/projects")
    def projects():
        samples = _store().list_projects()
        return {"items": samples, "stations": quality.station_history(samples)}

    @app.post("/api/projects")
    def new_project():
        return _api_project(_store().save_new(setup.create_project(_json()))), 201

    @app.post("/api/demos")
    def demo():
        kind = choice(_json().get("kind", "stepped"), "合成样件", ("stepped", "eccentric"))
        with current_app.extensions["imaging_slots"]:
            project, assets = make_demo(kind)
        return _api_project(_store().save_new(project, assets)), 201

    @app.post("/api/projects/import")
    def exchange_import():
        _, payload = _one_upload("bundle", maximum=MAX_ARCHIVE_BYTES)
        with current_app.extensions["imaging_slots"]:
            project, assets = open_bundle(payload, request.form.get("title"))
        return _api_project(_store().save_new(project, assets)), 201

    @app.get("/api/projects/<identity>")
    def get_project(identity):
        return _api_project(_store().project(identity))

    @app.patch("/api/projects/<identity>")
    def edit_project(identity):
        return _change(identity, setup.edit_project)

    @app.delete("/api/projects/<identity>")
    def remove_project(identity):
        _store().remove_project(identity, _json().get("revision"))
        return "", 204

    @app.patch("/api/projects/<identity>/calibration")
    def calibrate(identity):
        return _change(identity, setup.edit_calibration)

    @app.get("/api/projects/<identity>/target/demo.png")
    def demo_target(identity):
        project = _store().project(identity)
        if not project.width:
            raise InspectionError("需要先设置图像画幅。", code="no-frame")
        return _png(synthetic_target(project.width, project.height)[0])

    @app.post("/api/projects/<identity>/target")
    def measure_targets(identity):
        import json

        project = _store().project(identity)
        if not project.width:
            raise InspectionError("需要先设置图像画幅。", code="no-frame")
        _, data = _one_upload()
        try:
            roi = json.loads(request.form.get("parameters", "{}"))
        except (TypeError, ValueError) as exc:
            raise InspectionError("标靶设置无法读取。") from exc
        from .images import ingest
        meta, canonical = ingest(data)
        if (meta.width, meta.height) != (project.width, project.height):
            raise InspectionError("标靶影像尺寸必须与样件画面一致。", code="frame-size")
        return propose_axis(canonical, roi, meta.width, meta.height)

    @app.patch("/api/projects/<identity>/recipe")
    def edit_recipe(identity):
        return _change(identity, setup.edit_recipe)

    @app.post("/api/projects/<identity>/notes")
    def add_note(identity):
        return _change(identity, setup.append_note), 201

    @app.delete("/api/projects/<identity>/notes/<note>")
    def delete_note(identity, note):
        return _change(identity, lambda project, _data: setup.remove_note(project, note))

    @app.get("/api/projects/<identity>/bundle")
    def exchange_export(identity):
        def write(meter):
            product = meter.project
            return _download(build_bundle(product, meter.load), product.title, "样件交换", "zip", "application/zip")

        return _in_meter(identity, write)


def register_reference(app):
    path = "/api/projects/<identity>/reference"

    @app.get(path)
    def reference_info(identity):
        return _in_meter(identity, lambda meter: {"reference": meter.inspect_reference()})

    @app.post(path)
    def set_reference(identity):
        _, payload = _one_upload()
        with current_app.extensions["imaging_slots"]:
            packed, warnings = setup.prep_reference(payload)
        result = _store().change(
            identity, request.form.get("revision"), lambda p: setup.set_reference(p, packed[0]), (packed,),
        )
        return {"project": _api_project(result), "warnings": warnings}

    @app.delete(path)
    def remove_reference(identity):
        return _change(identity, lambda project, _data: setup.clear_reference(project))

    @app.get(path+"/image")
    def reference_image(identity):
        def write(meter):
            meta, payload = meter.load(_required_reference(meter))
            edge = integer(request.args.get("size", 960), "图像预览边长", 96, 1500)
            response = _png(thumbnail(payload, edge), conditional=True)
            response.set_etag(f"{meta.sha256}-{edge}")
            response.make_conditional(request)
            return response

        return _in_meter(identity, write)

    @app.get(path+"/lighting")
    def reference_lighting(identity):
        return _in_meter(identity, lambda meter: _lighting(meter))

    @app.get(path+"/heatmap")
    def reference_heatmap(identity):
        def write(meter):
            ramp = choice(request.args.get("ramp", "median"), "背光叠层", ("median", "shadow"))
            return _png(lighting.heatmap(_lighting(meter), ramp))

        return _in_meter(identity, write)

    @app.post(path+"/compare")
    def compare_light(identity):
        _, payload = _one_upload()
        grid = integer(request.form.get("grid", 8), "网格数", 3, 16)
        from .images import ingest
        meta, png = ingest(payload)

        def calculate(meter):
            project = meter.project
            if (meta.width, meta.height) != (project.width, project.height):
                raise InspectionError("待对照空场的画幅需与当前标定一致。", code="frame-size")
            base = meter.gray(_required_reference(meter))
            return lighting.analyze(gray_from_png(png), grid, base)

        return _in_meter(identity, calculate)


def _required_reference(meter):
    if not meter.project.reference:
        raise InspectionError("当前还未设置空场影像。", status=404, code="missing-reference")
    return meter.project.reference


def _lighting(meter):
    reference = meter.gray(_required_reference(meter))
    return lighting.analyze(reference, request.args.get("grid", 8))


def register_captures(app):
    path = "/api/projects/<identity>/views"

    @app.post(path)
    def upload_view(identity):
        filename, payload = _one_upload()
        with current_app.extensions["imaging_slots"]:
            packed, view = setup.prep_capture(payload, filename, request.form.get("angle"), request.form.get("title"))
        return _save_views(identity, request.form.get("revision"), [(packed, view)])

    @app.post(path+"/batch")
    def upload_views(identity):
        files = request.files.getlist("images")
        if not files or len(files) > MAX_VIEWS:
            raise InspectionError(f"一次可提交 1–{MAX_VIEWS} 张角度影像。")
        start = number(request.form.get("start_angle"), "起始转台角", -36000, 36000)
        step = number(request.form.get("angle_step"), "角度步进", -360, 360)
        if not step and len(files) > 1:
            raise InspectionError("批处理的转台角度步进不能为零。")
        batch = []
        with current_app.extensions["imaging_slots"]:
            for idx, up in enumerate(files):
                payload = up.stream.read(MAX_IMAGE_BYTES+1)
                packed, view = setup.prep_capture(payload, up.filename, start+idx*step)
                batch.append((packed, view))
        return _save_views(identity, request.form.get("revision"), batch)

    @app.get(path+"/<view>")
    def view_info(identity, view):
        z, _ = _query_geometry()
        return _in_meter(identity, lambda m: m.inspect_view(setup.lookup_view(m.project, view), z))

    @app.patch(path+"/<view>")
    def view_edit(identity, view):
        return _change(identity, lambda project, data: setup.modify_view(project, view, data))

    @app.delete(path+"/<view>")
    def view_delete(identity, view):
        return _change(identity, lambda project, _data: setup.remove_view(project, view))

    @app.get(path+"/<view>/image")
    def view_image(identity, view):
        mode = choice(request.args.get("mode", "original"), "图像显示", VIEWS)
        edge = integer(request.args.get("size", 1100), "预览最大边长", 72, 2000)

        def write(meter):
            item = setup.lookup_view(meter.project, view)
            if mode == "original":
                meta, raw = meter.load(item.asset)
                response = _png(thumbnail(raw, edge), conditional=True)
                response.set_etag(f"{meta.sha256}-{edge}")
                return response.make_conditional(request)
            silhouette = meter.view_mask(item)
            return _png(overlay_png(silhouette) if mode == "overlay" else ratio_preview(silhouette, edge))

        return _in_meter(identity, write)

    @app.get(path+"/<view>/crop")
    def view_crop(identity, view):
        def write(meter):
            p = meter.project
            item = setup.lookup_view(p, view)
            bounds = Window.parse({k: request.args.get(k) for k in ("x", "y", "width", "height")}, p.width, p.height, minimum=2)
            extent = (bounds.x, bounds.y, bounds.width, bounds.height)
            image, actual = crop_png(meter.load(item.asset)[1], extent, 1200)
            response = _png(image)
            response.headers["X-RimBench-Crop"] = ",".join(str(x) for x in actual)
            return response

        return _in_meter(identity, write)

    @app.get(path+"/<view>/coordinate")
    def view_location(identity, view):
        x, y = request.args.get("x"), request.args.get("y")
        return _in_meter(identity, lambda m: m.axis().format_click(x, y, setup.lookup_view(m.project, view)))


def _save_views(identity, revision, batch):
    assets = [packed for packed, _ in batch]
    records = [(meta, view) for (meta, _), view in batch]
    revised = _store().change(identity, revision, lambda p: setup.add_views(p, records), assets)
    return {"project": _api_project(revised), "added": [view.id for _, view in batch]}, 201


def register_measurements(app):
    path = "/api/projects/<identity>"

    @app.get(path+"/overview")
    def axis_overview(identity):
        def calculate(meter):
            return {
                "axis": meter.axis_summary(), "suggested": suggested_range(meter),
                "reference": meter.inspect_reference(),
            }

        return _in_meter(identity, calculate)

    @app.get(path+"/section")
    def section(identity):
        z, direction = _query_geometry()
        return _in_meter(identity, lambda meter: {
            "revision": meter.project.revision,
            "section": meter.section(z, direction),
        })

    @app.get(path+"/coverage")
    def coverage(identity):
        z, _ = _query_geometry()
        return _in_meter(identity, lambda meter: quality.acquisition_report(meter, z))

    @app.get(path+"/section.svg")
    def section_download(identity):
        z, direction = _query_geometry()
        printing = request.args.get("print", "0") == "1"

        def write(meter):
            drawing = documents.section_svg(meter.section(z, direction), print_scale=printing)
            return _download(drawing.encode(), meter.project.title, f"{z:g}mm", "svg", "image/svg+xml")

        return _in_meter(identity, write)

    @app.get(path+"/section.csv")
    def section_evidence(identity):
        z, direction = _query_geometry()

        def write(meter):
            section = meter.section(z, direction)
            return _download(documents.evidence_csv(section), meter.project.title, "角度证据", "csv", "text/csv; charset=utf-8")

        return _in_meter(identity, write)

    @app.get(path+"/profile")
    def profile_data(identity):
        args = ProfileRequest.parse(request.args.to_dict())
        return _in_meter(identity, lambda meter: profile(meter, args))

    @app.get(path+"/profile.csv")
    def profile_download(identity):
        args = ProfileRequest.parse(request.args.to_dict())

        def write(meter):
            result = profile(meter, args)
            return _download(documents.profile_csv(result), meter.project.title, "轴向曲线", "csv", "text/csv; charset=utf-8")

        return _in_meter(identity, write)


def register_gauges(app):
    path = "/api/projects/<identity>/gauges"

    @app.put(path)
    def gauges_replace(identity):
        return _change(identity, lambda project, data: quality.replace_gauges(project, data.get("gauges")))

    @app.get(path)
    def gauges_data(identity):
        def calculate(meter):
            result = quality.gauge_results(meter)
            return {**result, "verification": quality.review_gate(meter.project, result)}

        return _in_meter(identity, calculate)

    @app.get(path+".csv")
    def gauges_export(identity):
        def write(meter):
            result = quality.gauge_results(meter)
            return _download(documents.gauges_csv(result), meter.project.title, "公差判读", "csv", "text/csv; charset=utf-8")

        return _in_meter(identity, write)


def register_archive(app):
    path = "/api/projects/<identity>/snapshots"

    @app.get(path)
    def snapshots(identity):
        return {"items": _store().list_snapshots(identity)}

    @app.post(path)
    def save_snapshot(identity):
        data = _json()
        project, archive = _in_meter(identity, lambda meter: (meter.project, capture(meter, data)))
        return _store().save_snapshot(project, archive), 201

    @app.get(path+"/<snapshot>")
    def archived(identity, snapshot):
        return public_snapshot(_store().snapshot(identity, snapshot))

    @app.delete(path+"/<snapshot>")
    def remove_snapshot(identity, snapshot):
        _store().remove_snapshot(identity, snapshot)
        return "", 204

    @app.get(path+"/<snapshot>/data.json")
    def archived_json(identity, snapshot):
        data = _store().snapshot(identity, snapshot)
        return _download(documents.raw_json(public_snapshot(data)), data["summary"]["title"],
                         "测量快照", "json", "application/json")

    @app.get(path+"/<snapshot>/report")
    def archived_report(identity, snapshot):
        raw = _store().snapshot(identity, snapshot)
        body = documents.html_report(
            raw["project"], raw["section"], raw["gauges"],
            captured_at=raw["summary"]["captured_at"],
            snapshot_name=raw["summary"]["title"], curve=raw["profile"],
        )
        return Response(body, mimetype="text/html")

    @app.get(path+"/<snapshot>/compare")
    @app.get(path+"/<snapshot>/compare.csv")
    def archive_compare(identity, snapshot):
        saved = _store().snapshot(identity, snapshot)
        reference_profile = saved.get("profile")
        args = request.args.to_dict()
        mode = choice(args.get("mode", "profile"), "比对模式", ("profile", "section"))
        if mode == "section":
            def compare_outline(meter):
                old = saved["section"]
                current = meter.section(old["z"], old["direction"])
                return compare_sections(old, current)

            return _in_meter(identity, compare_outline)
        if not reference_profile:
            raise InspectionError("该快照没有保存轴向曲线。", code="no-snapshot-curve")
        spec = ProfileRequest.parse({**reference_profile["parameters"], **args})
        z_shift = number(args.get("z_shift", 0), "参考高度补偿", -100000, 100000)

        def compare_curve(meter):
            current = profile(meter, spec)
            result = compare_profiles(reference_profile, current, z_shift)
            if request.path.endswith(".csv"):
                return _download(documents.comparisons_csv(result), saved["summary"]["title"],
                                 "复测差值", "csv", "text/csv; charset=utf-8")
            return result

        return _in_meter(identity, compare_curve)

    @app.get("/api/projects/<identity>/report")
    def live_report(identity):
        z, direction = _query_geometry()

        def write(meter):
            outline = meter.section(z, direction)
            return Response(documents.html_report(meter.project.data(), outline, quality.gauge_results(meter)), mimetype="text/html")

        return _in_meter(identity, write)


def register_inspection(app):
    root = "/api/projects/<identity>/inspection"

    @app.get(root+"/pairs")
    def pairs(identity):
        return available_pairs(_store().project(identity))

    @app.get(root+"/calculate/<kind>")
    @app.get(root+"/calculate/<kind>.<format_name>")
    def inspection_calculate(identity, kind, format_name=None):
        arguments = request.args.to_dict()

        def calculate(meter):
            audit.frozen_revision(arguments, meter.project.revision)
            report = audit.calculate(meter, kind, arguments)
            if not format_name:
                return report
            return _inspection_download(kind, report, format_name, meter.project.title)

        return _in_meter(identity, calculate)

    @app.get(root+"/records")
    def inspection_records(identity):
        return _store().list_audits(identity)

    @app.post(root+"/records")
    def save_inspection_record(identity):
        body = _json()
        project, archive = _in_meter(identity, lambda meter: (meter.project, audit.record_audit(meter, body)))
        return _store().save_audit(project, archive), 201

    @app.get(root+"/records/<record_id>/data.<format_name>")
    def download_inspection_record(identity, record_id, format_name):
        archived = _store().audit(identity, record_id)
        name = archived["summary"]["title"]
        kind = archived["summary"]["kind"]
        return _inspection_download(kind, archived["result"], format_name, name, archive=archived)

    @app.get(root+"/records/<record_id>")
    def get_inspection_record(identity, record_id):
        return _store().audit(identity, record_id)

    @app.delete(root+"/records/<record_id>")
    def remove_inspection_record(identity, record_id):
        _store().remove_audit(identity, record_id)
        return "", 204

    @app.get(root+"/field/demo.png")
    def field_demo_image(identity):
        variable = request.args.get("type") != "gain"

        def render(meter):
            reference = _required_reference(meter)
            return _png(fieldcheck.field_fixture(meter.gray(reference), variable))

        return _in_meter(identity, render)

    @app.post(root+"/field")
    @app.post(root+"/field.<format_name>")
    def inspect_field(identity, format_name=None):
        params = _multipart_arguments("光场复检")
        _, payload = _one_upload()

        def calculate(meter):
            audit.frozen_revision(params, meter.project.revision)
            reference = _required_reference(meter)
            original = meter.gray(reference)
            height, width = original.shape
            source = _match_station_image(payload, width, height)
            settings = fieldcheck.FieldRequest.parse(params, width, height)
            if format_name == "png":
                return _png(fieldcheck.compare_fields(source, original, settings, render=True))
            result = fieldcheck.compare_fields(source, original, settings)
            result.update(project_id=meter.project.id, revision=meter.project.revision, reference_sha256=reference)
            if format_name:
                return _inspection_download("field", result, format_name, meter.project.title)
            return result

        return _in_meter(identity, calculate)

    @app.get(root+"/grid/demo")
    @app.get(root+"/grid/demo.png")
    def grid_demo(identity):
        project = _store().project(identity)
        if not project.width or not project.calibration:
            raise InspectionError("需要先设置工位画幅和轴标尺。", code="grid-requires-frame")
        with current_app.extensions["imaging_slots"]:
            drawing, parameters = gridtarget.grid_fixture(
                project.width, project.height, project.calibration.px_per_mm,
                distortion=request.args.get("type") == "distorted",
                tilted=request.args.get("type") == "tilted",
            )
        if request.path.endswith(".png"):
            return _png(drawing)
        return {"parameters": parameters, "frame": [project.width, project.height]}

    @app.post(root+"/grid")
    @app.post(root+"/grid.<format_name>")
    def inspect_grid(identity, format_name=None):
        params = _multipart_arguments("九点标板")
        _, payload = _one_upload()

        def calculate(meter):
            project = meter.project
            audit.frozen_revision(params, project.revision)
            width, height = project.width, project.height
            source = _match_station_image(payload, width, height)
            settings = gridtarget.GridRequest.parse(params, width, height)
            result = gridtarget.inspect_grid(source, settings)
            result.update(project_id=project.id, revision=project.revision)
            if format_name:
                return _inspection_download("grid", result, format_name, project.title)
            return result

        return _in_meter(identity, calculate)


def _match_station_image(payload, width, height):
    if not width or not height:
        raise InspectionError("需要先确定工位影像画幅。", code="no-frame")
    meta, png = ingest(payload)
    if (meta.width, meta.height) != (width, height):
        raise InspectionError("上传图像的像素画幅需要与样件画面一致。", code="frame-size")
    return gray_from_png(png)


def _inspection_download(kind, result, format_name, title, *, archive=None):
    format_name = choice(format_name, "核验报告格式", ("json", "csv"))
    payload, mimetype = audit_docs.export_data(kind, result, format_name, archive=archive)
    name = audit.REPORTS[kind].name if kind in audit.REPORTS else {"field": "光场复检", "grid": "九点标板"}[kind]
    return _download(payload, title, name, format_name, mimetype)

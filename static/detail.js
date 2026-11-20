import {api, f, qs} from "./ui.js";

// The detail view shows the stored original itself, cropped on the server to
// just the window the screen needs. Position and layer choice are kept per
// image, so switching between the original and the contour overlay never moves
// the view. A generation counter plus AbortController drop late responses, so a
// crop for the previous angle cannot be painted into the current image.
const SIZES = [64, 96, 192, 300, 420, 520, 640, 800, 1000, 1400, 2000];
const MAX_SCALE = 48;
const MARGIN = 0.5;
const QUIET_DELAY = 110;
const SNAP_DELAY = 24;
const registry = {current: null};

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

export function detailMarkup(active) {
  const layer = active.imageMode === "overlay" ? "overlay" : "original";
  return `<div class="detail-view" id="detail-view">
    <div class="detail-toolbar">
      <div class="detail-modes" role="group" aria-label="细节图层">
        <button type="button" data-detail-mode="original" aria-pressed="${layer === "original"}">原图</button>
        <button type="button" data-detail-mode="overlay" aria-pressed="${layer === "overlay"}">轮廓叠层</button>
      </div>
      <span class="detail-crop subtext" id="detail-crop">正在读取原图细节…</span>
      <div class="detail-zoom" role="group" aria-label="缩放">
        <button type="button" data-detail="out" title="缩小" aria-label="缩小">−</button>
        <output id="detail-scale" aria-live="off">适应</output>
        <button type="button" data-detail="in" title="放大" aria-label="放大">＋</button>
        <button type="button" data-detail="fit" title="适应窗口">适应</button>
        <button type="button" data-detail="one" title="一个原图像素一个屏幕像素">1:1</button>
      </div>
    </div>
    <div class="detail-canvas" id="detail-canvas" tabindex="0" role="application"
         aria-label="原图细览：滚轮缩放，拖动平移，点选像素查看数值">
      <img class="detail-layer" id="detail-base" alt="所选角度原图的局部细览" draggable="false" hidden>
      <svg class="detail-layer detail-lines" id="detail-guides" aria-hidden="true"></svg>
      <svg class="detail-layer detail-lines" id="detail-lines" aria-hidden="true"></svg>
      <div class="detail-empty" id="detail-empty">正在读取原图细节…</div>
    </div>
    <div class="status-line detail-readout" id="detail-readout" aria-live="polite">
      滚轮缩放 · 拖动平移 · 点击像素查看原图坐标、实际数值和轮廓归属
    </div>
  </div>`;
}

export function mountDetail(ctx, inspected) {
  const stage = document.querySelector("#detail-canvas");
  if (!stage) return null;
  registry.current?.dispose();
  const p = ctx.active.project;
  const width = p.width || 1;
  const height = p.height || 1;
  const state = ctx.detail;
  const viewId = ctx.active.view;
  if (state.project !== p.id) {
    Object.assign(state, {project: p.id, view: null, scale: 0, cx: width/2, cy: height/2, selected: null,
      lastKey: null, lastLayer: null, layer: null});
  }
  if (state.view !== viewId) {
    Object.assign(state, {view: viewId, selected: null, lastKey: null, lastLayer: null});
  }
  // The detail switch owns the layer between renders; the overview selector only
  // seeds it. An unrelated re-render must not bounce the operator's choice back.
  if (state.layer !== "original" && state.layer !== "overlay") {
    state.layer = ctx.active.imageMode === "original" ? "original" : "overlay";
  }
  document.querySelectorAll("[data-detail-mode]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.detailMode === state.layer));
  });

  const baseImage = document.querySelector("#detail-base");
  const guides = document.querySelector("#detail-guides");
  const marks = document.querySelector("#detail-lines");
  const empty = document.querySelector("#detail-empty");
  const readout = document.querySelector("#detail-readout");
  const scaleNote = document.querySelector("#detail-scale");
  const cropNote = document.querySelector("#detail-crop");
  const root = document.querySelector("#detail-view");

  let disposed = false;
  let loadToken = 0;
  let selectToken = 0;
  let loading = false;
  let queued = false;
  let controller = null;
  let timer = 0;
  let crop = null;
  let drag = null;
  let observer = null;

  function box() {
    return stage.getBoundingClientRect();
  }

  function fitScale() {
    const rect = box();
    if (!rect.width || !rect.height) return 1;
    return Math.min(rect.width/width, rect.height/height);
  }

  function scale() {
    return state.scale || fitScale();
  }

  function centerFor(rect, s) {
    const halfW = rect.width/(2*s);
    const halfH = rect.height/(2*s);
    state.cx = halfW >= width/2 ? width/2 : clamp(state.cx, halfW, width-halfW);
    state.cy = halfH >= height/2 ? height/2 : clamp(state.cy, halfH, height-halfH);
  }

  function visibleWindow(rect, s) {
    const halfW = rect.width/(2*s);
    const halfH = rect.height/(2*s);
    return {
      x0: clamp(state.cx-halfW, 0, width), x1: clamp(state.cx+halfW, 0, width),
      y0: clamp(state.cy-halfH, 0, height), y1: clamp(state.cy+halfH, 0, height),
    };
  }

  function requestWindow(visible) {
    const padX = Math.max(2, Math.ceil((visible.x1-visible.x0)*MARGIN));
    const padY = Math.max(2, Math.ceil((visible.y1-visible.y0)*MARGIN));
    const x = Math.floor(clamp(visible.x0-padX, 0, width-2));
    const y = Math.floor(clamp(visible.y0-padY, 0, height-2));
    const right = Math.ceil(clamp(visible.x1+padX, x+2, width));
    const bottom = Math.ceil(clamp(visible.y1+padY, y+2, height));
    return {x, y, width: right-x, height: bottom-y};
  }

  function requestSize(windowRect) {
    // Ask for one output pixel per displayed pixel of this window at the current
    // zoom: a large frame shown small transfers little, and high zoom never gets
    // a softened picture. The server never upsamples beyond the window.
    const wanted = Math.ceil(Math.max(windowRect.width, windowRect.height)*scale());
    const long = Math.min(2000, Math.max(32, wanted));
    return SIZES.find(size => size >= long) ?? 2000;
  }

  function clearCrop() {
    loadToken++;
    controller?.abort();
    controller = null;
    loading = false;
    queued = false;
    if (crop?.url) URL.revokeObjectURL(crop.url);
    crop = null;
    baseImage.hidden = true;
    baseImage.removeAttribute("src");
    guides.innerHTML = "";
    marks.innerHTML = "";
    empty.hidden = false;
    empty.textContent = "正在读取原图细节…";
    cropNote.textContent = "正在读取原图细节…";
  }

  function schedule(delay = QUIET_DELAY) {
    clearTimeout(timer);
    timer = window.setTimeout(load, delay);
  }

  function wanted() {
    const rect = box();
    if (!rect.width || !rect.height) return null;
    const s = scale();
    centerFor(rect, s);
    const windowRect = requestWindow(visibleWindow(rect, s));
    const size = requestSize(windowRect);
    return {rect, s, windowRect, size,
      key: `${windowRect.x},${windowRect.y},${windowRect.width},${windowRect.height},${size},${state.layer}`};
  }

  async function load(force) {
    if (disposed) return;
    if (loading) { queued = true; return; }
    const target = wanted();
    if (!target) return;
    if (!force && state.lastKey === target.key && crop) return draw();
    const {windowRect, size} = target;
    loading = true;
    const token = ++loadToken;
    controller?.abort();
    controller = new AbortController();
    const params = {
      x: windowRect.x, y: windowRect.y, width: windowRect.width, height: windowRect.height,
      size, overlay: state.layer === "overlay" ? "1" : "0", r: p.revision,
    };
    try {
      const response = await fetch(ctx.base()+"/views/"+viewId+"/crop?"+qs(params), {signal: controller.signal});
      if (!response.ok) throw new Error("原图细节读取失败。");
      const blob = await response.blob();
      const header = (response.headers.get("X-RimBench-Crop") || "").split(",").map(Number);
      if (header.length < 6 || header.slice(0, 6).some(value => !Number.isFinite(value))) {
        throw new Error("原图细节返回了无效的取景范围。");
      }
      const shown = {x: header[0], y: header[1], width: header[2], height: header[3],
        outW: header[4], outH: header[5]};
      const url = URL.createObjectURL(blob);
      const probe = new Image();
      probe.src = url;
      try {
        await probe.decode();
      } catch {
        URL.revokeObjectURL(url);
        throw new Error("原图细节无法显示。");
      }
      if (disposed || token !== loadToken) {
        URL.revokeObjectURL(url);
        return;
      }
      if (crop?.url) URL.revokeObjectURL(crop.url);
      crop = {...shown, url};
      state.lastKey = target.key;
      state.lastLayer = state.layer;
      baseImage.src = url;
      baseImage.hidden = false;
      empty.hidden = true;
      draw();
    } catch (error) {
      if (error.name === "AbortError" || disposed || token !== loadToken) return;
      empty.hidden = false;
      empty.textContent = error.message;
      cropNote.textContent = error.message;
    } finally {
      if (token === loadToken) {
        loading = false;
        if (queued && !disposed) { queued = false; schedule(SNAP_DELAY); }
      }
    }
  }

  function draw() {
    if (!crop || disposed) return;
    const rect = box();
    if (!rect.width) return;
    const s = scale();
    centerFor(rect, s);
    // The crop usually covers more than the viewport (padding for panning); it
    // is drawn at the requested scale and clipped by the stage. The scaling
    // factor from source pixels to CSS pixels is therefore exact.
    const left = rect.width/2+(crop.x-state.cx)*s;
    const top = rect.height/2+(crop.y-state.cy)*s;
    const shownW = crop.width*s;
    const shownH = crop.height*s;
    const transform = `translate(${left.toFixed(2)}px, ${top.toFixed(2)}px)`;
    baseImage.style.transform = transform;
    baseImage.style.width = shownW+"px";
    baseImage.style.height = shownH+"px";
    // Nearest-neighbour gives a crisp single-pixel look; only do it once a
    // source pixel really covers more than a screen pixel.
    baseImage.classList.toggle("pixelated", s*crop.outW/crop.width >= 1.5);
    for (const layer of [guides, marks]) {
      layer.style.transform = transform;
      layer.style.width = shownW+"px";
      layer.style.height = shownH+"px";
      layer.setAttribute("viewBox", `0 0 ${crop.outW} ${crop.outH}`);
    }
    drawGuides(crop, s);
    scaleNote.textContent = s >= 1 ? f(s, 1)+"×" : f(s*100, 0)+"%";
    const lowered = crop.outW < crop.width || crop.outH < crop.height;
    cropNote.textContent = `取景 ${crop.x}, ${crop.y} · ${crop.width}×${crop.height} px`
      + (lowered ? " · 缩小预览" : "") + (state.layer === "overlay" ? " · 轮廓叠层" : " · 原图");
  }

  function drawGuides(shown, s) {
    if (!shown.outW || !shown.outH) return;
    const kx = shown.outW/shown.width;
    const ky = shown.outH/shown.height;
    const place = ([x, y]) => toCrop(x, y, shown).join(",");
    // Marks keep a readable size on screen: six CSS pixels regardless of zoom.
    const radius = (6/(Math.max(kx, ky)*s)).toFixed(3);
    const parts = [];
    const guideParts = [];
    const data = inspected?.guides;
    if (data?.axis?.axis?.length === 2) {
      const [a, b] = data.axis.axis;
      const cut = clipLine(a, b, shown);
      if (cut) guideParts.push(`<line x1="${cut[0][0]}" y1="${cut[0][1]}" x2="${cut[1][0]}" y2="${cut[1][1]}"
        stroke="#8cf1d3" stroke-width="1.5" stroke-dasharray="6 5" opacity=".8"
        vector-effect="non-scaling-stroke"/>`);
    }
    const section = data?.cross_section;
    if (section?.line?.length === 2) {
      // The section line spans the frame; only its part inside this crop is drawn,
      // but it is clipped from the same original coordinates, so it does not shift.
      const cut = clipLine(section.line[0], section.line[1], shown);
      if (cut) guideParts.push(`<line x1="${cut[0][0]}" y1="${cut[0][1]}" x2="${cut[1][0]}" y2="${cut[1][1]}"
        stroke="#ffd38b" stroke-width="1.2" opacity=".85" vector-effect="non-scaling-stroke"/>`);
      for (const run of section.inside ?? []) {
        if (run.length < 2) continue;
        guideParts.push(`<polyline points="${run.map(place).join(" ")}" fill="none" stroke="#ffd38b"
          stroke-width="3.2" stroke-linecap="round" opacity=".95" vector-effect="non-scaling-stroke"/>`);
      }
    }
    for (const endpoint of data?.endpoints ?? []) {
      if (!nearWindow(endpoint[0], endpoint[1], shown, 2*Math.max(1, s))) continue;
      const [cx, cy] = toCrop(endpoint[0], endpoint[1], shown);
      guideParts.push(`<circle cx="${cx}" cy="${cy}" r="${radius}" fill="#cb7736"
        stroke="#f5fefb" stroke-width="1.4" vector-effect="non-scaling-stroke"/>`);
    }
    if (state.selected) {
      const [rx, ry] = toCrop(state.selected.x, state.selected.y, shown);
      const span = Math.max(kx, ky);
      parts.push(`<rect x="${rx}" y="${ry}" width="${span.toFixed(3)}" height="${span.toFixed(3)}"
        fill="none" stroke="#ffffff" stroke-width="2" vector-effect="non-scaling-stroke"/>`);
    }
    guides.innerHTML = guideParts.join("");
    marks.innerHTML = parts.join("");
  }

  function toCrop(x, y, shown) {
    const kx = shown.outW/shown.width;
    const ky = shown.outH/shown.height;
    return [((x-shown.x)*kx).toFixed(3), ((y-shown.y)*ky).toFixed(3)];
  }

  function nearWindow(x, y, shown, slack) {
    return x >= shown.x-slack && x <= shown.x+shown.width+slack
      && y >= shown.y-slack && y <= shown.y+shown.height+slack;
  }

  function clipLine(a, b, shown) {
    const dx = b[0]-a[0], dy = b[1]-a[1];
    if (!dx && !dy) return null;
    let low = 0, high = 1;
    for (const [start, delta, lowBound, highBound] of [
      [a[0], dx, shown.x, shown.x+shown.width],
      [a[1], dy, shown.y, shown.y+shown.height],
    ]) {
      if (Math.abs(delta) < 1e-9) {
        if (start < lowBound || start > highBound) return null;
        continue;
      }
      const one = (lowBound-start)/delta, two = (highBound-start)/delta;
      low = Math.max(low, Math.min(one, two));
      high = Math.min(high, Math.max(one, two));
      if (high < low) return null;
    }
    return [toCrop(a[0]+dx*low, a[1]+dy*low, shown), toCrop(a[0]+dx*high, a[1]+dy*high, shown)];
  }

  function zoomAt(factor, clientX, clientY) {
    const rect = box();
    const current = scale();
    const fit = fitScale();
    const px = clientX == null ? state.cx : state.cx+(clientX-rect.left-rect.width/2)/current;
    const py = clientY == null ? state.cy : state.cy+(clientY-rect.top-rect.height/2)/current;
    const next = clamp(current*factor, fit, MAX_SCALE);
    state.scale = next <= fit*(1+1e-4) ? 0 : next;
    const applied = state.scale || fit;
    if (clientX != null) {
      state.cx = px-(clientX-rect.left-rect.width/2)/applied;
      state.cy = py-(clientY-rect.top-rect.height/2)/applied;
    }
    centerFor(rect, applied);
    draw();
    schedule();
  }

  function setScale(next) {
    const rect = box();
    const fit = fitScale();
    const target = clamp(next, fit, MAX_SCALE);
    state.scale = target <= fit*(1+1e-4) ? 0 : target;
    centerFor(rect, state.scale || fit);
    draw();
    schedule(SNAP_DELAY);
  }

  function setMode(mode) {
    const layer = mode === "normalized" ? "original" : mode;
    if (state.layer === layer) return;
    state.layer = layer;
    // Only the crop layer changes here. The window stays where the operator put
    // it, so the same original pixels remain in view across the switch.
    root.querySelectorAll("[data-detail-mode]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.detailMode === layer));
    });
    schedule(SNAP_DELAY);
  }

  function describe(pixel, coordinate) {
    const parts = [`<b>原图 (${pixel.x}, ${pixel.y}) px</b>`];
    if (pixel.mode === "rgb8") {
      const {r, g, b} = pixel.value;
      parts.push(`颜色 RGB(${r}, ${g}, ${b})`);
    } else {
      const limit = pixel.bits === 16 ? 65535 : 255;
      parts.push(`灰度 ${pixel.value.level} / ${limit}${pixel.bits === 16 ? "（16 位原始值）" : ""}`);
    }
    parts.push(pixel.mask
      ? (pixel.contour ? "属于轮廓边像素" : "属于轮廓内部")
      : "不属于轮廓（背景）");
    if (coordinate) parts.push(`径向 ${f(coordinate.radial_mm)} mm · 高度 ${f(coordinate.z_mm)} mm`);
    return parts.join(" · ");
  }

  function select(px, py) {
    if (px < 0 || py < 0 || px >= width || py >= height) return;
    const token = ++selectToken;
    state.selected = {x: px, y: py};
    readout.textContent = `原图 (${px}, ${py}) px · 正在读取数值…`;
    draw();
    Promise.allSettled([
      api(ctx.base()+"/views/"+viewId+"/pixel?"+qs({x: px, y: py})),
      api(ctx.base()+"/views/"+viewId+"/coordinate?"+qs({x: px, y: py})),
    ]).then(results => {
      if (disposed || token !== selectToken) return;
      if (!state.selected || state.selected.x !== px || state.selected.y !== py) return;
      const pixel = results[0].status === "fulfilled" ? results[0].value : null;
      const coordinate = results[1].status === "fulfilled" ? results[1].value : null;
      readout.innerHTML = pixel ? describe(pixel, coordinate)
        : `原图 (${px}, ${py}) px · 数值读取失败。`;
    });
  }

  function pointFrom(event) {
    const rect = box();
    const s = scale();
    return {
      x: Math.floor(state.cx+(event.clientX-rect.left-rect.width/2)/s),
      y: Math.floor(state.cy+(event.clientY-rect.top-rect.height/2)/s),
    };
  }

  function onWheel(event) {
    event.preventDefault();
    zoomAt(Math.exp(-event.deltaY*0.0016), event.clientX, event.clientY);
  }

  function onPointerDown(event) {
    if (event.button !== 0) return;
    // Keep the picture on screen while dragging so a drag never turns into a
    // pixel pick from a browser text/image selection.
    event.preventDefault();
    stage.setPointerCapture(event.pointerId);
    drag = {x: event.clientX, y: event.clientY, cx: state.cx, cy: state.cy, moved: 0, pointer: event.pointerId};
    stage.classList.add("dragging");
  }

  function onPointerMove(event) {
    if (!drag || drag.pointer !== event.pointerId) return;
    const dx = event.clientX-drag.x;
    const dy = event.clientY-drag.y;
    drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
    if (drag.moved < 3) return;
    const s = scale();
    const rect = box();
    state.cx = drag.cx-dx/s;
    state.cy = drag.cy-dy/s;
    centerFor(rect, s);
    draw();
    schedule();
  }

  function onPointerUp(event) {
    if (!drag || drag.pointer !== event.pointerId) return;
    const moved = drag.moved;
    drag = null;
    stage.classList.remove("dragging");
    if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
    if (moved < 3) {
      const {x, y} = pointFrom(event);
      select(x, y);
    }
    schedule(SNAP_DELAY);
  }

  function onKeyDown(event) {
    const rect = box();
    const s = scale();
    const step = 60/s;
    const actions = {
      ArrowLeft: () => { state.cx -= step; },
      ArrowRight: () => { state.cx += step; },
      ArrowUp: () => { state.cy -= step; },
      ArrowDown: () => { state.cy += step; },
      "+": () => zoomAt(1.4),
      "=": () => zoomAt(1.4),
      "-": () => zoomAt(1/1.4),
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    clearTimeout(timer);
    action();
    centerFor(rect, scale());
    draw();
    schedule();
  }

  root.querySelectorAll("[data-detail-mode]").forEach(button => {
    button.onclick = () => setMode(button.dataset.detailMode);
  });
  root.querySelectorAll("[data-detail]").forEach(button => {
    button.onclick = () => {
      const action = button.dataset.detail;
      if (action === "in") zoomAt(1.5);
      else if (action === "out") zoomAt(1/1.5);
      else if (action === "one") setScale(1);
      else { state.scale = 0; draw(); schedule(SNAP_DELAY); }
    };
  });
  stage.addEventListener("wheel", onWheel, {passive: false});
  stage.addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerUp);
  stage.addEventListener("pointercancel", onPointerUp);
  stage.addEventListener("keydown", onKeyDown);

  if ("ResizeObserver" in window) {
    observer = new ResizeObserver(() => {
      if (disposed) return;
      draw();
      schedule();
    });
    observer.observe(stage);
  }

  function dispose() {
    disposed = true;
    loadToken++;
    selectToken++;
    clearTimeout(timer);
    controller?.abort();
    observer?.disconnect();
    if (crop?.url) URL.revokeObjectURL(crop.url);
    crop = null;
    state.lastKey = null;
  }

  function focus(px, py) {
    if (disposed) return;
    state.cx = clamp(px+0.5, 0, width);
    state.cy = clamp(py+0.5, 0, height);
    if (!state.scale) state.scale = Math.max(1, fitScale());
    const rect = box();
    centerFor(rect, scale());
    draw();
    schedule(SNAP_DELAY);
    stage.focus({preventScroll: true});
    select(px, py);
  }

  // Each mount owns a fresh blob URL, so a previous crop cannot be reused. The
  // position, zoom and layer survive in shared state, so the same original
  // pixels come back at the same place.
  baseImage.hidden = true;
  guides.innerHTML = "";
  marks.innerHTML = "";
  empty.hidden = false;
  empty.textContent = "正在读取原图细节…";
  draw();
  load();
  registry.current = {dispose, focus, setMode};
  return registry.current;
}

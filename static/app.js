import {$, $$, api, dialog, esc, f, field, formObject, guarded, panel, pill, qs, select, span, table, toast, metrics} from "./ui.js";
import {axialPlot, cards, observationOverlay, sectionPlot} from "./plots.js";
import {detailMarkup, mountDetail} from "./detail.js";
import {captureTab} from "./capture.js";
import {inspectionTab} from "./inspection.js";
import {gaugesTab, recordsTab} from "./records.js";

const active = {
  project:null, list:[], view:null, tab:"measure", z:22, direction:0, imageMode:"overlay",
  overview:null, measure:null, curve:null, profile:null, light:null, snapshot:null,
  request:0,
};
const detail = {project:null, view:null, scale:0, cx:0, cy:0, selected:null, lastKey:null, lastLayer:null, layer:"overlay"};
const work = $("#work-panel");
const base = () => "/api/projects/"+active.project.id;
const args = () => qs({z:active.z, direction:active.direction});
const context = {active, work, base, args, refresh, render, update, navigate, measureAt, detail, setImageMode};
let detailController = null;

async function list() {
  const data = await api("/api/projects");
  active.list = data.items;
  $("#project-list").innerHTML = data.items.length ? data.items.map(p=>`<button type="button" class="project-item ${p.id===active.project?.id?"active":""}" data-project="${p.id}">
      ${esc(p.title)}<span>${esc(p.station || "未分配测站")} · ${p.view_count} 张</span></button>`).join("") :
      '<div class="muted" style="padding:9px">还没有检验样件</div>';
  $("#station-summary").innerHTML = data.stations.map(row=>`<p>${esc(row.station)} · ${row.projects} 个样件</p>`).join("");
  $$("[data-project]").forEach(button=>button.addEventListener("click",guarded(()=>openProject(button.dataset.project))));
}

async function openProject(identity, already) {
  const seq = ++active.request;
  const project = already || await api("/api/projects/"+identity);
  if (seq !== active.request) return;
  active.project = project;
  active.view = project.views.find(row=>row.included)?.id || project.views[0]?.id;
  active.tab = project.views.length ? "measure" : "capture";
  active.overview = null;
  active.measure = null;
  active.profile = null;
  active.curve = null;
  active.snapshot = null;
  active.light = null;
  active.z = 22;
  active.direction = 0;
  localStorage.setItem("rimbench.active",identity);
  if (project.views.length) {
    const overview = await api(base()+"/overview");
    if (seq !== active.request) return;
    active.overview = overview;
    const range = overview.suggested;
    if (range.z != null) {
      active.z = Number(f(range.z));
      active.profile = {lower:f(range.lower),upper:f(range.upper),count:31,metric:"width",direction:0};
    }
  }
  detail.cx = project.width/2 || 0;
  detail.cy = project.height/2 || 0;
  detail.scale = 0;
  detail.selected = null;
  detail.lastKey = null;
  detail.lastLayer = null;
  detail.view = null;
  header();
  await Promise.all([list(),render()]);
}

function header() {
  const p = active.project;
  $("#welcome").hidden = !!p;
  $("#project-workspace").hidden = !p;
  if (!p) return;
  $("#project-title").textContent = p.title;
  $("#project-meta").innerHTML = `<span>${esc(p.station || "未分配测站")}</span><span>${p.views.length} 张影像</span>
    <span>版本 ${p.revision}</span>${p.sample?'<span class="pill good">合成演示</span>':""}`;
  $("#bundle-download").href = base()+"/bundle";
}

async function update(path, body, method="PATCH") {
  const result = await api(base()+path,{method,body:{...body,revision:active.project.revision}});
  await refresh(result.project || result);
}

async function refresh(project) {
  active.project = project || await api(base());
  active.measure = null;
  active.curve = null;
  active.light = null;
  if (!active.project.views.some(row=>row.id===active.view)) {
    active.view = active.project.views[0]?.id;
    detail.selected = null;
    detail.view = null;
  }
  if (active.project.views.length) {
    active.overview = await api(base()+"/overview");
    const range = active.overview.suggested;
    if (!active.profile && range.z != null) {
      active.z = Number(f(range.z));
      active.profile = {lower:f(range.lower),upper:f(range.upper),count:31,metric:"width",direction:active.direction};
    }
  } else active.overview = null;
  header();
  await Promise.all([list(),render()]);
}

function projectEditor(create) {
  const p = create ? {} : active.project;
  const markup = `<div class="form-grid">${field("title","样件名称",p.title,{type:"text"})}
    ${field("station","测站",p.station,{type:"text",required:false})}
    ${field("operator","检验人",p.operator,{type:"text",required:false})}</div>`;
  return dialog(create?"新建检验样件":"编辑样件信息",markup,async form => {
    const inputs = Object.fromEntries(form);
    if (create) {
      const out = await api("/api/projects",{method:"POST",body:inputs});
      await openProject(out.id,out);
      toast("样件已创建");
    } else {
      await update("",inputs);
      toast("样件信息已保存");
    }
  });
}

async function navigate(tab) {
  active.tab = tab;
  await render();
}

async function render() {
  if (!active.project) return;
  const seq = ++active.request;
  $$("[data-tab]").forEach(button=>{
    const here = button.dataset.tab === active.tab;
    button.classList.toggle("active",here);
    if (here) button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  });
  if (active.tab === "capture") return captureTab(context, () => seq === active.request);
  if (active.tab === "inspection") return inspectionTab(context, () => seq === active.request);
  if (active.tab === "gauges") return gaugesTab(context, () => seq === active.request);
  if (active.tab === "records") return recordsTab(context, () => seq === active.request);
  return measureTab(seq);
}

async function measureAt(z, direction = active.direction) {
  active.z = Number(z);
  active.direction = Number(direction);
  active.measure = null;
  await navigate("measure");
}

async function measureTab(seq) {
  const p = active.project;
  if (!p.views.length) {
    work.innerHTML = '<div class="notice">请先在采集与标定中添加角度影像。</div>';
    return;
  }
  if (!active.measure) work.innerHTML = '<div class="notice">正在计算投影支撑…</div>';
  const [measured, inspected] = await Promise.all([
    api(base()+"/section?"+args()),
    active.view ? api(base()+"/views/"+active.view+"?"+args()) : null,
  ]);
  if (seq !== active.request) return;
  if (measured.revision !== p.revision) {
    work.innerHTML = '<div class="notice warning">当前样件已变更，请重新载入样件。</div>';
    return;
  }
  const s = measured.section;
  active.measure = s;
  const image = active.view ? base()+"/views/"+active.view+"/image" : "";
  const q = args();
  const angleList = p.views.map(v=>[v.id,`${v.angle}° · ${v.title}${v.included?"":" · 已排除"}`]);
  const viewSelect = '<label class="field">查看角度<select id="inspect-angle">'+angleList.map(([key,value])=>
    `<option value="${key}" ${key===active.view?"selected":""}>${esc(value)}</option>`).join("")+'</select></label>';
  work.innerHTML = `<form id="section-form" class="panel controls">
    ${field("z","截面高度 · mm",active.z)}${field("direction","测量方向 · °",active.direction)}
    <button type="submit" class="primary">计算截面</button><div class="action-spacer"></div>
    <a class="button quiet" href="${base()}/section.csv?${q}">角度 CSV</a>
    <a class="button quiet" href="${base()}/section.svg?${q}&print=1">截面 SVG</a>
    <a class="button quiet" target="_blank" rel="noopener" href="${base()}/report?${q}">检验报告 ↗</a>
    </form>
    <div class="section-grid">
      ${panel("角度影像",pill(inspected.observation.state),`
        <div class="image-toolbar">${viewSelect}
          ${select("image-mode","预览图层",{overlay:"原图与轮廓",original:"原图",normalized:"归一图"},active.imageMode)}
          <span class="toolbar-note">概览用于定位；下方细览按需读取原图，可放大到单个像素。</span></div>
        <div class="section-media">
          <div class="visual-frame cursor" style="--aspect:${p.width}/${p.height}">
          <img class="camera" id="measure-photo" src="${image}?mode=${active.imageMode==="normalized"?"normalized":"original"}&v=${p.revision}" alt="所选角度计量原图">
          <img class="image-overlay" id="measure-overlay" src="${image}?mode=overlay&size=1100&v=${p.revision}" alt="" ${active.imageMode==="overlay"?"":"hidden"}>
          ${observationOverlay(inspected,active.overview,active.z,p.width,p.height)}</div>
          ${detailMarkup(active)}
        </div>
        <div class="status-line" id="pixel-location">${esc(inspected.observation.reason)}</div>`)}
      ${panel("重建截面",pill(s.state),`<div class="chart-frame">${sectionPlot(s,active.view)}</div>
        <div class="chart-legend"><span><i class="legend-line"></i>标称轮廓</span><span><i class="legend-line outer"></i>外包络</span>
        <span><i class="legend-line inner"></i>内包络</span><span>${s.usable_views} / ${p.views.length} 个有效角度</span></div>${cards(s)}`)}
    </div>
    <div class="section-grid spaced">
      ${panel("轴向尺寸曲线","",`<div class="panel-body">${profileForm()}</div><div id="axial-plot" class="chart-frame">${axialPlot(active.curve,active.z)}</div>
      <div id="profile-summary" class="status-line">${curveSummary()}</div>`)}
      ${panel("支撑证据",`<span class="subtext">最大法线缺口 ${f(s.normal_coverage.gap,1)}°</span>`,evidenceTable(s))}
    </div>`;
  bindMeasurement();
  detailController = mountDetail(context, inspected);
}

function setImageMode(mode) {
  const value = mode === "normalized" ? "normalized" : mode;
  if (active.imageMode === value) return;
  active.imageMode = value;
  // The detail view keeps its own two-layer switch; keep it on the same layer.
  detail.layer = value === "normalized" ? "original" : value;
  const selector = $('[name="image-mode"]');
  if (selector) selector.value = value;
  $$('[data-detail-mode]').forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.detailMode === detail.layer));
  });
  // The overview is part of the same view, so it is reloaded rather than
  // rebuilding the whole panel — this keeps the detail position on screen.
  const photo = $("#measure-photo");
  const overlay = $("#measure-overlay");
  const display = value === "normalized" ? "normalized" : "original";
  if (photo) photo.src = base()+"/views/"+active.view+"/image?mode="+display+"&v="+active.project.revision;
  if (overlay) overlay.hidden = value !== "overlay";
}

function profileForm() {
  const values = active.profile || {lower:0,upper:42,count:31,metric:"width",direction:active.direction};
  return `<form class="inline-form" id="profile-form">${field("lower","起始 · mm",values.lower)}
    ${field("upper","结束 · mm",values.upper)}${field("count","采样点",values.count,{min:2,max:121})}
    ${select("metric","曲线量值",metrics,values.metric)}
    <button type="submit">生成曲线</button><span id="profile-download">${profileDownload()}</span></form>`;
}

function profileDownload() {
  return active.curve ? `<a class="button quiet" href="${base()}/profile.csv?${qs(active.curve.parameters)}">CSV</a>` : "";
}

function curveSummary() {
  const r = active.curve?.summary;
  return r ? `${r.measured}/${r.total} 层有效 · 极差 ${f(r.variation)} ${active.curve.units} · ${r.missing_ranges.length} 处缺测区` : "点击曲线上的取样点可联动截面与角度影像。";
}

function evidenceTable(s) {
  const support = new Set(s.active_support.map(edge=>edge.view_id));
  return table(["角度","证据状态","左端 / 右端 · mm","活跃"], s.observations.map(obs=>({
    active:obs.view_id===active.view,
    items:[`<button type="button" data-observation="${obs.view_id}">${f(obs.angle,1)}°</button>`,pill(obs.state),
      obs.lower != null ? `${f(obs.lower)} / ${f(obs.upper)}`:"—",support.has(obs.view_id)?"●":"—"],
  })));
}

function bindMeasurement() {
  $("#section-form").onsubmit = guarded(event=>{
    const values = formObject(event.currentTarget);
    return measureAt(values.z,values.direction);
  });
  $("#inspect-angle").onchange = guarded(event=>{
    active.view = event.currentTarget.value;
    detail.selected = null;
    detail.view = null;
    return render();
  });
  $('[name="image-mode"]').onchange = guarded(event=>setImageMode(event.currentTarget.value));
  $$("[data-observation]").forEach(button=>button.onclick=guarded(()=>{active.view=button.dataset.observation; return render();}));
  $("#profile-form").onsubmit = guarded(async event=>{
    const params = {...formObject(event.currentTarget),direction:active.direction};
    const projectID = active.project.id;
    const curve = await api(base()+"/profile?"+qs(params));
    if (projectID !== active.project.id || active.tab !== "measure" || curve.revision !== active.project.revision) return;
    active.profile = params;
    active.curve = curve;
    $("#axial-plot").innerHTML = axialPlot(curve,active.z);
    $("#profile-summary").textContent = curveSummary();
    $("#profile-download").innerHTML = profileDownload();
    bindSamples();
  });
  $("#measure-photo").onclick = guarded(async event=>{
    const img = event.currentTarget;
    const rect = img.getBoundingClientRect();
    const {width,height} = active.project;
    const zoom = Math.min(rect.width/width,rect.height/height);
    const x = Math.floor((event.clientX-rect.left-(rect.width-zoom*width)/2)/zoom);
    const y = Math.floor((event.clientY-rect.top-(rect.height-zoom*height)/2)/zoom);
    if (x<0 || y<0 || x>width-1 || y>height-1) return;
    // Recentre the detail view on the overview click and let it report the pixel.
    detailController?.focus(x, y);
    const click = await api(base()+"/views/"+active.view+"/coordinate?"+qs({x,y}));
    const location = $("#pixel-location");
    if (location) location.textContent = `原图 (${x}, ${y}) px · 径向 ${f(click.radial_mm)} mm · 高度 ${f(click.z_mm)} mm`;
  });
  bindSamples();
}

function bindSamples() {
  $$("[data-sample-z]").forEach(dot=>{
    const go = guarded(()=>measureAt(dot.dataset.sampleZ));
    dot.onclick = go;
    dot.onkeydown = event=>{if (event.key==="Enter" || event.key===" ") go(event);};
  });
}

$$("[data-demo]").forEach(button=>button.onclick=guarded(async ()=>{
  const value = await api("/api/demos",{method:"POST",body:{kind:button.dataset.demo}});
  await openProject(value.id,value);
  toast("合成样件已载入");
}));
$$("[data-tab]").forEach(button=>button.onclick=guarded(()=>navigate(button.dataset.tab)));
$("#new-project").onclick = $("#welcome-create").onclick = ()=>projectEditor(true);
$("#project-edit").onclick = ()=>projectEditor(false);
$("#load-bundle").onclick = ()=>dialog("导入样件交换包",`<div class="form-grid">${field("bundle","RimBench ZIP",null,{type:"file",accept:".zip"})}
  ${field("title","新名称（可选）","",{type:"text",required:false})}</div>`,async body=>{
    const p = await api("/api/projects/import",{method:"POST",body});
    await openProject(p.id,p);
    toast("已导入样件及原始影像");
  },"导入");

async function initialize() {
  const health = $("#health");
  try {
    await api("/api/health");
    health.classList.add("ok");
    health.innerHTML = "<i></i>工作站已连接";
    await list();
    const old = localStorage.getItem("rimbench.active");
    if (active.list.some(row=>row.id===old)) await openProject(old);
  } catch (error) {
    health.innerHTML = "<i></i>连接异常";
    toast(error.message,true);
  }
}
initialize();

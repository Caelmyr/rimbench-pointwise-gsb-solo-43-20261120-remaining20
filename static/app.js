import {$, $$, api, dialog, esc, f, field, formObject, guarded, panel, pill, qs, select, span, table, toast, metrics} from "./ui.js";
import {axialPlot, cards, observationOverlay, sectionPlot} from "./plots.js";
import {captureTab} from "./capture.js";
import {inspectionTab} from "./inspection.js";
import {gaugesTab, recordsTab} from "./records.js";

const active = {
  project:null, list:[], view:null, tab:"measure", z:22, direction:0, imageMode:"overlay",
  overview:null, measure:null, curve:null, profile:null, light:null, snapshot:null,
  request:0,
};
const work = $("#work-panel");
const base = () => "/api/projects/"+active.project.id;
const args = () => qs({z:active.z, direction:active.direction});
const context = {active, work, base, args, refresh, render, update, navigate, measureAt};

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
  if (!active.project.views.some(row=>row.id===active.view)) active.view = active.project.views[0]?.id;
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
        <div class="image-toolbar">${viewSelect}${select("image-mode","图层",{overlay:"原图与轮廓",original:"原图",normalized:"归一图"},active.imageMode)}</div>
        <div class="visual-frame cursor" style="--aspect:${p.width}/${p.height}">
        <img class="camera" id="measure-photo" src="${image}?mode=${active.imageMode==="normalized"?"normalized":"original"}&v=${p.revision}" alt="所选角度计量原图">
        ${active.imageMode==="overlay"?`<img class="image-overlay" src="${image}?mode=overlay&v=${p.revision}" alt="">`:""}
        ${observationOverlay(inspected,active.overview,active.z,p.width,p.height)}</div>
        <div class="status-line" id="pixel-location">${esc(inspected.observation.reason)}</div>`)}
      ${panel("重建截面",pill(s.state),`<div class="chart-frame">${sectionPlot(s,active.view)}</div>
        <div class="chart-legend"><span><i class="legend-line"></i>标称轮廓</span><span><i class="legend-line outer"></i>外包络</span>
        <span><i class="legend-line inner"></i>内包络</span><span>${s.usable_views} / ${p.views.length} 个有效角度</span></div>${cards(s)}`)}
    </div>
    <div class="section-grid spaced">
      ${panel("轴向尺寸曲线","",`<div class="panel-body">${profileForm()}</div><div id="axial-plot" class="chart-frame">${axialPlot(active.curve,active.z)}</div>
      <div class="chart-legend"><span><i class="legend-step used"></i>两侧稳定段</span><span><i class="legend-step value"></i>测值区间</span>
      <span><i class="legend-step spacing"></i>采样间隔允许范围</span><span><i class="legend-step mark"></i>肩位</span><span><i class="legend-step review"></i>需补测</span></div>
      <div id="profile-summary" class="status-line">${curveSummary()}</div><div id="step-summary">${stepSummary()}</div>`)}
      ${panel("支撑证据",`<span class="subtext">最大法线缺口 ${f(s.normal_coverage.gap,1)}°</span>`,evidenceTable(s))}
    </div>`;
  bindMeasurement();
}

function profileForm() {
  const values = active.profile || {lower:0,upper:42,count:31,metric:"width",direction:active.direction};
  return `<form class="inline-form" id="profile-form">${field("lower","起始 · mm",values.lower)}
    ${field("upper","结束 · mm",values.upper)}${field("count","采样点",values.count,{min:2,max:121})}
    ${select("metric","曲线量值",metrics,values.metric)}
    <button type="submit">生成曲线</button><span id="profile-download">${profileDownload()}</span></form>`;
}

function profileDownload() {
  if (!active.curve) return "";
  const query = qs(active.curve.parameters);
  return `<a class="button quiet" href="${base()}/profile.csv?${query}">CSV</a>
    <a class="button quiet" href="${base()}/profile.csv?${query}&part=steps">肩位 CSV</a>`;
}

function curveSummary() {
  const r = active.curve?.summary;
  return r ? `${r.measured}/${r.total} 层有效 · 极差 ${f(r.variation)} ${active.curve.units} · ${r.missing_ranges.length} 处缺测区` : "点击曲线上的取样点可联动截面与角度影像。";
}

const stepLabels = {measured:"已认定", ambiguous:"需补测", missing:"缺测", rejected:"不成台阶", "no-step":"无台阶", "review-required":"需补测", partial:"部分认定", "not-applicable":"不适用", insufficient:"采样不足", "no-curve":"无曲线"};
const stepPill = state => `<span class="pill ${state==="review-required"||state==="ambiguous"?"warn":state==="measured"||state==="partial"?"good":state==="missing"?"bad":""}">${esc(stepLabels[state] || state)}</span>`;

function stepSummary() {
  const steps = active.curve?.steps;
  if (!steps) return "";
  const spacing = steps.sample_spacing;
  const parts = [`<div class="status-line step-line">${stepPill(steps.state)} <span>${esc(steps.detail)}</span>
    <span class="subtext">采样间隔 ${f(spacing?.spacing,3)} mm · 可区分分辨率 ${f(steps.resolution,3)} ${esc(active.curve.units)}</span></div>`];
  if (steps.shoulders.length) {
    parts.push(`<div class="data-wrap"><table><thead><tr><th scope="col">肩位</th><th scope="col">状态</th><th scope="col">高度 / mm</th><th scope="col">测值区间 / mm</th><th scope="col">采样间隔允许范围 / mm</th><th scope="col">两侧稳定段</th><th scope="col">台阶高度</th></tr></thead><tbody>
      ${steps.shoulders.map(row=>`<tr><td>${row.id+1}</td><td>${stepPill(row.state)}</td><td>${row.z==null?"—":f(row.z,3)}</td>
        <td>${span(row.interval)}</td><td>${span(row.spacing_range)}</td>
        <td class="subtext">${segmentSpan(steps,row.left.segment)} ‖ ${segmentSpan(steps,row.right.segment)}</td>
        <td>${row.step?f(row.step.height,3):"—"}</td></tr>`).join("")}</tbody></table></div>`);
  }
  if (steps.pairs.length) {
    parts.push(`<div class="data-wrap"><table><thead><tr><th scope="col">肩距</th><th scope="col">距离 / mm</th><th scope="col">测值区间 / mm</th><th scope="col">采样间隔允许范围 / mm</th></tr></thead><tbody>
      ${steps.pairs.map(row=>`<tr><td>肩 ${row.first+1} → ${row.second+1}</td><td>${f(row.distance,3)}</td><td>${span(row.interval)}</td><td>${span(row.spacing_range)}</td></tr>`).join("")}</tbody></table></div>`);
  }
  if (steps.review_spans.length) {
    parts.push(`<div class="panel-body step-review">${steps.review_spans.map(row=>`<p class="notice ${row.kind==="dense"?"warning":""}">
      <b>${row.kind==="dense"?"需补测":"缺测保留"}</b> · ${f(row.start_z,2)}–${f(row.end_z,2)} mm${row.suggested_spacing?` · 建议采样间隔 ${f(row.suggested_spacing,2)} mm`:""} · ${esc(row.detail)}</p>`).join("")}</div>`);
  }
  return parts.join("");
}

function segmentSpan(steps, identity) {
  const segment = steps.segments.find(row=>row.id===identity);
  return segment ? `${f(segment.start_z,2)}–${f(segment.end_z,2)} mm` : "—";
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
  $("#inspect-angle").onchange = guarded(event=>{active.view=event.currentTarget.value; return render();});
  $('[name="image-mode"]').onchange = guarded(event=>{active.imageMode=event.currentTarget.value; return render();});
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
    $("#step-summary").innerHTML = stepSummary();
    $("#profile-download").innerHTML = profileDownload();
    bindSamples();
  });
  $("#measure-photo").onclick = guarded(async event=>{
    const img = event.currentTarget;
    const rect = img.getBoundingClientRect();
    const {width,height} = active.project;
    const zoom = Math.min(rect.width/width,rect.height/height);
    const x = (event.clientX-rect.left-(rect.width-zoom*width)/2)/zoom;
    const y = (event.clientY-rect.top-(rect.height-zoom*height)/2)/zoom;
    if (x<0 || y<0 || x>width-1 || y>height-1) return;
    const click = await api(base()+"/views/"+active.view+"/coordinate?"+qs({x,y}));
    const location = $("#pixel-location");
    if (location) location.textContent = `原图 (${f(x,1)}, ${f(y,1)}) px · 径向 ${f(click.radial_mm)} mm · 高度 ${f(click.z_mm)} mm`;
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

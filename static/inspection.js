import {$, $$, api, dialog, esc, f, field, formObject, guarded, panel, pill, qs, select, stateLabels, toast, table} from "./ui.js";
import {axialPlot, sectionPlot} from "./plots.js";

const procedures = {
  round: ["截面圆拟合", "从有效截面的凸轮廓读取基准圆、径向残差和指定圆心的极差。"],
  form: ["轴向形位", "使用转台原点或另选基准段，比较被测段的圆心轴线与径向极差。"],
  clearance: ["套环包络", "沿所选高度检查圆形套环与凸轮廓的最小径向余量。"],
  lobes: ["周向形状", "跟踪所选周向周期的起伏幅值和轴向相位。"],
  edges: ["成像边缘", "查看左右边缘的亮度过渡、反差和过渡带宽度。"],
  repeat: ["投影复核", "将同向或相反方向的独立采集转换到同一物理坐标，比较两端。"],
  field: ["光场复检", "用当前空场参考比较新拍空场，显示整体亮度变化和局部异常。"],
  grid: ["九点标板", "从已知点距的平面标板检查工位像素比例、几何残差和成像清晰度。"],
};

const labels = {
  checked:"检查完成", complete:"覆盖全部抽样", partial:"部分抽样有效", spindle:"转台轴", selected:"基准段拟合",
  "no-fit":"拟合不稳定", "datum-outside":"基准在轮廓外", "datum-insufficient":"基准覆盖不足",
  "no-datum":"缺少基准", "no-contour":"缺少有效轮廓", available:"可读",
  fits:"满足余量", interference:"轮廓干涉", "insufficient-gap":"余量不足", "uncertain-gap":"余量交叠",
  "no-measurement":"截面缺测", undersampled:"角度抽样不足", "phase-weak":"相位信号不足",
  within:"落在复核阈值内", exceeds:"超出复核阈值", overlaps:"差值区间跨过阈值",
  "first-missing":"第一张缺测", "second-missing":"第二张缺测", "both-missing":"两张缺测",
  stable:"光场稳定", "global-gain":"整幅亮度偏移", "localized-drift":"局部异常", insufficient:"有效数据不足",
  dimmer:"相对变暗", brighter:"相对变亮", found:"已读圆心", "wide-edge":"过渡带过宽",
  "no-target":"圆点不可读", "dot-too-small":"靶像过小", "insufficient-spokes":"有效径向样本不足",
  "outside-window":"取样越界", "unlit-field":"参考空场过暗", "clipped-tone":"亮度截幅",
  "uneven-plateau":"背景或暗部不均", "low-contrast":"反差不足", "no-transition":"缺少过渡",
  "incomplete-transition":"只读到部分过渡", "unstable-transition":"过渡不唯一",
  "current-revision":"当前版本", "earlier-revision":"历史版本", "legacy-method":"旧版计算方法",
};

const cornerNames = {top_left:"左上",top_right:"右上",bottom_left:"左下",bottom_right:"右下"};
const quadrantNames = {"top-left":"左上","top-right":"右上","bottom-left":"左下","bottom-right":"右下"};
const uploadKinds = new Set(["field", "grid"]);
const store = {id:null, kind:"round", values:{}, live:null, archive:null, upload:{}, urls:[]};
const textState = value => labels[value] || stateLabels[value] || value || "—";
const resultState = value => labels[value] ? `<span class="pill ${esc(value==="review"?"warn":value)}">${esc(labels[value])}</span>` : pill(value);
const angle = value => value == null ? "—" : f(value, 2)+"°";
const percent = value => value == null ? "—" : f(value*100, 2)+"%";
const mm = value => value == null ? "—" : f(value, 3)+" mm";
const px = value => value == null ? "—" : f(value, 3)+" px";
const point = point => point ? `${f(point[0], 3)}, ${f(point[1], 3)}` : "—";
const rows = (heads, items) => table(heads, items.map(items=>({items})));
const pos = z => `<button type="button" class="small" data-audit-z="${z}">${f(z,2)} mm</button>`;
const cards = entries => `<div class="metric-strip audit-metrics">${entries.map(([label,value]) =>
  `<div class="metric-card"><span class="title">${esc(label)}</span><b>${esc(value)}</b></div>`).join("")}</div>`;

function releaseUrls() {
  store.urls.forEach(url=>URL.revokeObjectURL(url));
  store.urls = [];
}

function localUrl(blob) {
  const url = URL.createObjectURL(blob);
  store.urls.push(url);
  return url;
}

function setupDefaults(active) {
  const suggested = active.overview?.suggested;
  const z = active.z;
  const low = suggested?.lower ?? z-10;
  const high = suggested?.upper ?? z+10;
  const width = active.project.width || 0;
  const height = active.project.height || 0;
  return {
    round:{z,datum_x:0,datum_y:0},
    form:{lower:f(low,2),upper:f(high,2),count:15,datum:"spindle",datum_lower:f(low,2),datum_upper:f(low+(high-low)/4,2),datum_count:7},
    clearance:{lower:f(low,2),upper:f(high,2),count:21,center_x:0,center_y:0,bore:36,minimum_gap:0.15},
    lobes:{lower:f(low,2),upper:f(high,2),count:21,order:2,amplitude_floor:0.03},
    edges:{z,radius:8,view:""},
    repeat:{lower:f(low,2),upper:f(high,2),count:25,pair:"",angle_tolerance:0.3,alert_mm:0.3},
    field:{x:0,y:0,width,height,drift_limit:0.045,visible_fraction:0.5,minimum_region:16},
    grid:{
      top_left_x:f(width*.25),top_left_y:f(height*.25),top_right_x:f(width*.75),top_right_y:f(height*.25),
      bottom_left_x:f(width*.25),bottom_left_y:f(height*.75),bottom_right_x:f(width*.75),bottom_right_y:f(height*.75),
      pitch_x:10,pitch_y:10,radius:12,expected:f(active.overview?.axis.frame.px_per_mm,4),
      scale_limit:0.015,residual_limit:0.35,focus_limit:2.8,
    },
  };
}

export async function inspectionTab(ctx, current) {
  const {active, base, work} = ctx;
  if (active.project.id !== store.id) {
    releaseUrls();
    Object.assign(store,{id:active.project.id,kind:"round",values:setupDefaults(active),live:null,archive:null,upload:{}});
  }
  const root = base()+"/inspection";
  const records = await api(root+"/records");
  if (!current()) return;
  if (store.archive && store.archive.revision_status.current !== active.project.revision) {
    const updated = await api(root+"/records/"+store.archive.summary.id);
    if (!current()) return;
    store.archive = updated;
  }
  if (!active.project.views.length) {
    work.innerHTML = panel("形位与质检","",`<div class="panel-body"><p>导入影像并完成轴与比例标定后，可以进行形位与质检。</p>
      <button type="button" id="audit-capture">转到采集与标定</button></div>`);
    $("#audit-capture").onclick = ()=>ctx.navigate("capture");
    return;
  }
  const pairs = store.kind === "repeat" ? (await api(root+"/pairs")).pairs : null;
  if (!current()) return;
  const archived = store.archive;
  const source = archived?.summary.kind || store.kind;
  const live = store.live?.revision===active.project.revision && store.live.kind===store.kind ? store.live : null;
  work.innerHTML = `<div class="stack">
    ${panel("形位与质检","",`<div class="panel-body"><nav class="audit-choices" aria-label="核验项目">
      ${Object.entries(procedures).map(([key,[label]])=>`<button type="button" data-inspection="${key}" aria-pressed="${store.kind===key}">${label}</button>`).join("")}
      </nav><p class="subtext">${procedures[store.kind][1]}</p>${requestForm(store.kind,ctx,pairs)}</div>`)}
    <div id="audit-result" tabindex="-1">${archived ? outputPanel(source, archived.result, archiveActions(root, archived), archived) : live ? outputPanel(source, live.result, liveActions(root, live)) : ""}</div>
    ${panel("已归档的核验","",archiveTable(records.items))}
  </div>`;
  $$("[data-inspection]").forEach(button=>button.onclick=guarded(async ()=>{
    store.kind = button.dataset.inspection;
    store.live = null;
    store.archive = null;
    await ctx.render();
  }));
  bindRequest(ctx,current,root);
  bindResult(ctx,current,root,live);
  bindArchive(ctx,current,root);
}

function requestForm(kind, ctx, pairs) {
  const v = store.values[kind];
  const p = ctx.active.project;
  let markup = "";
  let empty = false;
  const input = (key,label,opts={})=>field(key,label,v[key],opts);
  const minCount = ["form","repeat"].includes(kind) ? 3 : 2;
  const range = input("lower","起始高度 / mm")+input("upper","结束高度 / mm")+input("count","抽样截面数",{min:minCount});
  if (kind === "round") {
    markup = input("z","高度 / mm")+input("datum_x","指定圆心 X / mm")+input("datum_y","指定圆心 Y / mm");
  } else if (kind === "form") {
    const datum = select("datum","基准轴",{spindle:"转台轴",selected:"另选基准段"},v.datum);
    markup = range+datum+`<div id="audit-datum-range" class="audit-subform span-all" ${v.datum==="selected"?"":"hidden"}>
      ${input("datum_lower","基准段起点 / mm")}${input("datum_upper","基准段终点 / mm")}${input("datum_count","基准截面数",{min:3})}
      </div>`;
  } else if (kind === "clearance") {
    markup = range+input("bore","套环内径 / mm",{min:.01})+input("minimum_gap","最小径向余量 / mm",{min:0})+
      input("center_x","套环圆心 X / mm")+input("center_y","套环圆心 Y / mm");
  } else if (kind === "lobes") {
    markup = range+input("order","周向周期数",{min:2,max:12})+input("amplitude_floor","相位所需的最小幅值 / mm",{min:.001});
  } else if (kind === "edges") {
    const options = Object.fromEntries([["","参与重建的角度"],...p.views.filter(view=>view.included).map(view=>[view.id,view.title])]);
    markup = input("z","截面高度 / mm")+input("radius","两侧检查长度 / px",{min:4,max:24})+select("view","采集影像",options,v.view);
  } else if (kind === "repeat") {
    const lookup = Object.entries(pairs).flatMap(([mode,set])=>set.filter(pair=>!pair.same_asset).map(pair=>[
      [mode,pair.first,pair.second].join(","),`${mode==="repeat"?"同向":"相反"} · ${pair.titles.join(" / ")}`,
    ]));
    if (!lookup.length) {
      empty=true;
      markup='<p class="notice span-all">可复核的采集对需要两张独立原图，角度接近相同或相差 180°。</p>';
    } else {
      if (!lookup.some(([id])=>id===v.pair)) v.pair=lookup[0][0];
      markup = select("pair","采集对",Object.fromEntries(lookup),v.pair)+range+
        input("angle_tolerance","角度容差 / °",{min:.005,max:4})+input("alert_mm","端点差阈值 / mm",{min:.001});
    }
  } else if (uploadKinds.has(kind)) {
    const file = store.upload[kind];
    const fixture = kind==="field" ? [["gain","合成整体偏移"],["local","合成局部异常"]] : [["regular","合成规整标板"],["distorted","合成畸变标板"]];
    markup = `<div class="span-all audit-upload">
      ${field("image",kind==="field"?"新拍空场影像":"标板影像","",{type:"file",accept:"image/png,image/jpeg,image/tiff,image/bmp",required:false})}
      <span class="subtext" id="audit-file-name">${esc(file?.name || "请选择影像或合成样例")}</span>
      <div class="row-actions">${fixture.map(([key,label])=>`<button type="button" data-audit-demo="${key}">${label}</button>`).join("")}</div>
    </div>`;
    if (kind==="field") {
      markup += input("drift_limit","允许的相对光差",{min:.005,max:.45})+input("visible_fraction","最小有效画面比例",{min:.05,max:1})+
        input("minimum_region","最小异常区域 / px",{min:4})+`<details class="span-all"><summary>局部检查窗口</summary>
        <div class="audit-subform">${input("x","起点 X / px")}${input("y","起点 Y / px")}${input("width","宽 / px")}${input("height","高 / px")}</div></details>`;
    } else {
      markup += input("pitch_x","横向点距 / mm",{min:.01})+input("pitch_y","纵向点距 / mm",{min:.01})+input("expected","预期 px/mm",{min:.001})+
        input("radius","圆点搜索半径 / px",{min:4,max:45})+input("scale_limit","比例偏差限值",{min:.001})+
        input("residual_limit","定位残差限值 / px",{min:.01})+input("focus_limit","过渡宽度限值 / px",{min:.2})+
        `<details class="span-all"><summary>圆靶估计中心</summary><div class="audit-subform">${Object.entries(cornerNames).map(([key,name])=>
          input(key+"_x",name+" X / px")+input(key+"_y",name+" Y / px")).join("")}</div></details>`;
    }
  }
  return `<form id="audit-request" class="audit-fields" autocomplete="off">${markup}
    <div class="row-actions span-all"><button class="primary" type="submit" ${empty?"disabled":""}>运行核验</button>
    <span id="audit-error" role="alert" class="dialog-error"></span></div></form>`;
}

function toParams(kind, values) {
  const result = Object.fromEntries(Object.entries(values).filter(([key,value])=>key!=="image" && value!==""));
  if (kind==="repeat") {
    const [mode,first,second] = values.pair.split(",");
    Object.assign(result,{mode,first,second});
    delete result.pair;
  } else if (kind==="field") {
    result.window = Object.fromEntries(["x","y","width","height"].map(key=>[key,result[key]]));
    ["x","y","width","height"].forEach(key=>delete result[key]);
  } else if (kind==="grid") {
    Object.keys(cornerNames).forEach(key=>{
      result[key] = {x:result[key+"_x"],y:result[key+"_y"]};
      delete result[key+"_x"];
      delete result[key+"_y"];
    });
  }
  return result;
}

function multipart(file, params) {
  const body = new FormData();
  body.set("image",file);
  body.set("parameters",JSON.stringify(params));
  return body;
}

async function requiredResponse(path, options) {
  const response = await fetch(path,options);
  if (!response.ok) {
    const data = await response.json().catch(()=>null);
    throw new Error(data?.error?.message || `请求失败（${response.status}）`);
  }
  return response;
}

function bindRequest(ctx, current, root) {
  const form = $("#audit-request");
  const kind = store.kind;
  if (!form) return;
  const datumSelector = $('[name="datum"]',form);
  if (datumSelector) datumSelector.onchange = ()=>$("#audit-datum-range").hidden=datumSelector.value!=="selected";
  const uploader = $('[name="image"]',form);
  if (uploader) uploader.onchange = ()=>{
    const file = uploader.files[0];
    if (file) store.upload[kind] = file;
    $("#audit-file-name").textContent = file?.name || store.upload[kind]?.name || "请选择影像或合成样例";
  };
  $$("[data-audit-demo]").forEach(button=>button.onclick=guarded(async ()=>{
    const type = button.dataset.auditDemo;
    const demo = root+"/"+kind+"/demo";
    const response = await requiredResponse(demo+".png?"+qs({type}));
    const file = new File([await response.blob()],button.textContent.trim()+".png",{type:"image/png"});
    const guide = kind==="grid" ? await api(demo+"?"+qs({type})) : null;
    if (!current()) return;
    store.upload[kind] = file;
    const values = formObject(form);
    delete values.image;
    Object.assign(store.values[kind],values);
    if (guide) {
      const parameters = {...guide.parameters};
      Object.keys(cornerNames).forEach(key=>{
        parameters[key+"_x"] = f(parameters[key].x,3);
        parameters[key+"_y"] = f(parameters[key].y,3);
        delete parameters[key];
      });
      Object.assign(store.values[kind],parameters);
    }
    await ctx.render();
  }));
  form.onsubmit=async event=>{
    event.preventDefault();
    const errorBox = $("#audit-error");
    const button = event.submitter || $('button[type="submit"]',form);
    errorBox.textContent = "";
    button.disabled = true;
    try {
      const values = formObject(form);
      delete values.image;
      store.values[kind] = values;
      const revision = ctx.active.project.revision;
      const params = {...toParams(kind,values),revision};
      const isUpload = uploadKinds.has(kind);
      const file = isUpload ? store.upload[kind] : null;
      if (isUpload && !file) throw new Error("先选择影像或加载合成样例。");
      const route = isUpload ? root+"/"+kind : root+"/calculate/"+kind;
      const result = isUpload ?
        await api(route,{method:"POST",body:multipart(file,params)}) :
        await api(route+"?"+qs(params));
      const overlay = kind==="field" ? await requiredResponse(route+".png",{method:"POST",body:multipart(file,params)}).then(res=>res.blob()) : null;
      if (!current()) return;
      releaseUrls();
      const presentation = {
        ...result,
        image: file ? localUrl(file) : null,
        overlay: overlay ? localUrl(overlay) : null,
      };
      store.live = {kind,revision,result:presentation,params,file};
      store.archive = null;
      $("#audit-result").innerHTML = outputPanel(kind,presentation,liveActions(root,store.live));
      bindResult(ctx,current,root,store.live);
      $("#audit-result").focus({preventScroll:true});
      $("#audit-result").scrollIntoView({behavior:"smooth",block:"nearest"});
    } catch(error) {
      if (current()) errorBox.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
}

function archiveActions(root, archive) {
  const path = root+"/records/"+archive.summary.id;
  return `<div class="row-actions"><a class="button" href="${path}/data.csv">下载 CSV</a>
    <a class="button" href="${path}/data.json">下载 JSON</a><button type="button" class="danger" id="audit-archive-delete">移除</button></div>`;
}

function liveActions(root, live) {
  if (live.file) {
    return `<div class="row-actions"><button type="button" data-audit-download="csv">下载 CSV</button>
      <button type="button" data-audit-download="json">下载 JSON</button></div>`;
  }
  const path = root+"/calculate/"+live.kind;
  const suffix = qs(live.params);
  return `<div class="row-actions"><a class="button" href="${path}.csv?${suffix}">下载 CSV</a>
    <button type="button" id="audit-record" class="primary">归档结果</button></div>`;
}

function bindResult(ctx,current,root,live) {
  $$("[data-audit-z]").forEach(button=>button.onclick=guarded(()=>ctx.measureAt(button.dataset.auditZ)));
  const record = $("#audit-record");
  if (record && live) record.onclick=()=>dialog("归档核验",field("title","记录名称",procedures[live.kind][0],{type:"text"}),
    async form=>{
      const data = await api(root+"/records",{method:"POST",body:{
        title:form.get("title"),kind:live.kind,revision:live.revision,parameters:live.params,
      }});
      if (!current()) return;
      const archived = await api(root+"/records/"+data.id);
      if (!current()) return;
      store.archive = archived;
      await ctx.render();
      toast("核验结果已归档");
    },"保存记录");
  for (const button of $$("[data-audit-download]")) button.onclick=guarded(async ()=>{
    const format = button.dataset.auditDownload;
    const response = await requiredResponse(`${root}/${live.kind}.${format}`,{method:"POST",body:multipart(live.file,live.params)});
    const downloadUrl = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = `RimBench-${procedures[live.kind][0]}.${format}`;
    anchor.click();
    window.setTimeout(()=>URL.revokeObjectURL(downloadUrl),1000);
  });
  const remove = $("#audit-archive-delete");
  if (remove && store.archive) remove.onclick=()=>{
    const archive = store.archive;
    dialog("移除核验记录",`<p>移除 ${esc(archive.summary.title)}。</p>`,async ()=>{
      await api(root+"/records/"+archive.summary.id,{method:"DELETE"});
      store.archive=null;
      await ctx.render();
    },"移除");
  };
}

function archiveTable(items) {
  return items.length ? rows(["名称","核验项目","判读","版本","时间",""],items.map(item=>[
    esc(item.title),esc(item.kind_label),resultState(item.state),
    `${item.revision} · ${esc(textState(item.revision_status.state))}`,esc(new Date(item.when).toLocaleString("zh-CN")),
    `<button type="button" data-audit-open="${item.id}">查看记录</button>`,
  ])) : '<div class="empty">运行核验后可归档计算结果。</div>';
}

function bindArchive(ctx,current,root) {
  $$("[data-audit-open]").forEach(button=>button.onclick=guarded(async ()=>{
    const archive=await api(root+"/records/"+button.dataset.auditOpen);
    if (!current()) return;
    store.archive=archive;
    $("#audit-result").innerHTML=outputPanel(archive.summary.kind,archive.result,archiveActions(root,archive),archive);
    bindResult(ctx,current,root,null);
    $("#audit-result").focus({preventScroll:true});
    $("#audit-result").scrollIntoView({behavior:"smooth",block:"nearest"});
  }));
}

function outputPanel(kind,data,actions,archive=null) {
  const title=archive ? esc(archive.summary.title) : procedures[kind][0]+"结果";
  const revision=archive ? `<p class="notice">记录版本 ${archive.summary.revision} · ${esc(textState(archive.revision_status.state))}</p>` : "";
  const renderer = {round:roundOutput,form:formOutput,clearance:clearanceOutput,lobes:lobeOutput,
    edges:edgeOutput,repeat:repeatOutput,field:fieldOutput,grid:gridOutput}[kind];
  return panel(title,actions,revision+renderer(data));
}

function curve(rows, lower, upper, value, unit, active=null, interval=()=>null, xName="Z / mm") {
  return `<div class="chart-frame">${axialPlot({
    parameters:{lower,upper},units:unit,
    samples:rows.map(row=>({z:row.z,value:value(row),interval:interval(row)})),
  },active,{pointControls:false,xName,title:unit+"曲线"})}</div>`;
}

function roundOutput(data) {
  const found = data.fitted_circle;
  if (!found) return `<div class="panel-body">${resultState(data.state)} <span>${esc(data.detail)}</span></div>`;
  const relative = data.origin;
  const points = data.polar.map(row=>({...row,z:row.angle}));
  const outline = sectionPlot({polygons:{nominal:found.polygon,outer:[],inner:[]},centroid:found.center});
  return `${cards([["圆心 X, Y / mm",point(found.center)],["拟合直径",mm(found.diameter)],["拟合 RMS",mm(found.rms)],
    ["拟合圆心径向极差",mm(found.datum.spread)],["指定圆心偏距",mm(relative.distance)],
    ["指定圆心径向极差",mm(relative.radii?.spread)]])}
    <div class="audit-visuals"><div class="chart-frame">${outline}</div>
    ${curve(points,0,360,row=>row.circle_residual,"残差 / mm",null,()=>null,"方位角 / °")}</div>
    <div class="panel-body status-line">${resultState(relative.state)} <span>${esc(data.detail)} 等角残差图横轴为方位角（°）。</span></div>`;
}

function formOutput(data) {
  const s = data.summary;
  const parameters = data.parameters;
  const datum = data.datum;
  return `${cards([["有效抽样",`${s.measured} / ${s.total}`],["最大圆心偏距",mm(s.maximum_axis_offset?.value)],
    ["径向扫掠",mm(s.radial_sweep?.value)],["中心线偏离直径",mm(s.center_line_diameter)],["轴线夹角",angle(s.axis_angle)],
    ["拟合直径变化",mm(s.diameter_range)]])}
    <div class="panel-body status-line">${resultState(s.state)} ${resultState(datum.state)}
    <span>${esc(datum.detail || (s.reference_extensions?`${s.reference_extensions} 个截面使用基准线外推。`:"横向偏差基于重建凸截面的拟合圆心。"))}</span></div>
    ${curve(data.rows,parameters.lower,parameters.upper,row=>row.offset,"偏距 / mm")}
    ${rows(["高度","状态","拟合圆心 X, Y","拟合直径","基准偏距","基准径向极差","直线残差"],data.rows.map(row=>[
      pos(row.z),resultState(row.state),point(row.center),mm(row.diameter),mm(row.offset),mm(row.radial_spread),mm(row.center_residual),
    ]))}
    ${datum.rows.length?`<details class="panel-body"><summary>基准段有效性</summary>${rows(["高度","状态","圆心 X, Y","拟合 RMS"],
       datum.rows.map(row=>[pos(row.z),resultState(row.state),point(row.center),mm(row.fit_rms)]))}</details>`:""}`;
}

function clearanceOutput(data) {
  const s = data.summary;
  const p = data.parameters;
  const critical = s.worst_outer;
  const pointData = critical?.point;
  const values = [
    ["抽样判读",textState(s.state)],
    ["套环内径",mm(p.bore)],
    ["要求余量",mm(p.minimum_gap)],
    ["最小名义余量",mm(s.worst_nominal?.gap)],
    ["最小余量下界",mm(critical?.gap)],
    ["下界限制位置",critical ? `${mm(critical.z)} · ${angle(pointData?.direction)}` : "—"],
  ];
  const plot = critical ? sleevePlot(critical,p) : "";
  return `${cards(values)}<div class="audit-visuals ${critical?"":"single"}">${curve(data.rows,p.lower,p.upper,row=>row.nominal_gap,"余量 / mm",critical?.z,
    row=>row.gap_lower!=null && row.gap_upper!=null ? [row.gap_lower,row.gap_upper] : null)}${plot}</div>
    ${rows(["高度","判读","名义余量","下界","上界","截面"],data.rows.map(row=>[
      pos(row.z),resultState(row.state),mm(row.nominal_gap),mm(row.gap_lower),mm(row.gap_upper),resultState(row.measurement),
    ]))}`;
}

function sleevePlot(row,p) {
  const ring = Array.from({length:72},(_,i)=>[
    p.center_x+p.bore/2*Math.cos(i*Math.PI/36),
    p.center_y+p.bore/2*Math.sin(i*Math.PI/36),
  ]);
  const section = {polygons:{nominal:row.outline,outer:row.outer_outline,inner:ring},centroid:row.point?.point};
  return `<div class="chart-frame">${sectionPlot(section)}<span class="subtext audit-chart-label">虚线圆为套环内径，标记为最小余量见证点</span></div>`;
}

function lobeOutput(data) {
  const s = data.summary;
  const p = data.parameters;
  return `${cards([["记录周期",String(p.order)],["有效截面",`${s.measured} / ${s.total}`],
    ["可跟踪相位",String(s.with_phase)],["相位周期",angle(s.phase_period)],["最大起伏幅值",mm(s.max_amplitude?.value)]])}
    <div class="audit-visuals">${curve(data.rows,p.lower,p.upper,row=>row.amplitude,"幅值 / mm")}
    ${curve(data.rows,p.lower,p.upper,row=>row.phase_continued,"相位 / °")}</div>
    ${rows(["高度","状态","幅值","相位","连续相位","主要周期","功率占比"],data.rows.map(row=>[
      pos(row.z),resultState(row.state),mm(row.amplitude),angle(row.phase),angle(row.phase_continued),row.dominant_order ?? "—",percent(row.mode_power_fraction),
    ]))}`;
}

function edgeOutput(data) {
  const active = data.rows.flatMap(row=>row.edges.map(edge=>({row,edge})));
  const labels=data.rows.map(row=>{
    if (!row.edges.length) return [[esc(row.title),"—",resultState(row.state),"—","—","—",esc(row.reason)]];
    return row.edges.map(edge=>[
      esc(row.title),edge.side==="left"?"左":"右",resultState(edge.state),
      edge.tone?.contrast==null?"—":f(edge.tone.contrast,4),px(edge.transition?.width_px),
      px(edge.transition?.midpoint_px),esc(edge.flags.map(textState).join(" · ") || "—"),
    ]);
  }).flat();
  const analyses = active.filter(item=>item.edge.trace).map(({edge})=>{
    const trace=edge.trace.map(sample=>({z:sample.distance_px,value:sample.raw}));
    const label=edge.side==="left"?"左侧原图亮度":"右侧原图亮度";
    return `<div><p class="audit-chart-label subtext">${label} · 横轴为像素位置</p>
      ${curve(trace,-data.parameters.radius,data.parameters.radius,row=>row.value,"亮度",null,()=>null,"距端点 / px")}</div>`;
  });
  return `${cards([["高度",mm(data.parameters.z)],["影像数量",String(data.rows.length)],
    ["未纳入本报告",String(data.omitted_views)],["最宽过渡带",px(data.max_measured_width?.width_px)]])}
    ${analyses.length?`<div class="audit-visuals">${analyses.join("")}</div>`:""}
    ${rows(["采集影像","侧别","状态","反差","过渡宽度","过渡中点偏移","质检标记"],labels)}`;
}

function repeatOutput(data) {
  const s = data.summary;
  const p = data.parameters;
  return `${cards([["有效复核",`${s.compared} / ${s.total}`],["角度偏差",angle(data.angle_delta_deg)],
    ["最大端点差",mm(s.maximum_edge_delta?.value)],["平均宽度差",mm(s.mean_width_delta)],["中心差 RMS",mm(s.rms_center_delta)]])}
    <div class="status-line panel-body"><span>${esc(data.first.title)} 对 ${esc(data.second.title)} · ${p.mode==="reverse"?"已换算到第一张角度":"同向复核"}</span></div>
    ${curve(data.rows,p.lower,p.upper,row=>row.changes?.maximum,"端点差 / mm")}
    ${rows(["高度","复核判读","左端差","右端差","宽度差","圆心差","合并端点误差"],data.rows.map(row=>[
      pos(row.z),resultState(row.state),mm(row.changes?.left),mm(row.changes?.right),mm(row.changes?.width),mm(row.changes?.center),mm(row.changes?.edge_error),
    ]))}`;
}

function fieldOutput(data) {
  const p = data.parameters.window;
  return `${cards([["判读",textState(data.state)],["有效空场",percent(data.valid_fraction)],["亮度增益",f(data.gain,4)],
    ["局部异常",String(data.region_count)],["偏暗比例",percent(data.residual?.low_fraction)],
    ["偏亮比例",percent(data.residual?.high_fraction)]])}
    ${data.image?`<div class="audit-visuals audit-images">
      <figure><img src="${data.image}" alt="复检空场原图"><figcaption>采集空场</figcaption></figure>
      <figure><img src="${data.overlay}" alt="所选窗口的相对亮度异常图"><figcaption>窗口 ${p.x}, ${p.y} · ${p.width} × ${p.height} px · 蓝暗，橙亮，灰不可测</figcaption></figure>
      </div>`:""}
    ${rows(["分区","有效比例","新图亮度","原参考亮度","相对光差","异常占比"],data.quadrants.map(row=>[
      quadrantNames[row.quadrant],percent(row.valid_fraction),f(row.captured_median,4),f(row.reference_median,4),percent(row.median_deviation),percent(row.local_drift),
    ]))}
    ${data.regions.length?rows(["异常","窗口 X, Y / px","尺寸 / px","偏差均值","最大偏差"],data.regions.map(row=>[
      resultState(row.type),row.bounds.slice(0,2).join(", "),row.bounds.slice(2).join(" × "),percent(row.mean_deviation),percent(row.max_absolute_deviation),
    ])):""}`;
}

function gridOutput(data) {
  const fit=data.fit;
  const s=data.summary;
  const focus=data.focus;
  const grid = data.image ? `<figure class="audit-grid-image"><img src="${data.image}" alt="九点标板原图"><svg viewBox="0 0 ${data.frame.join(" ")}" class="image-overlay" aria-hidden="true">
    ${data.points.map(row=>{
      const pt=row.observed_px || row.predicted_location;
      const [x,y]=pt;
      const stroke = row.observed_px ? "#65e0b1" : "#f9a680";
      return `<circle cx="${x}" cy="${y}" r="4" stroke="${stroke}" fill="none" stroke-width="1.6"/>
        <path d="M${x-7} ${y}h14M${x} ${y-7}v14" stroke="${stroke}" stroke-width="1.3"/>`;
    }).join("")}</svg></figure>`:"";
  return `${cards([["判读",textState(data.state)],["圆点",`${data.detected} / 9`],["横向 px/mm",f(fit?.x_scale,4)],
    ["纵向 px/mm",f(fit?.y_scale,4)],["最大定位残差",px(s?.maximum_residual)],
    ["最宽过渡 P90",px(focus.max_target_p90)],["横纵夹角",angle(fit?.angle_between_axes)]])}
    ${s?.warnings.length?`<div class="notice warning">${esc(s.warnings.join(" "))}</div>`:""}
    <div class="audit-visuals ${grid?"":"single"}">${grid}
    ${rows(["标板位置","实测中心","残差","清晰度","过渡宽度 P90"],data.points.map(row=>[
      `${row.row+1} / ${row.column+1}`,point(row.observed_px),px(row.residual_px),
      resultState(row.focus.state),px(row.focus.widths?.p90),
    ]))}</div>`;
}

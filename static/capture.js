import {$, $$, api, dialog, esc, f, field, formObject, guarded, panel, pill, qs, select, table, toast} from "./ui.js";

const imageTypes = ".png,.jpg,.jpeg,.tif,.tiff,.bmp";
const labelMap = {
  top_x:"轴上点 X / px", top_y:"轴上点 Y / px", bottom_x:"轴下点 X / px", bottom_y:"轴下点 Y / px",
  scale_px:"标尺像素 / px", scale_mm:"实际长度 / mm", ruler_uncertainty:"标尺误差 / mm",
  cutoff:"暗部阈值", min_area:"最小主体 / px²", close_radius:"闭合半径 / px",
  ambiguity_px:"端点误差 / px", min_span:"最短截线 / px", inset:"画面边距 / px",
};
const checkLabels = {compensate_drift:"背光漂移补偿（可关闭）"};

export async function captureTab(ctx, current) {
  const {active, work, base} = ctx;
  const p = active.project;
  const views = p.views;
  const view = views.find(v=>v.id===active.view);
  const reference = p.reference ? await api(base()+"/reference/lighting") : null;
  if (!current()) return;
  work.innerHTML = `<div class="capture-grid">
    <div class="stack">${panel("转台角度影像",`<div class="row-actions"><button id="upload-view" type="button" class="primary">添加角度影像</button><button id="upload-batch" type="button">批量添加</button></div>`,
      views.length?table(["转台角","影像","参与","位移 / px","操作"],views.map(v=>({
        active:v.id===active.view,
        items:[f(v.angle,1)+"°",esc(v.title),pill(v.included?"good":"excluded"),f(v.shift_x,1)+" / "+f(v.shift_y,1),
          `<button data-view-edit="${v.id}" type="button">编辑</button> <button data-view-measure="${v.id}" type="button">查看</button>`],
      }))):'<div class="empty">每个角度上传一张固定背光影像。样件中所有图像需共享画幅。</div>')}
      ${p.calibration?panel("轴标定与像素比例",'<button type="button" id="target">两点标靶</button>',`
        <form id="calibration" class="panel-body form-grid">${Object.entries(p.calibration).map(([key,value])=>field(key,labelMap[key],value)).join("")}
        <div class="span-all row-actions"><button type="submit">应用轴标定</button><span class="subtext">${p.width} × ${p.height} px · ${f(p.calibration.scale_px/p.calibration.scale_mm,4)} px/mm</span></div></form>`):""}
      ${view?panel("细节取景",`<span class="subtext">${esc(view.title)}</span>`,cropMarkup(p)):""}</div>
    <div class="stack">
      ${panel("轮廓配方","",`<form class="panel-body form-grid" id="recipe">${Object.entries(p.segmentation).map(([key,value])=>key in checkLabels?checkBox(key,value):field(key,labelMap[key],value)).join("")}
        <div class="span-all subtext">开启后只用当前角度图里能确认为背景的区域，对照原空场估计缓慢亮度变化；物体暗面和局部反光不作证据，某一侧缺少可信参照时该张保留原处理。</div>
        <div class="span-all"><button type="submit">应用轮廓配方</button></div></form>`)}
      ${panel("背光空场",`<button type="button" id="reference-upload">${p.reference?"更换参考":"上传参考"}</button>`,referencePanel(ctx,reference))}
      ${panel("角度覆盖","",coverage(p))}
    </div>
  </div>`;
  bind(ctx, view);
}

function checkBox(key, value) {
  return `<label class="check span-all"><input type="checkbox" name="${key}" ${value?"checked":""}>${esc(checkLabels[key])}</label>`;
}

function cropMarkup(p) {
  return `<form id="crop-form" class="inline-form panel-body">${field("x","原图 X",Math.round(p.width*0.34),{min:0})}
    ${field("y","原图 Y",Math.round(p.height*0.34),{min:0})}
    ${field("width","取景宽度",Math.max(2,Math.round(p.width*0.3)),{min:2})}
    ${field("height","取景高度",Math.max(2,Math.round(p.height*0.3)),{min:2})}
    <button type="submit">查看细节</button></form><div id="crop-content"></div>`;
}

function referencePanel(ctx, ref) {
  if (!ref) return '<div class="empty">设置同一相机和曝光的无样件背光图，可校正照明不均。</div>';
  const {base,active} = ctx;
  const p = active.project;
  return `<div class="panel-body">
    <div class="visual-frame" style="--aspect:${p.width}/${p.height}">
      <img class="camera" src="${base()}/reference/image?v=${p.revision}" alt="空场参考原图">
      <img class="image-overlay" src="${base()}/reference/heatmap?ramp=shadow&v=${p.revision}" alt="">
    </div>
    <p class="subtext">均匀度 ${f(ref.summary.uniformity*100,1)}% · 局部阴影 ${ref.summary.dirty_cells} 格 · 过曝 ${ref.summary.saturated_cells} 格</p>
    <div class="row-actions"><button id="reference-compare" type="button">对照新空场</button><button id="reference-delete" type="button" class="danger">移除空场</button></div>
    <div id="reference-result"></div>
  </div>`;
}

function coverage(p) {
  const q = p.coverage;
  const gap = q.angle_coverage;
  return `<div class="panel-body"><p>参与 ${q.included_count} / ${p.views.length} 张 · ${gap.distinct} 个独立方向</p>
    <p class="subtext">最大方向缺口 ${f(gap.gap,1)}°${gap.from==null?"":` · ${f(gap.from,1)}° → ${f(gap.to,1)}°`}</p>
    <p class="subtext">${q.overlap_pairs.length} 对角度在同一投影方向。</p></div>`;
}

async function uploads(ctx, path, data) {
  data.append("revision",ctx.active.project.revision);
  const result = await api(ctx.base()+path,{method:"POST",body:data});
  await ctx.refresh(result.project);
  toast("影像已保存");
}

function bind(ctx, view) {
  const {active, update, base} = ctx;
  $("#upload-view").onclick = ()=>dialog("添加角度影像",`<div class="form-grid">
    ${field("image","采集影像","",{type:"file",accept:imageTypes})}
    ${field("angle","转台角度 / °",0)}${field("title","名称（可选）","",{type:"text",required:false})}</div>`,
  data=>uploads(ctx,"/views",data),"添加");
  $("#upload-batch").onclick = ()=>dialog("批量添加角度影像",`<p class="subtext">按文件选择顺序分配角度，首张使用起始角。</p><div class="form-grid">
    ${field("images","采集影像","",{type:"file",accept:imageTypes,multi:true})}
    ${field("start_angle","起始角 / °",0)}${field("angle_step","角度步进 / °",22.5)}</div>`,
  data=>uploads(ctx,"/views/batch",data),"添加");
  $("#reference-upload").onclick = ()=>dialog("设置背光空场",field("image","无样件的曝光影像","",{type:"file",accept:imageTypes}),
    data=>uploads(ctx,"/reference",data),"上传");
  const applyForm = (selector,path) => {
    const form = $(selector);
    if (form) form.onsubmit = guarded(async event=>{
      const values = formObject(event.currentTarget);
      Object.keys(checkLabels).forEach(key=>{values[key]=event.currentTarget.elements[key]?.checked??false;});
      await update(path,values);
      toast("设置已应用");
    });
  };
  applyForm("#calibration","/calibration");
  applyForm("#recipe","/recipe");
  $$("[data-view-edit]").forEach(button=>button.onclick=()=>viewEditor(ctx, active.project.views.find(row=>row.id===button.dataset.viewEdit)));
  $$("[data-view-measure]").forEach(button=>button.onclick=guarded(()=>{
    active.view=button.dataset.viewMeasure;
    return ctx.navigate("measure");
  }));
  if ($("#target")) $("#target").onclick=()=>targets(ctx);
  if ($("#reference-delete")) $("#reference-delete").onclick=()=>dialog("移除空场参考","<p>现有角度影像将按各自亮度重新提取轮廓。</p>",
    ()=>update("/reference",{},"DELETE"),"移除");
  if ($("#reference-compare")) $("#reference-compare").onclick = ()=>dialog("对照新空场",field("image","待比较的空场图像","",{type:"file",accept:imageTypes}),async data=>{
    const result = await api(base()+"/reference/compare",{method:"POST",body:data});
    const r = result.summary.reference_drift;
    $("#reference-result").innerHTML = `<p class="notice">亮度中位偏差 ${f(r.median*100)} 个百分点 · ${r.outside_five_percent_cells} 格超过 5 个百分点</p>`;
  },"比较");
  if (view) $("#crop-form").onsubmit = guarded(event=>{
    const values = formObject(event.currentTarget);
    $("#crop-content").innerHTML = `<img class="small-image" alt="原图局部" src="${base()}/views/${view.id}/crop?${qs(values)}">`;
  });
}

function viewEditor(ctx, item) {
  const body = `<div class="form-grid">${field("title","名称",item.title,{type:"text"})}
    ${field("angle","转台角度 / °",item.angle)}
    ${field("shift_x","横向偏移 / px",item.shift_x)}
    ${field("shift_y","纵向偏移 / px",item.shift_y)}
    ${field("exposure","曝光补偿",item.exposure)}
    <label class="check"><input type="checkbox" name="included" ${item.included?"checked":""}>纳入截面</label>
    <div class="span-all"><button type="button" class="danger" id="remove-angle">移除此角度</button></div></div>`;
  dialog("编辑角度影像",body,async data=>{
    await ctx.update("/views/"+item.id,{...Object.fromEntries(data),included:data.has("included")});
    toast("角度影像已更新");
  });
  $("#remove-angle").onclick = ()=>dialog("移除角度影像",`<p>${esc(item.title)} 将从当前样件移除。</p>`,
    ()=>ctx.update("/views/"+item.id,{},"DELETE"),"移除");
}

function targets(ctx) {
  const p = ctx.active.project;
  const x = Math.round(p.width*.5), ya = Math.round(p.height*.26), yb = Math.round(p.height*.74);
  const radius = Math.round(Math.min(75,Math.min(p.width,p.height)*.09));
  const size = 2*radius+1;
  const bounds = (prefix,y) => field(prefix+"_x","起始 X / px",x-radius)+field(prefix+"_y","起始 Y / px",y-radius)
    +field(prefix+"_width","宽度 / px",size)+field(prefix+"_height","高度 / px",size);
  const fields = `<p class="subtext">上传和采集画幅一致的暗圆靶图，并框住两个靶心。</p>
    <a class="button" target="_blank" href="${ctx.base()}/target/demo.png">查看合成标靶</a>
    ${field("image","标靶图像","",{type:"file",accept:imageTypes})}
    <h3>上方标靶</h3><div class="form-grid">${bounds("upper",ya)}</div><h3>下方标靶</h3><div class="form-grid">${bounds("lower",yb)}</div>
    <div class="form-grid spaced">${field("spacing_mm","已知靶心距离 / mm",33.4)}${field("uncertainty_mm","靶心距离误差 / mm",.02)}</div>`;
  dialog("从双圆标靶估算轴和比例",fields,async input=>{
    const data = Object.fromEntries(input);
    const roi = prefix => Object.fromEntries(["x","y","width","height"].map(key=>[key,data[prefix+"_"+key]]));
    input.set("parameters",JSON.stringify({upper:roi("upper"),lower:roi("lower"),spacing_mm:data.spacing_mm,uncertainty_mm:data.uncertainty_mm}));
    const result = await api(ctx.base()+"/target",{method:"POST",body:input});
    const preview = `<p>靶心相距 ${f(result.separation_px,3)} px · 轴倾角 ${f(result.tilt_deg,3)}°</p>`
      + table(["标靶","X / px","Y / px","直径 / px"],result.targets.map((row,i)=>({items:[i+1,f(row.center[0]),f(row.center[1]),f(row.diameter_px)]})));
    dialog("确认标靶标定",preview,()=>ctx.update("/calibration",result.calibration),"应用");
    return false;
  },"检测靶心");
}

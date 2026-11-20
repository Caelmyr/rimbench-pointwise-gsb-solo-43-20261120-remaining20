import {$, $$, api, dialog, esc, f, field, formObject, guarded, metrics, panel, pill, qs, select, span, table, toast} from "./ui.js";
import {axialPlot, cards, sectionPlot} from "./plots.js";

export async function gaugesTab(ctx, current) {
  const {base,work,active} = ctx;
  const measured = await api(base()+"/gauges");
  if (!current()) return;
  const summary = measured.verification;
  work.innerHTML = panel("量规结果",`<div class="row-actions"><a class="button" href="${base()}/gauges.csv">下载 CSV</a><button type="button" id="new-gauge" class="primary">新增量规</button></div>`,
    `<div class="status-line">${pill(summary.state)} <span>${esc(summary.detail)}</span></div>
    ${measured.rows.length ? table(["量规","项目","高度","公差范围","实测","误差区间","判定",""], measured.rows.map(row=>{
      const g = row.gauge;
      return {items:[esc(g.title),esc(row.metric_label),
        `<button type="button" data-gauge-z="${g.z}" data-gauge-direction="${g.direction}">${f(g.z)} mm</button>`,
        `${f(g.low)} – ${f(g.high)} ${esc(row.unit)}`,`${f(row.value)} ${esc(row.unit)}`,span(row.interval),
        `<span title="${esc(row.detail)}">${pill(row.decision)}</span>`,`<button type="button" data-gauge-edit="${g.id}">编辑</button>`]};
    })) : '<div class="empty">先设置量值、高度与公差范围。</div>'}`);
  $("#new-gauge").onclick = ()=>gaugeEditor(ctx);
  $$("[data-gauge-edit]").forEach(item=>item.onclick=()=>gaugeEditor(ctx,active.project.gauges.find(g=>g.id===item.dataset.gaugeEdit)));
  $$("[data-gauge-z]").forEach(item=>item.onclick=guarded(()=>ctx.measureAt(item.dataset.gaugeZ,item.dataset.gaugeDirection)));
}

function gaugeEditor(ctx, editing) {
  const values = editing || {title:"",metric:"width",z:ctx.active.z,direction:ctx.active.direction,nominal:"",low:"",high:""};
  const form = `<div class="form-grid">${field("title","量规名称",values.title,{type:"text"})}
    ${select("metric","测量项目",metrics,values.metric)}${field("z","物理高度 / mm",values.z)}
    ${field("direction","方向 / °",values.direction)}${field("nominal","标称",values.nominal,{min:0})}
    ${field("low","下限",values.low,{min:0})}${field("high","上限",values.high,{min:0})}
    ${editing?'<div class="span-all"><button type="button" class="danger" id="delete-gauge">移除量规</button></div>':""}</div>`;
  dialog(editing?"编辑量规":"新增公差量规",form,async formData=>{
    const all = ctx.active.project.gauges;
    const draft = Object.fromEntries(formData);
    const next = editing ? all.map(g=>g.id===editing.id?{...draft,id:g.id}:g) : [...all,draft];
    await ctx.update("/gauges",{gauges:next},"PUT");
    toast("公差量规已保存");
  });
  if (editing) $("#delete-gauge").onclick = ()=>dialog("移除量规",`<p>${esc(editing.title)} 将从当前配方移除。</p>`,
    ()=>ctx.update("/gauges",{gauges:ctx.active.project.gauges.filter(g=>g.id!==editing.id)},"PUT"),"移除");
}

export async function recordsTab(ctx, current) {
  const {active,work,base} = ctx;
  const list = await api(base()+"/snapshots");
  if (!current()) return;
  const saved = active.snapshot ? list.items.find(item=>item.id===active.snapshot.summary.id) : null;
  if (!saved) active.snapshot = null;
  work.innerHTML = `<div class="stack">
    ${panel("测量快照",`<button type="button" class="primary" id="snapshot-save" ${active.project.views.length?"":"disabled"}>保存当前测量</button>`, snapshotRows(ctx,list.items))}
    <div id="snapshot-content">${saved?selectedSnapshot(ctx):""}</div>
    ${panel("检验记录",'<button type="button" id="note-new">添加记录</button>',notes(ctx))}
  </div>`;
  $("#snapshot-save").onclick = ()=>saveSnapshot(ctx);
  $("#note-new").onclick = ()=>writeNote(ctx);
  $$("[data-note-delete]").forEach(b=>b.onclick=()=>dialog("移除检验记录","<p>从此样件移除该条记录。</p>",
    ()=>ctx.update("/notes/"+b.dataset.noteDelete,{},"DELETE"),"移除"));
  $$("[data-open-snapshot]").forEach(b=>b.onclick=guarded(async ()=>{
    const identity = active.project.id;
    const result = await api(base()+"/snapshots/"+b.dataset.openSnapshot);
    if (active.tab !== "records" || active.project.id !== identity) return;
    active.snapshot = result;
    await ctx.render();
  }));
  if (saved) bindSnapshot(ctx);
}

function snapshotRows(ctx, items) {
  if (!items.length) return '<div class="empty">保存已测量的截面与可选轴向曲线，供下次检查时比较。</div>';
  return table(["快照","时间","版本","截面","状态",""],items.map(row=>({items:[
    esc(row.title),esc(new Date(row.captured_at).toLocaleString("zh-CN")),row.revision,`${f(row.z)} mm`,pill(row.review.state),
    `<button type="button" data-open-snapshot="${row.id}">查看快照</button>`,
  ]})));
}

function saveSnapshot(ctx) {
  const {active,base} = ctx;
  const markup = `<div class="form-grid">${field("title","快照名称","",{type:"text"})}
    ${field("z","截面高度 / mm",active.z)}${field("direction","方向 / °",active.direction)}
    ${active.curve?'<label class="check span-all"><input name="curve" type="checkbox" checked>包含当前轴向曲线</label>':""}</div>`;
  dialog("归档当前测量",markup,async values=>{
    const body = Object.fromEntries(values);
    body.revision = active.project.revision;
    if (values.has("curve") && active.curve) body.profile = active.curve.parameters;
    const saved = await api(base()+"/snapshots",{method:"POST",body});
    active.snapshot = await api(base()+"/snapshots/"+saved.id);
    await ctx.render();
    toast("测量快照已归档");
  },"保存快照");
}

function selectedSnapshot(ctx) {
  const {active,base} = ctx;
  const saved = active.snapshot;
  const path = base()+"/snapshots/"+saved.summary.id;
  const outline = panel(`固定截面 · ${f(saved.section.z)} mm`,pill(saved.section.state),
    `<div class="chart-frame">${sectionPlot(saved.section,null)}</div>${cards(saved.section)}`);
  const compare = panel("复测", "",`<div class="panel-body"><div class="row-actions">
    <button id="compare-section" type="button">比较同高截面</button>
    <a class="button" href="${path}/data.json">JSON</a>
    <a class="button" target="_blank" rel="noopener" href="${path}/report">快照报告 ↗</a></div>
    ${saved.profile?`<form id="compare-curve" class="inline-form spaced">${field("z_shift","参考高度补偿 / mm",0)}
      <button type="submit">比较轴向曲线</button></form>`:""}
    <div class="spaced" id="comparison-result"></div>
    <button id="delete-snapshot" class="danger" type="button">删除此快照</button></div>`);
  return `<div class="section-grid">${outline}${compare}</div>`;
}

function bindSnapshot(ctx) {
  const saved = ctx.active.snapshot;
  const path = ctx.base()+"/snapshots/"+saved.summary.id;
  $("#compare-section").onclick = guarded(async ()=>{
    const result = await api(path+"/compare?mode=section");
    const box = $("#comparison-result");
    if (!box) return;
    box.innerHTML = result.state !== "comparable" ? '<p class="notice warning">当前没有可比较的截面。</p>' :
      table(["量值","原值","现值","差值","区间"],Object.entries(result.metrics).map(([key,row])=>({items:[
        metrics[key][0],f(row.before),f(row.after),`${f(row.delta)} ${esc(row.unit)}`,span(row.delta_interval),
      ]})));
  });
  if ($("#compare-curve")) $("#compare-curve").onsubmit = guarded(async event=>{
    const parameters = formObject(event.currentTarget);
    const query = qs(parameters);
    const result = await api(path+"/compare?"+query);
    const box = $("#comparison-result");
    if (!box) return;
    const compare = result.summary;
    box.innerHTML = `<p class="notice">${compare.compared}/${compare.total} 个高度可比 · ${compare.confirmed_changes} 个差值区间离开零</p>
      ${table(["现高度","参考高度","差值","区间","状态"],result.rows.map(row=>({items:[
        f(row.z),f(row.reference_z),f(row.delta),span(row.delta_interval),pill(row.state),
      ]})))}
      <a class="button spaced" href="${path}/compare.csv?${query}">下载差值 CSV</a>`;
  });
  $("#delete-snapshot").onclick = ()=>dialog("删除测量快照",`<p>删除 ${esc(saved.summary.title)}。</p>`, async ()=>{
    await api(path,{method:"DELETE"});
    ctx.active.snapshot = null;
    await ctx.render();
  },"删除");
}

function notes(ctx) {
  const items = ctx.active.project.notes;
  if (!items.length) return '<div class="empty">可记录观察、复测计划或挂起原因。</div>';
  const kinds = {observation:"观察",review:"复测",hold:"挂起"};
  return `<div class="panel-body"><ul class="note-list">${items.slice().reverse().map(note=>`<li>
    <div class="row-actions"><span><span class="pill ${note.kind==="hold"?"warn":""}">${esc(kinds[note.kind])}</span>
    ${esc(note.author)} · ${esc(new Date(note.created_at).toLocaleDateString("zh-CN"))}</span>
    <button type="button" data-note-delete="${note.id}">移除</button></div><p>${esc(note.text)}</p>
    <span class="subtext">${note.z!=null?`高度 ${f(note.z)} mm`:""} ${note.view?esc(ctx.active.project.views.find(v=>v.id===note.view)?.title):""}</span>
    </li>`).join("")}</ul></div>`;
}

function writeNote(ctx) {
  const p = ctx.active.project;
  const body = `<div class="form-grid">${field("author","记录者",p.operator||"检验员",{type:"text"})}
    ${select("kind","类型",{observation:"观察",review:"复测",hold:"挂起"},"observation")}
    ${field("z","关联高度 / mm（可选）",ctx.active.z,{required:false})}
    ${select("view","关联影像",{ "":"不指定",...Object.fromEntries(p.views.map(row=>[row.id,row.title]))},"")}
    <label class="field span-all">记录内容<textarea name="text" required maxlength="1000"></textarea></label></div>`;
  dialog("添加检验记录",body,data=>{
    const note = Object.fromEntries(data);
    note.z = note.z || null;
    note.view = note.view || null;
    return ctx.update("/notes",note,"POST");
  });
}

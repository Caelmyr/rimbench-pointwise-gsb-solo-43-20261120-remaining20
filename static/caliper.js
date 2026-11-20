import {api, dialog, esc, f, field, pill, select, span, table, toast} from "./ui.js";

const stateLabels = {
  "caliper-match": "宽度相符",
  "caliper-mismatch": "宽度不符",
  "caliper-gap": "落入缺测区",
  "caliper-outside": "超出测量区间",
};
const statePillClass = {
  "caliper-match": "good",
  "caliper-mismatch": "bad",
  "caliper-gap": "warn",
  "caliper-outside": "warn",
};
const decisionLabels = {
  determined: "可以定零位",
  ambiguous: "还不能定零位",
  "no-fit": "没有可行补偿",
  insufficient: "可比读数不足",
};
const decisionPillClass = {
  determined: "good",
  ambiguous: "warn",
  "no-fit": "bad",
  insufficient: "warn",
};

export function directionOptions(active) {
  const directions = new Set([active.direction, 0]);
  active.project.views.forEach(view => directions.add(Number(view.angle)));
  if (active.profile?.direction != null) directions.add(Number(active.profile.direction));
  return Object.fromEntries([...directions].sort((a, b) => a-b).map(value => [value, `${f(value, 1)}°`]));
}

export function caliperDialog(ctx) {
  const {active} = ctx;
  const values = active.caliper || {shift_limit: 5};
  const markup = `<div class="form-grid">
    ${field("caliper", "高度、宽度 CSV（毫米）", null, {type: "file", accept: ".csv,text/csv", required: true})}
    ${select("direction", "测量方向", directionOptions(active), active.direction)}
    ${field("shift_limit", "高度补偿允许范围 ± / mm", values.shift_limit ?? 5, {min: 0.001})}
    <p class="subtext span-all">只允许整组卡尺读数沿高度平移；不修改宽度，也不沿高度拉伸。落入缺测区或超出转台测量区间的读数不参与判定。</p>
  </div>`;
  dialog("卡尺复检对零", markup, async form => {
    const upload = form.get("caliper");
    if (!upload || !(upload instanceof File) || !upload.size) {
      throw new Error("请先选择卡尺高度、宽度 CSV。");
    }
    const params = {
      direction: form.get("direction"),
      shift_limit: form.get("shift_limit"),
      revision: active.project.revision,
    };
    const body = new FormData();
    body.set("caliper", upload);
    body.set("parameters", JSON.stringify(params));
    const result = await api(ctx.base()+"/caliper", {method: "POST", body});
    active.caliper = result;
    active.caliperFile = upload.name;
    active.caliperBlob = upload;
    await ctx.render();
    toast("卡尺复检对零完成");
  }, "开始对零");
}

function miniPill(state) {
  return `<span class="pill ${esc(statePillClass[state] || "")}">${esc(stateLabels[state] || state)}</span>`;
}

function candidateTable(result) {
  if (!result.candidates.length) return "";
  return table(
    ["可行补偿范围 / mm", "代表补偿 / mm", "可比", "相符", "宽度不符", "缺测区", "超出区间"],
    result.candidates.map(candidate => ({items: [
      `${f(candidate.shift_low, 3)} – ${f(candidate.shift_high, 3)}`,
      f(candidate.shift, 3),
      String(candidate.comparable), String(candidate.matches), String(candidate.mismatches),
      String(candidate.gaps), String(candidate.outside),
    ]})),
  );
}

function detailTable(result) {
  return table(
    ["序号", "卡尺高度 / mm", "卡尺宽度 / mm", "对齐后高度 / mm", "转台宽度 / mm",
     "转台尺寸区间 / mm", "宽度差值 / mm", "转台采样跨度 / mm", "核对", "原因"],
    result.rows.map(row => ({items: [
      String(row.index), f(row.caliper_z, 3), f(row.caliper_width, 3), f(row.aligned_z, 3),
      f(row.reference_value, 3), span(row.reference_interval), f(row.delta_width, 3),
      row.reference_span ? `${f(row.reference_span[0], 2)} – ${f(row.reference_span[1], 2)}` : "—",
      miniPill(row.state), esc(row.reason),
    ]})),
  );
}

export function caliperPanel(ctx) {
  const {active} = ctx;
  const result = active.caliper;
  if (!result) return "";
  const stale = result.revision !== active.project.revision;
  const summary = result.decision === "determined"
    ? `补偿 ${f(result.candidates[0].shift_low, 3)} – ${f(result.candidates[0].shift_high, 3)} mm（代表值 ${f(result.representative_shift, 3)} mm）`
    : result.decision === "ambiguous"
      ? `${result.candidates.length} 段可行补偿并存`
      : `最充分对齐下 ${result.n_max}/${result.total_readings} 个读数可比`;
  return `<section class="panel spaced" id="caliper-panel">
    <header class="panel-head"><h2>卡尺复检对零</h2>
      <span class="row-actions"><button type="button" id="caliper-redo">重新导入 CSV</button>
      <button type="button" class="quiet" id="caliper-download">核对 CSV</button></span></header>
    <div class="status-line">
      <span class="pill ${esc(decisionPillClass[result.decision] || "")}">${esc(decisionLabels[result.decision] || result.decision)}</span>
      <span>${esc(summary)} · 方向 ${f(result.direction, 1)}° · 允许 ±${f(result.shift_limit, 2)} mm · ${esc(active.caliperFile || "")}</span>
    </div>
    <div class="panel-body">
      ${stale ? '<p class="notice warning">样件已在对零之后更新，结果基于旧版本，请重新对零。</p>' : ""}
      <p class="subtext">${esc(result.detail)}</p>
      ${candidateTable(result)}
      <h3 class="subtext" style="margin:14px 0 6px">逐读数核对（采用代表补偿 ${f(result.representative_shift, 3)} mm）</h3>
      ${detailTable(result)}
    </div>
  </section>`;
}

function caliperFormData(ctx) {
  const {active} = ctx;
  const result = active.caliper;
  if (!active.caliperBlob) throw new Error("原始 CSV 已不在页面中，请重新导入。");
  const body = new FormData();
  body.set("caliper", active.caliperBlob, active.caliperFile || "caliper.csv");
  body.set("parameters", JSON.stringify({
    direction: result.direction, shift_limit: result.shift_limit, revision: result.revision,
  }));
  return body;
}

export function bindCaliper(ctx) {
  const redo = $("#caliper-redo");
  if (redo) redo.onclick = () => caliperDialog(ctx);
  const download = $("#caliper-download");
  if (download) download.onclick = async () => {
    const response = await fetch(ctx.base()+"/caliper.csv", {method: "POST", body: caliperFormData(ctx)});
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.error?.message || `请求失败（${response.status}）`);
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "RimBench-卡尺复检对零.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  };
}

export const $ = (q, root = document) => root.querySelector(q);
export const $$ = (q, root = document) => Array.from(root.querySelectorAll(q));
export const esc = (s = "") => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
export const qs = (o) => new URLSearchParams(o).toString();
export const f = (n, d = 2) => n == null || !Number.isFinite(Number(n)) ? "—" : Number(n).toFixed(d);
export const span = (p) => p ? f(p[0]) + " – " + f(p[1]) : "—";
export const stateLabels = {
  measured:"已测量", unbounded:"支撑未闭合", unseen:"没有有效角度", conflict:"角度冲突",
  borderline:"边界不稳", usable:"有效", excluded:"已排除", clipped:"主体触边", outside:"截线超出画面",
  empty:"该层无主体", "too-narrow":"轮廓过窄", disconnected:"轮廓分段", "image-rejected":"影像不可用",
  pass:"合格", fail:"超差", review:"待复测", unavailable:"暂无结果",
  "on-hold":"已挂起", unset:"未设置量规", ready:"可放行", "out-of-spec":"超出公差",
  comparable:"可比较", "current-gap":"本次缺测", "reference-gap":"参考缺测", "outside-reference":"超出参考",
};
export const metrics = {
  width:["方向宽度","mm"], max_feret:["最大卡尺径","mm"], min_feret:["最小卡尺径","mm"],
  equivalent_diameter:["等面积直径","mm"], area:["截面积","mm²"], roundness:["圆度",""],
};
export const pill = (state) => `<span class="pill ${esc(state)}">${esc(stateLabels[state] || state)}</span>`;
export const field = (key, title, value, opts = {}) =>
  `<label class="field">${esc(title)}<input name="${key}" type="${opts.type ?? "number"}" value="${esc(value)}" ${opts.required === false ? "" : "required"} ${opts.type ? "" : 'step="any"'} ${opts.min != null ? 'min="'+opts.min+'"' : ""} ${opts.max != null ? 'max="'+opts.max+'"' : ""} ${opts.accept ? 'accept="'+opts.accept+'"' : ""} ${opts.multi ? "multiple" : ""}></label>`;
export const options = (dict, selected) => Object.entries(dict).map(([key,value]) =>
  `<option value="${esc(key)}" ${selected === key ? "selected" : ""}>${esc(Array.isArray(value) ? value[0] : value)}</option>`).join("");
export const select = (key, title, values, selected) => `<label class="field">${esc(title)}<select name="${key}">${options(values, selected)}</select></label>`;
export const table = (headings, rows) => `<div class="data-wrap"><table><thead><tr>${headings.map(x=>`<th scope="col">${esc(x)}</th>`).join("")}</tr></thead>
  <tbody>${rows.map(row=>`<tr ${row.active ? 'class="active"' : ""}>${row.items.map(x=>`<td>${x}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
export const panel = (title, right, content) => `<section class="panel"><header class="panel-head"><h2>${title}</h2>${right}</header>${content}</section>`;
export const formObject = (form) => Object.fromEntries(new FormData(form));

export async function api(path, {method="GET", body, signal}={}) {
  const headers = body && !(body instanceof FormData) ? {"Content-Type":"application/json"} : {};
  const payload = body && !(body instanceof FormData) ? JSON.stringify(body) : body;
  const response = await fetch(path, {method, headers, body:payload, signal});
  if (response.status === 204) return null;
  const type = response.headers.get("Content-Type") || "";
  const value = type.includes("json") ? await response.json() : null;
  if (!response.ok) {
    const detail = value?.error;
    let message = detail?.message || `请求失败（${response.status}）`;
    if (detail?.details?.checks) message += " " + detail.details.checks.join(" ");
    throw Object.assign(new Error(message), {code:detail?.code, status:response.status});
  }
  return value;
}

let toastTimer = 0;
export function toast(text, error = false) {
  const box = $("#toast");
  clearTimeout(toastTimer);
  box.hidden = false;
  box.textContent = text;
  box.classList.toggle("error", error);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3800);
}

export function dialog(title, fields, action, submit = "保存") {
  const modal = $("#modal");
  const form = $("#modal-form");
  const alert = $("#modal-error");
  const button = $("#modal-submit");
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = fields;
  button.textContent = submit;
  button.disabled = false;
  alert.textContent = "";
  form.onsubmit = async (event) => {
    event.preventDefault();
    alert.textContent = "";
    button.disabled = true;
    try {
      if (await action(new FormData(form), form) !== false) modal.close();
    } catch (error) {
      alert.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  if (!modal.open) modal.showModal();
  return modal;
}
for (const selector of ["#modal-close", "#modal-cancel"]) {
  $(selector).addEventListener("click", () => $("#modal").close());
}

export function guarded(action, target) {
  return async (event) => {
    if (event) event.preventDefault();
    const button = target || event?.submitter || event?.currentTarget;
    if (button?.tagName === "BUTTON") button.disabled = true;
    try {
      await action(event);
    } catch (error) {
      if (error.name !== "AbortError") toast(error.message, true);
    } finally {
      if (button?.tagName === "BUTTON") button.disabled = false;
    }
  };
}

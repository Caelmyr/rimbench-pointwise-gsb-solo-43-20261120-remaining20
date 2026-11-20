import {esc, f, span, metrics, pill} from "./ui.js";

const L = 49, R = 524, TOP = 24, BOT = 292;

function gridX(values, where, min, max) {
  return values.filter(v => v >= min && v <= max).map(v => {
    const x = where(v);
    return `<line x1="${x}" y1="${TOP}" x2="${x}" y2="${BOT}" stroke="#dbe7df"/><text x="${x}" y="312" text-anchor="middle">${f(v,1)}</text>`;
  }).join("");
}
function gridY(values, where, min, max) {
  return values.filter(v => v >= min && v <= max).map(v => {
    const y = where(v);
    return `<line x1="${L}" y1="${y}" x2="${R}" y2="${y}" stroke="#dbe7df"/><text x="43" y="${y+4}" text-anchor="end">${f(v,1)}</text>`;
  }).join("");
}
function ticks(low, high, pieces = 6) {
  const raw = (high-low)/pieces;
  if (raw <= 0) return [low];
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = ([1,2,5,10].find(v => v * magnitude >= raw) ?? 10)*magnitude;
  const first = Math.ceil(low/step)*step;
  return Array.from({length:Math.min(20, Math.max(0, Math.floor((high-first)/step)+1))}, (_,i)=>first+i*step);
}
function wrap(title, contents, labels = "") {
  return `<svg role="img" aria-label="${esc(title)}" viewBox="0 0 550 335" xmlns="http://www.w3.org/2000/svg">
    <g font-size="10" fill="#46645e" font-family="system-ui,sans-serif">${contents}${labels}</g></svg>`;
}
function polygonPath(points, x, y) {
  return points.map(([a,b]) => `${x(a).toFixed(3)},${y(b).toFixed(3)}`).join(" ");
}

export function sectionPlot(section, selectedView) {
  const polygons = section.polygons;
  const points = [...polygons.outer, ...polygons.inner, ...polygons.nominal];
  if (!points.length) return `<div class="plot-empty">${esc(section.detail)}</div>`;
  const xs = points.map(([x])=>x), ys = points.map(([,y])=>y);
  const midX = (Math.min(...xs)+Math.max(...xs))/2;
  const midY = (Math.min(...ys)+Math.max(...ys))/2;
  const scale = Math.min((R-L-42)/Math.max(1, Math.max(...xs)-Math.min(...xs)),
                         (BOT-TOP-42)/Math.max(1, Math.max(...ys)-Math.min(...ys)));
  const centerX = (L+R)/2, centerY = (TOP+BOT)/2;
  const x = a => centerX+(a-midX)*scale;
  const y = b => centerY-(b-midY)*scale;
  const xl = midX+(L-centerX)/scale, xr = midX+(R-centerX)/scale;
  const yl = midY+(centerY-BOT)/scale, yr = midY+(centerY-TOP)/scale;
  const field = gridX(ticks(xl,xr), x, xl,xr)+gridY(ticks(yl,yr), y, yl,yr);
  const painter = ([key,fill,stroke,dash]) => polygons[key].length ? `<polygon points="${polygonPath(polygons[key],x,y)}"
      fill="${fill}" stroke="${stroke}" stroke-width="${key==="nominal" ? 2.4: 1.3}" stroke-dasharray="${dash}"/>` : "";
  const outlines = [["outer","#deebee","#7199ae","5 4"],["nominal","#b4dfd0","#23816b","none"],["inner","none","#398f78","3 4"]].map(painter).join("");
  const origin = x(0)>=L && x(0)<=R && y(0)>=TOP && y(0)<=BOT ? `<path d="M${x(0)-4},${y(0)}h8m-4,-4v8" stroke="#657c75"/>` : "";
  const mark = section.centroid ? `<circle cx="${x(section.centroid[0])}" cy="${y(section.centroid[1])}" r="3.2" fill="#11765e" stroke="#fff"/>` : "";
  const projections = (section.active_support ?? []).filter(item => item.view_id === selectedView).map(item => {
    const [nx,ny] = item.normal;
    const hits = polygons.nominal.filter(([u,v])=>Math.abs(nx*u+ny*v-item.offset)<0.00002);
    if (hits.length < 2) return "";
    return `<polyline points="${polygonPath(hits,x,y)}" fill="none" stroke="#dd803f" stroke-width="3.2"/>`;
  }).join("");
  return wrap("轮廓截面，横纵单位为毫米", field+outlines+origin+mark+projections,
    `<text x="496" y="326">X / mm</text><text x="15" y="18">Y / mm</text>`);
}

function segments(points, predicate) {
  const parts = [];
  let current = [];
  for (const sample of points) {
    if (predicate(sample)) current.push(sample);
    else if (current.length) { parts.push(current); current = []; }
  }
  if (current.length) parts.push(current);
  return parts;
}

export function axialPlot(curve, activeZ, {xName="Z / mm", pointControls=true, title="轴向测量曲线"}={}) {
  if (!curve) return '<div class="plot-empty">设定轴向范围后生成曲线。</div>';
  const zMin = curve.parameters.lower, zMax = curve.parameters.upper;
  const x = z => L+(z-zMin)/(zMax-zMin)*(R-L);
  const all = curve.samples.flatMap(row => row.interval ?? (row.value == null ? [] : [row.value]));
  if (!all.length) return '<div class="plot-empty">所选范围没有可测量的截线。</div>';
  const steps = curve.steps;
  const shoulderValues = (steps?.shoulders ?? []).flatMap(row => [row.half_level, row.left?.level, row.right?.level]).filter(v=>v!=null);
  const min = Math.min(...all, ...(shoulderValues.length?shoulderValues:all));
  const max = Math.max(...all, ...(shoulderValues.length?shoulderValues:all));
  const pad = Math.max((max-min)*0.09, 0.04);
  const y = value => BOT-(value-min+pad)/(max-min+2*pad)*(BOT-TOP);
  let body = gridX(ticks(zMin,zMax), x,zMin,zMax)+gridY(ticks(min-pad,max+pad),y,min-pad,max+pad);
  for (const run of segments(curve.samples, row => row.interval)) {
    const points = [...run.map(row => [row.z,row.interval[0]]), ...run.slice().reverse().map(row=>[row.z,row.interval[1]])];
    body += `<polygon points="${polygonPath(points,x,y)}" fill="#cde5d9"/>`;
  }
  body += stepLayer(steps, x, y, zMin, zMax);
  for (const run of segments(curve.samples, row => row.value != null)) {
    body += `<polyline points="${polygonPath(run.map(row=>[row.z,row.value]),x,y)}" fill="none" stroke="#237864" stroke-width="2.4"/>`;
  }
  if (activeZ >= zMin && activeZ <= zMax) body += `<line x1="${x(activeZ)}" x2="${x(activeZ)}" y1="${TOP}" y2="${BOT}" stroke="#da8148" stroke-dasharray="4 4"/>`;
  body += curve.samples.map(row => {
    const yy = row.value == null ? BOT-3 : y(row.value);
    const fill = row.value == null ? "#dc985f" : "#26836b";
    const interaction = pointControls ? `data-sample-z="${row.z}" role="button" tabindex="0" aria-label="截面 ${f(row.z)} 毫米" style="cursor:pointer"` : "";
    return `<circle ${interaction} cx="${x(row.z)}" cy="${yy}" r="4" fill="${fill}" stroke="white"><title>${esc(xName)} ${f(row.z)} · ${f(row.value)} ${esc(curve.units)}</title></circle>`;
  }).join("");
  return wrap(title, body, `<text x="522" y="328" text-anchor="end">${esc(xName)}</text><text x="17" y="17">${esc(curve.units)}${steps?.shoulders?.length?" · 肩位点":""}</text>`);
}

const STEP_COLORS = {shoulder:"#1d6f94", ambiguous:"#bb6a24", missing:"#8a5c8f"};

function stepLayer(steps, x, y, zMin, zMax) {
  if (!steps || !(steps.segments?.length || steps.shoulders?.length)) return "";
  let marks = "";
  for (const segment of steps.segments ?? []) {
    if (!segment.used || segment.interval == null) continue;
    const left = Math.max(segment.start_z, zMin), right = Math.min(segment.end_z, zMax);
    if (right < left) continue;
    const top = y(segment.interval[1]), bottom = y(segment.interval[0]);
    marks += `<rect x="${x(left)}" y="${Math.min(top,bottom)}" width="${Math.max(1,x(right)-x(left))}"
      height="${Math.max(1,Math.abs(bottom-top))}" fill="#24735f" fill-opacity="0.20" stroke="#24735f" stroke-width="1.1">
      <title>稳定段 ${f(segment.start_z)} – ${f(segment.end_z)} mm · ${f(segment.level)} ${esc(steps.units)}</title></rect>`;
    marks += `<line x1="${x(left)}" x2="${x(right)}" y1="${y(segment.level)}" y2="${y(segment.level)}" stroke="#175a4a" stroke-width="1.4" stroke-dasharray="3 2"/>`;
  }
  for (const spike of steps.spikes ?? []) {
    if (spike.value == null) continue;
    marks += `<circle cx="${x(spike.z)}" cy="${y(spike.value)}" r="5" fill="none" stroke="#b04a3c" stroke-width="1.3">
      <title>尖峰 ${f(spike.z)} mm · 已排除</title></circle>`;
  }
  for (const shoulder of steps.shoulders ?? []) {
    const span = shoulder.spacing_range;
    if (span) {
      marks += `<rect x="${x(span[0])}" y="${TOP}" width="${Math.max(1,x(span[1])-x(span[0]))}" height="${BOT-TOP}"
        fill="${STEP_COLORS[shoulder.state] ?? "#657c75"}" fill-opacity="0.10">
        <title>采样间隔允许的肩位范围 ${f(span[0])} – ${f(span[1])} mm</title></rect>`;
    }
    if (shoulder.interval) {
      marks += `<line x1="${x(shoulder.interval[0])}" x2="${x(shoulder.interval[1])}" y1="${BOT-7}" y2="${BOT-7}"
        stroke="${STEP_COLORS[shoulder.state] ?? "#657c75"}" stroke-width="3"><title>测值区间 ${f(shoulder.interval[0])} – ${f(shoulder.interval[1])} mm</title></line>`;
    }
    if (shoulder.half_level != null) {
      const transition = shoulder.transition;
      marks += `<line x1="${x(transition.start_z)}" x2="${x(transition.end_z)}" y1="${y(shoulder.half_level)}" y2="${y(shoulder.half_level)}"
        stroke="${STEP_COLORS[shoulder.state] ?? "#657c75"}" stroke-width="1.2" stroke-dasharray="2 2">
        <title>半高 ${f(shoulder.half_level)} ${esc(shoulder.units)}</title></line>`;
      marks += `<circle cx="${x(shoulder.left.used_end_z)}" cy="${y(shoulder.left.level)}" r="2.4" fill="#175a4a"/>
        <circle cx="${x(shoulder.right.used_start_z)}" cy="${y(shoulder.right.level)}" r="2.4" fill="#175a4a"/>`;
    }
    if (shoulder.z != null) {
      marks += `<line x1="${x(shoulder.z)}" x2="${x(shoulder.z)}" y1="${y(shoulder.half_level ?? shoulder.left.level)}" y2="${BOT-9}"
        stroke="${STEP_COLORS[shoulder.state] ?? "#657c75"}" stroke-width="1" stroke-dasharray="2 3"/>`;
      marks += `<path d="M${x(shoulder.z)-4.5},${TOP+3}L${x(shoulder.z)+4.5},${TOP+3}L${x(shoulder.z)},${TOP+11}Z"
        fill="${STEP_COLORS[shoulder.state] ?? "#657c75"}">
        <title>肩位 ${f(shoulder.z)} mm（${esc(shoulder.detail)}）</title></path>`;
    } else {
      const gap = shoulder.transition;
      marks += `<rect x="${x(gap.start_z)}" y="${TOP}" width="${Math.max(2,x(gap.end_z)-x(gap.start_z))}" height="${BOT-TOP}"
        fill="${STEP_COLORS[shoulder.state]}" fill-opacity="0.13" stroke="${STEP_COLORS[shoulder.state]}" stroke-dasharray="3 3">
        <title>肩位缺测 ${f(gap.start_z)} – ${f(gap.end_z)} mm</title></rect>`;
    }
    if (shoulder.state === "ambiguous") {
      marks += `<text x="${x(shoulder.z ?? (shoulder.transition.start_z+shoulder.transition.end_z)/2)}" y="${TOP+22}"
        text-anchor="middle" fill="#9a531b" font-size="10">补测</text>`;
    }
  }
  return marks;
}

export function observationOverlay(view, frame, z, width, height) {
  if (!view?.axis?.axis?.length) return "";
  const [a,b] = view.axis.axis;
  const p = view.axis.origin;
  const u = view.axis.up_unit;
  const r = view.axis.across_unit;
  const origin = [p[0]+z*view.axis.px_per_mm*u[0], p[1]+z*view.axis.px_per_mm*u[1]];
  const dist = Math.hypot(width,height);
  const ends = [origin.map((v,i)=>v-r[i]*dist), origin.map((v,i)=>v+r[i]*dist)];
  const vector = `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="#8cf1d3" stroke-width="1.7" stroke-dasharray="6 5"/>
    <line x1="${ends[0][0]}" y1="${ends[0][1]}" x2="${ends[1][0]}" y2="${ends[1][1]}" stroke="#ffd38b" stroke-width="1.55"/>`;
  const dot = (pt, color) => `<circle cx="${pt[0]}" cy="${pt[1]}" r="4.4" fill="${color}" stroke="#f5fefb" stroke-width="1.4"/>`;
  const marks = view.observation.endpoints?.map(pt=>dot(pt, "#cb7736")).join("") ?? "";
  return `<svg class="image-overlay" viewBox="0 0 ${width} ${height}" aria-hidden="true">${vector}${dot(p, "#65b99d")}${marks}</svg>`;
}

export function cards(section) {
  if (!section.metrics) return "";
  return `<div class="metric-strip">${Object.entries(metrics).map(([key,[name,unit]])=>`<div class="metric-card">
      <span class="title">${name}</span><b>${f(section.metrics[key],key==="roundness"?3:2)} <small>${unit}</small></b>
      <span class="range">${span(section.intervals?.[key])}</span></div>`).join("")}</div>`;
}

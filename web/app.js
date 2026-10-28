"use strict";
// NetMon UI. Live data arrives from the local NetMon process over a WebSocket;
// the page only displays it (all measuring happens in Python).

const COLORS = { down: "#0fa0c4", up: "#e36d26", grid: "#1a2230", axis: "#6c788b", bg: "#0a0e14" };
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const MAX_POINTS = 3600; // one hour of one-second samples

const state = {
  ws: null,
  view: "live",    // page shown: "live", "usage", "ping", "router", "connections", "map" or "settings"
  settings: { adapter: null, units: "bits" },
  ping: null,      // latest ping statistics (internet target)
  pingGateway: null, // ... and of the gateway
  pingRange: "1h", // Ping page: "1h", "24h", "7d" or "30d"
  usage: null,     // today / week / month totals and the quota (refreshed every minute)
  usageRange: "days",
  router: null,    // the Router tab: connection status and whole-network totals
  routerRange: "days",
  geoip: null,     // the GeoIP database: installed? downloading?
  region: "",      // the country set in Windows (for "Automatic" home country)
  capture: null,   // the packet capture (per-program traffic): on, off, Npcap missing...
  programs: null,  // live speeds per program (from the capture)
  usageSplit: "direction",       // Usage page: "direction" (down / up) or "origin" (domestic / intl)
  usagePrograms: null,           // Usage page: programs in the chosen period
  usageProgramsPeriod: "day",
  usageProgramsAll: false,
  system: null,    // start with Windows, data folder, database size
  range: 300,      // seconds shown in the chart
  started: 0,      // when NetMon started (unix seconds)
  times: [],       // unix seconds
  downs: [],       // bytes/s, null = gap (e.g. PC was asleep)
  ups: [],
  tick: null,      // latest full measurement
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// ---------- number formatting (same rules as netmon/fmt.py) ----------

const UNITS = {
  bits: { names: ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"], step: 1000, factor: 8 },
  bytes: { names: ["B/s", "KB/s", "MB/s", "GB/s"], step: 1024, factor: 1 },
};
const SIZE_NAMES = ["B", "KB", "MB", "GB", "TB"];

function scale(value, names, step) {
  let i = 0;
  while (value >= step && i < names.length - 1) { value /= step; i++; }
  const digits = i === 0 ? 0 : value < 10 ? 2 : value < 100 ? 1 : 0; // three significant digits
  return [value.toFixed(digits), names[i]];
}
const units = () => UNITS[state.settings.units];
const displayParts = (v) => scale(v, units().names, units().step);          // v already in display units
const rateParts = (bytesPerSec) => displayParts(bytesPerSec * units().factor);
const rate = (bytesPerSec) => rateParts(bytesPerSec).join(" ");
const size = (bytes) => scale(bytes, SIZE_NAMES, 1024).join(" ");
const linkSpeed = (bits) => (bits ? scale(bits, UNITS.bits.names, 1000).join(" ") : "–");
const percent = (x) => (x < 0.01 && x > 0 ? "<1%" : `${Math.round(x * 100)}%`);
const clock = (t, seconds = true) =>
  new Date(t * 1000).toLocaleTimeString("en-GB", seconds ? {} : { hour: "2-digit", minute: "2-digit" });

function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec / 3600) % 24, m = Math.floor(sec / 60) % 60, s = sec % 60;
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} m`;
  return `${m} m ${String(s).padStart(2, "0")} s`;
}

function rangeLabel(sec) {
  return sec >= 3600 ? `${sec / 3600} hour` : `${sec / 60} min`;
}

// ---------- history ----------

function addPoint(t, down, up) {
  const n = state.times.length;
  if (n && t <= state.times[n - 1]) return; // already have it
  if (n && t - state.times[n - 1] > 3) {    // a gap: break the line instead of bridging it
    state.times.push(state.times[n - 1] + 1);
    state.downs.push(null);
    state.ups.push(null);
  }
  state.times.push(t);
  state.downs.push(down);
  state.ups.push(up);
  const extra = state.times.length - MAX_POINTS;
  if (extra > 60) {
    state.times.splice(0, extra);
    state.downs.splice(0, extra);
    state.ups.splice(0, extra);
  }
}

function firstIndexFrom(t) {
  let lo = 0, hi = state.times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (state.times[mid] < t) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function peaks() {
  const from = firstIndexFrom(state.times.at(-1) - state.range);
  let down = 0, up = 0;
  for (let i = from; i < state.times.length; i++) {
    down = Math.max(down, state.downs[i] || 0);
    up = Math.max(up, state.ups[i] || 0);
  }
  return { down, up };
}

// ---------- chart ----------

let chart = null;

// Axis steps that land on round numbers in the current unit (1000s for bits, 1024s for bytes).
function axisSteps(step) {
  const out = [];
  for (let p = 1; p <= step ** 4; p *= step) {
    for (const m of [1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500]) out.push(m * p);
  }
  return out;
}

function tooltipPlugin() {
  let tip;
  const row = (cls, label, v) =>
    `<div class="row"><span class="swatch ${cls}"></span>${label}<b>${v == null ? "–" : displayParts(v).join(" ")}</b></div>`;
  return {
    hooks: {
      init(u) {
        tip = document.createElement("div");
        tip.className = "tooltip";
        tip.hidden = true;
        u.over.appendChild(tip);
      },
      setCursor(u) {
        const i = u.cursor.idx;
        if (i == null || u.cursor.left < 0 || u.data[1][i] == null) { tip.hidden = true; return; }
        tip.innerHTML = `<div class="time">${clock(u.data[0][i])}</div>` +
          row("down", "Download", u.data[1][i]) + row("up", "Upload", u.data[2][i]);
        tip.hidden = false;
        const x = u.cursor.left, y = u.cursor.top, w = tip.offsetWidth, h = tip.offsetHeight;
        tip.style.left = `${x + 16 + w > u.over.clientWidth ? x - w - 16 : x + 16}px`;
        tip.style.top = `${Math.max(0, Math.min(y - h / 2, u.over.clientHeight - h))}px`;
      },
    },
  };
}

function makeChart() {
  const el = $("chart");
  chart?.destroy();
  const fill = (color) => (u) => {
    const g = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
    g.addColorStop(0, color + "55");
    g.addColorStop(1, color + "06");
    return g;
  };
  const axis = {
    stroke: COLORS.axis,
    grid: { stroke: COLORS.grid, width: 1 },
    ticks: { show: false },
    font: "11px Segoe UI",
  };
  const seriesColor = [null, COLORS.down, COLORS.up];
  chart = new uPlot({
    width: el.clientWidth,
    height: el.clientHeight,
    padding: [10, 8, 0, 0],
    legend: { show: false },
    cursor: {
      y: false,
      drag: { x: false, y: false, setScale: false },
      points: { size: 8, width: 2, fill: COLORS.bg, stroke: (u, i) => seriesColor[i] },
    },
    scales: { x: { time: true, auto: false }, y: { auto: false } },
    axes: [
      { ...axis, space: 80, values: (u, splits) => splits.map((t) => clock(t, state.range <= 300)) },
      {
        ...axis, size: 82, space: 36, incrs: axisSteps(units().step),
        values: (u, splits) => splits.map((v) => { const [n, name] = displayParts(v); return `${+n} ${name}`; }),
      },
    ],
    series: [
      {},
      { label: "Download", stroke: COLORS.down, width: 2, fill: fill(COLORS.down), points: { show: false } },
      { label: "Upload", stroke: COLORS.up, width: 2, fill: fill(COLORS.up), points: { show: false } },
    ],
    plugins: [tooltipPlugin()],
  }, [[], [], []], el);
  renderChart();
}

function renderChart() {
  if (!chart || !state.times.length) return;
  const now = state.times.at(-1);
  const from = now - state.range;
  const start = Math.max(0, firstIndexFrom(from) - 1); // one point before, so the line enters from the edge
  const f = units().factor;
  const scaleBy = (v) => (v == null ? null : v * f);
  const xs = state.times.slice(start);
  const ds = state.downs.slice(start).map(scaleBy);
  const us = state.ups.slice(start).map(scaleBy);
  let top = 0;
  for (let i = 0; i < xs.length; i++) top = Math.max(top, ds[i] || 0, us[i] || 0);
  const floor = 1000 * f; // keep a bit of headroom so idle background noise isn't drawn full-height
  chart.batch(() => {
    chart.setData([xs, ds, us], false);
    chart.setScale("x", { min: from, max: now });
    chart.setScale("y", { min: 0, max: Math.max(top * 1.15, floor) });
  });
}

new ResizeObserver(() => {
  const el = $("chart");
  chart?.setSize({ width: el.clientWidth, height: el.clientHeight });
}).observe($("chart"));

// ---------- the rest of the page ----------

function renderCards() {
  const tick = state.tick;
  if (!tick) return;
  const peak = peaks();
  const linkBits = tick.adapters.filter((a) => a.monitored && a.connected).reduce((s, a) => s + a.link, 0);
  for (const dir of ["down", "up"]) {
    const [value, unit] = rateParts(tick[dir]);
    $(`${dir}-value`).textContent = value;
    $(`${dir}-unit`).textContent = unit;
    $(`${dir}-peak`).textContent = `Peak ${rate(peak[dir])} · last ${rangeLabel(state.range)}`;
    $(`${dir}-util`).textContent = linkBits ? `${percent((tick[dir] * 8) / linkBits)} of ${linkSpeed(linkBits)} link` : "";
  }
  $("rx-total").textContent = size(tick.rx_total);
  $("tx-total").textContent = size(tick.tx_total);
  $("uptime").textContent = duration(tick.t - state.started);
}

function renderAdapters() {
  const tick = state.tick;
  if (!tick) return;
  $("adapter-rows").innerHTML = tick.adapters.map((a) => `
    <tr class="${a.connected ? "" : "off"}">
      <td><div>${esc(a.name)}</div><div class="desc">${esc(a.description)}</div></td>
      <td>${a.physical ? "Physical" : "Virtual"}</td>
      <td><span class="pill ${a.connected ? "ok" : ""}">${a.connected ? "Connected" : "Disconnected"}</span></td>
      <td class="num">${linkSpeed(a.link)}</td>
      <td class="num">${a.connected ? rate(a.down) : "–"}</td>
      <td class="num">${a.connected ? rate(a.up) : "–"}</td>
      <td>${a.monitored ? "Counted" : "—"}</td>
    </tr>`).join("");

  // Adapter picker: rebuild only when the list of adapters changes.
  const select = $("adapter");
  const options = [["", "All physical adapters"], ...tick.adapters.map((a) => [a.guid, a.physical ? a.name : `${a.name} (virtual)`])];
  const saved = state.settings.adapter;
  if (saved && !tick.adapters.some((a) => a.guid === saved)) options.push([saved, "Saved adapter (not present)"]);
  const key = JSON.stringify(options);
  if (select.dataset.key !== key) {
    select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
    select.dataset.key = key;
  }
  if (document.activeElement !== select) select.value = saved || "";

  const chosen = tick.adapters.find((a) => a.guid === saved);
  $("monitoring").textContent = `Monitoring: ${saved ? (chosen ? chosen.name : "saved adapter (not present)") : "all physical adapters"}`;
}

// ---------- programs (packet capture) ----------

function captureProblem() {
  const c = state.capture || { state: "off" };
  if (c.state === "on") return "";
  if (c.state === "off") return "Per-program traffic is off – turn it on in Settings.";
  if (c.state === "missing") return "Install Npcap (free, from npcap.com) to see which programs use the network – see Settings.";
  if (c.state === "idle") return "No network connection to watch.";
  return c.message || "The packet capture isn't working – see Settings.";
}

function rateBar(down, up, top) {
  return `<i class="bar"><b class="down" style="width:${(down / top) * 100}%"></b><b class="up" style="width:${(up / top) * 100}%"></b></i>`;
}

function renderPrograms() {
  const el = $("live-programs");
  const problem = captureProblem();
  const list = state.programs?.programs || [];
  $("programs-more").textContent = !problem && state.programs?.more ? `+${state.programs.more} more` : "";
  if (problem) { el.innerHTML = `<p class="empty">${esc(problem)}</p>`; return; }
  if (!list.length) { el.innerHTML = `<p class="empty">Quiet – no program is using the network right now.</p>`; return; }
  const top = Math.max(1, ...list.map((p) => p.down + p.up));
  el.innerHTML = list.map((p) => `<button class="prog" data-search="${esc(p.exe || p.name)}" title="${esc(p.exe ? `${p.name} (${p.exe}) – show its connections` : p.name)}">
      ${appIcon(p.id, p)}<span class="name ellipsis">${esc(p.name)}</span>
      ${rateBar(p.down, p.up, top)}
      <span class="rates"><span><i class="swatch down"></i>${rate(p.down)}</span><span><i class="swatch up"></i>${rate(p.up)}</span></span>
    </button>`).join("");
}

$("live-programs").addEventListener("click", (e) => {
  const row = e.target.closest(".prog");
  if (!row) return;
  conn.query = row.dataset.search; // open Connections filtered to this program
  $("conn-search").value = conn.query;
  location.hash = "connections";
});

function renderCaptureStatus() {
  const c = state.capture || { state: "off" };
  const [cls, html] = {
    on: ["ok", `Capturing on ${esc((c.adapters || []).join(", "))} · Npcap ${esc(c.version)}`
      + (c.problems?.length ? ` · ${esc(c.problems.join("; "))}` : "")],
    idle: ["", `Npcap ${esc(c.version || "")} is ready – waiting for a network connection.`],
    off: ["", "Off."],
    missing: ["warn", "Npcap isn't installed. Get it free from <a href=\"https://npcap.com\" target=\"_blank\" rel=\"noopener\">npcap.com</a> (installing it needs admin once). "
      + "In its setup, leave <b>“Restrict Npcap driver's access to Administrators only”</b> unticked."],
    error: ["bad", esc(c.message || "The packet capture isn't working.")],
  }[c.state] || ["", ""];
  $("capture-status").innerHTML = `<span class="dot ${cls}"></span><span>${html}</span>`;
}

// ---------- usage page ----------

const daysLeft = (d) => (d >= 1 ? `${Math.floor(d)} day${Math.floor(d) === 1 ? "" : "s"} left`
  : `${Math.max(1, Math.round(d * 24))} hours left`);

function usageCard(title, period, measured) {
  const [value, unit] = scale(period.rx + period.tx, SIZE_NAMES, 1024);
  const domestic = period.drx + period.dtx, internet = period.irx + period.itx;
  const unmeasured = period.rx + period.tx - internet;
  const split = `Domestic ${size(domestic)} · International ${size(internet - domestic)}`
    + (unmeasured > (period.rx + period.tx) * 0.02 ? ` · ${size(unmeasured)} not measured` : "");
  return `<article class="card">
    <header>${title}<span class="spacer"></span><span class="muted label">${esc(period.label)}</span></header>
    <div class="big">${value}<span class="unit">${unit}</span></div>
    <footer><span><span class="swatch down"></span>${size(period.rx)}</span><span><span class="swatch up"></span>${size(period.tx)}</span></footer>
    ${measured ? `<div class="split" title="Domestic / international is measured by the packet capture">${split}</div>` : ""}
  </article>`;
}

function quotaCard(q) {
  const head = (label = "") => `<header>Quota<span class="spacer"></span><span class="muted label">${esc(label)}</span></header>`;
  if (!q || !q.enabled) {
    return `<article class="card">${head()}<p class="empty">No quota set.</p><a class="link" href="#settings">Set one in Settings →</a></article>`;
  }
  if (q.waiting) return `<article class="card">${head()}<p class="empty">${esc(q.waiting)}</p><a class="link" href="#settings">Quota settings →</a></article>`;
  const pct = q.percent;
  const level = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
  const overPace = q.projected > q.limit;
  return `<article class="card">${head(q.label)}
    <div class="big">${pct >= 10 ? Math.floor(pct) : pct.toFixed(1)}<span class="unit">% used${pct >= 100 ? " – limit reached" : ""}</span></div>
    <div class="progress ${level}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"><i style="width:${Math.min(100, pct)}%"></i></div>
    <footer><span>${size(q.used)} of ${size(q.limit)}</span><span>${size(q.left)} left</span></footer>
    <footer><span>${daysLeft(q.days_left)}</span><span>${q.left && q.days_left >= 1 ? `≈ ${size(q.per_day)} / day` : ""}</span></footer>
    <div class="pace">${overPace ? "⚠ " : ""}At this pace: ≈ ${size(q.projected)} by the end${q.download_only ? " (download only)" : ""}</div>
    ${q.source === "router" ? `<div class="pace">Whole network (router)${q.domestic_factor < 1
      ? ` · domestic counted at ${Math.round(q.domestic_factor * 100)}%` : ""}</div>`
      : q.domestic_factor < 1 ? `<div class="pace">Domestic counted at ${Math.round(q.domestic_factor * 100)}%</div>` : ""}
  </article>`;
}

function renderUsageCards() {
  const u = state.usage;
  if (!u) return;
  const measured = u.summary.captured_since != null; // domestic traffic is known since the capture started
  $("usage-cards").innerHTML = usageCard("Today", u.summary.day, measured) + usageCard("This week", u.summary.week, measured)
    + usageCard("This month", u.summary.month, measured) + quotaCard(u.quota);
}

// ---------- stacked bar chart (Usage and Router pages) ----------

// Round axis steps in bytes (1024-based, like the sizes everywhere else).
function sizeStep(max) {
  const target = max / 4;
  for (let p = 1; ; p *= 1024) {
    for (const m of [1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500]) if (m * p >= target) return m * p;
  }
}

// A bar with rounded top corners only (the end away from the baseline).
function barPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// The two ways a bar can be split; the first series sits at the bottom.
const DIRECTION = [
  { label: "Download", cls: "down", value: (b) => b.rx },
  { label: "Upload", cls: "up", value: (b) => b.tx },
];
const ORIGIN = [
  { label: "Domestic", cls: "dom", value: (b) => b.drx + b.dtx },
  { label: "International", cls: "intl", value: (b) => b.rx + b.tx - b.drx - b.dtx },
];
// This PC: only the packet capture knows where traffic went; earlier hours count as "not measured".
const MEASURED_ORIGIN = [
  ORIGIN[0],
  { label: "International", cls: "intl", value: (b) => b.irx + b.itx - b.drx - b.dtx },
];

function stackedBars(chartId, tableId, emptyText) {
  const el = $(chartId);
  const bars = {
    data: null,         // {range, buckets: [{label, short, current, rx, tx, ...}]}
    series: DIRECTION,
    mode: "chart",      // or "table"

    render() {
      el.hidden = this.mode !== "chart";
      $(tableId).hidden = this.mode !== "table";
      if (this.data) this.mode === "table" ? this.table() : this.chart();
    },

    chart() {
      if (!this.data || !el.clientWidth) return; // hidden page: drawn when it shows (resize)
      const [lo, hi] = this.series;
      const W = el.clientWidth, H = el.clientHeight;
      const pad = { l: 72, r: 10, t: 14, b: 28 };
      const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
      const list = this.data.buckets;
      const most = Math.max(0, ...list.map((b) => lo.value(b) + hi.value(b)));
      const step = sizeStep(Math.max(most, 1024 ** 2));
      const top = Math.max(step, Math.ceil(most / step) * step);
      const y = (v) => pad.t + ph - (v / top) * ph;
      const slot = pw / list.length;
      const barW = Math.max(3, Math.min(34, slot * 0.64));
      const every = Math.ceil(list.length / Math.max(1, Math.floor(pw / 56))); // keep axis labels apart

      let svg = `<svg width="${W}" height="${H}" role="img" aria-label="${lo.label} and ${hi.label} per ${this.data.range.slice(0, -1)}">`;
      for (let v = 0; v <= top + 1; v += step) {
        svg += `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/>`
          + `<text class="axis" x="${pad.l - 10}" y="${y(v)}" text-anchor="end" dominant-baseline="middle">${v ? size(v) : "0"}</text>`;
      }
      list.forEach((b, i) => {
        const x = pad.l + i * slot + (slot - barW) / 2;
        const base = y(0);
        const a = Math.max(0, lo.value(b)), c = Math.max(0, hi.value(b));
        let ha = (a / top) * ph, hc = (c / top) * ph;
        if (a && ha < 1) ha = 1; // tiny but not zero: still visible
        if (c && hc < 1) hc = 1;
        const gap = ha > 4 && hc > 0 ? 2 : 0; // a sliver of background between the two parts
        const est = b.estimated ? " est" : ""; // pale: shared out over a gap - an estimate
        if (ha) svg += `<path class="bar ${lo.cls}${est}" d="${barPath(x, base - ha + gap, barW, ha - gap, hc ? 0 : 4)}"/>`;
        if (hc) svg += `<path class="bar ${hi.cls}${est}" d="${barPath(x, base - ha - hc, barW, hc, 4)}"/>`;
        if ((list.length - 1 - i) % every === 0) {
          svg += `<text class="axis" x="${x + barW / 2}" y="${H - 9}" text-anchor="middle">${esc(b.short)}</text>`;
        }
        svg += `<rect class="hit" data-i="${i}" x="${pad.l + i * slot}" y="${pad.t}" width="${slot}" height="${ph}"/>`;
      });
      svg += "</svg>";
      if (!most) svg += `<div class="empty">${esc(emptyText)}</div>`;
      el.innerHTML = svg + `<div class="tooltip" hidden></div>`;
    },

    table() {
      const [lo, hi] = this.series;
      const rows = [...this.data.buckets].reverse().map((b) => `<tr>
          <td>${esc(b.label)}${b.current ? ' <span class="muted">(so far)</span>' : ""}${b.estimated ? ' <span class="muted">(estimated)</span>' : ""}</td>
          <td class="num">${size(lo.value(b))}</td><td class="num">${size(hi.value(b))}</td>
          <td class="num">${size(lo.value(b) + hi.value(b))}</td>
        </tr>`).join("");
      $(tableId).innerHTML = `<table class="table"><thead><tr><th>Period</th><th class="num">${lo.label}</th>`
        + `<th class="num">${hi.label}</th><th class="num">Total</th></tr></thead><tbody>${rows}</tbody></table>`;
    },
  };

  el.addEventListener("mousemove", (e) => {
    const tip = el.querySelector(".tooltip");
    const hit = e.target.closest(".hit");
    if (!tip) return;
    if (!hit) { tip.hidden = true; return; }
    const b = bars.data.buckets[Number(hit.dataset.i)];
    const row = (cls, label, v) => `<div class="row">${cls ? `<span class="swatch ${cls}"></span>` : ""}${label}<b>${size(v)}</b></div>`;
    tip.innerHTML = `<div class="time">${esc(b.label)}${b.current ? " (so far)" : ""}${b.estimated ? " · estimated" : ""}</div>`
      + bars.series.map((s) => row(s.cls, s.label, s.value(b))).join("")
      + row("", "Total", bars.series[0].value(b) + bars.series[1].value(b));
    tip.hidden = false;
    const box = el.getBoundingClientRect();
    const x = e.clientX - box.left, yPos = e.clientY - box.top;
    tip.style.left = `${x + 16 + tip.offsetWidth > box.width ? x - tip.offsetWidth - 16 : x + 16}px`;
    tip.style.top = `${Math.max(0, Math.min(yPos - tip.offsetHeight / 2, box.height - tip.offsetHeight))}px`;
  });
  el.addEventListener("mouseleave", () => {
    const tip = el.querySelector(".tooltip");
    if (tip) tip.hidden = true;
  });
  new ResizeObserver(() => bars.mode === "chart" && bars.chart()).observe(el);
  return bars;
}

const usageBars = stackedBars("usage-chart", "usage-table",
  "No usage recorded in this period yet – NetMon records traffic while it runs.");

function renderUsage() {
  renderUsageCards();
  usageBars.render();
  renderUsageHint();
  renderUsagePrograms();
}

function requestUsage() {
  send({ type: "usage", range: state.usageRange });
  send({ type: "usage_programs", period: state.usageProgramsPeriod });
}

function renderUsageLegend() {
  $("usage-legend").innerHTML = usageBars.series.map((s) => `<span><i class="swatch ${s.cls}"></i>${s.label}</span>`).join("");
}

function capturedSince() {
  const since = state.usage?.summary?.captured_since;
  return since ? new Date(since * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
}

function renderUsageHint() {
  const s = state.settings, capturing = state.capture?.state === "on";
  let text = "NetMon records usage while it runs (physical adapters, or the adapter picked at the top). "
    + "Weeks start on Saturday; months follow the Jalali calendar. Updated every minute.";
  if (s.usage_exclude_lan && (capturing || capturedSince())) text += " Local network (LAN) traffic isn't counted.";
  if (state.usageSplit === "origin") {
    text += capturedSince() ? ` Domestic / international is measured by the packet capture, since ${capturedSince()}.`
      : " Domestic / international needs the packet capture (Settings → Per-program traffic).";
  }
  $("usage-hint").textContent = text;
}

function historyIcon(p) {
  if (p.icon) return `<img class="icon" src="/api/program-icon/${p.id}?token=${encodeURIComponent(TOKEN)}" alt="">`;
  return appIcon(0, { name: p.name, icon: false });
}

const TOP_PROGRAMS = 15;

function renderUsagePrograms() {
  markSegment("usage-programs-period", state.usageProgramsPeriod);
  const data = state.usagePrograms, el = $("usage-programs");
  if (!data || data.period !== state.usageProgramsPeriod) return; // its numbers are on the way
  $("usage-programs-note").textContent = data.label;
  if (!data.programs.length) {
    el.innerHTML = `<p class="empty">${capturedSince() ? "Nothing recorded in this period yet."
      : "Per-program usage is recorded by the packet capture – see Settings → Per-program traffic."}</p>`;
    return;
  }
  const [lo, hi] = state.usageSplit === "origin" ? ORIGIN : DIRECTION; // a program's rx/tx are all measured
  const list = state.usageProgramsAll ? data.programs : data.programs.slice(0, TOP_PROGRAMS);
  const top = Math.max(1, ...data.programs.map((p) => p.rx + p.tx));
  const rows = list.map((p) => `<tr>
      <td><div class="app">${historyIcon(p)}<div class="ellipsis"><div class="ellipsis" title="${esc(p.path || p.name)}">${esc(p.name)}</div><div class="sub">${esc(p.exe || "—")}</div></div></div></td>
      <td class="num">${size(Math.max(0, lo.value(p)))}</td>
      <td class="num">${size(Math.max(0, hi.value(p)))}</td>
      <td class="num"><b>${size(p.rx + p.tx)}</b></td>
      <td class="bar-cell"><i class="bar"><b class="${lo.cls}" style="width:${(Math.max(0, lo.value(p)) / top) * 100}%"></b><b class="${hi.cls}" style="width:${(Math.max(0, hi.value(p)) / top) * 100}%"></b></i></td>
    </tr>`).join("");
  const more = data.programs.length - TOP_PROGRAMS;
  el.innerHTML = `<table class="table programs-table"><thead><tr><th>Program</th><th class="num">${lo.label}</th>`
    + `<th class="num">${hi.label}</th><th class="num">Total</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
    + (more > 0 ? `<button class="link more" id="usage-programs-more">${state.usageProgramsAll ? "Show the top " + TOP_PROGRAMS
      : `Show all ${data.programs.length} programs`}</button>` : "");
}

$("usage-programs").addEventListener("click", (e) => {
  if (!e.target.closest("#usage-programs-more")) return;
  state.usageProgramsAll = !state.usageProgramsAll;
  renderUsagePrograms();
});

onSegment("usage-programs-period", (value) => {
  state.usageProgramsPeriod = value;
  state.usageProgramsAll = false;
  markSegment("usage-programs-period", value);
  send({ type: "usage_programs", period: value });
});

onSegment("usage-split", (value) => {
  state.usageSplit = value;
  usageBars.series = value === "origin" ? MEASURED_ORIGIN : DIRECTION;
  markSegment("usage-split", value);
  renderUsageLegend();
  usageBars.render();
  renderUsageHint();
  renderUsagePrograms();
});

onSegment("usage-range", (value) => { state.usageRange = value; markSegment("usage-range", value); requestUsage(); });
onSegment("usage-mode", (value) => { usageBars.mode = value; markSegment("usage-mode", value); usageBars.render(); });

// ---------- router page ----------

const routerBars = stackedBars("router-chart", "router-table", "No data from the router in this period yet.");
const RELAY_URL = /^https?:\/\/[A-Za-z0-9.\-]+(:\d+)?(\/[\w.\-\/]*)?$/;
const RELAY_TOKEN = /^[A-Za-z0-9_\-]{16,128}$/;

function ago(seconds) {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

function routerCard(title, p, measured) {
  const [value, unit] = scale(p.rx + p.tx, SIZE_NAMES, 1024);
  const domestic = p.drx + p.dtx;
  return `<article class="card">
    <header>${title}<span class="spacer"></span><span class="muted label">${esc(p.label)}</span></header>
    <div class="big">${value}<span class="unit">${unit}</span></div>
    <footer><span><span class="swatch down"></span>${size(p.rx)}</span><span><span class="swatch up"></span>${size(p.tx)}</span></footer>
    <div class="split">${measured ? `Domestic ${size(domestic)} · International ${size(p.rx + p.tx - domestic)}`
      : "Domestic share: not measured"}</div>
  </article>`;
}

function renderRouterStatus() {
  const status = state.router?.status || { state: "off" };
  const r = status.routers?.[0];
  const direct = status.mode === "direct";
  const line = {
    off: ["", "The MikroTik integration is off."],
    setup: ["", direct ? "Not connected yet – make the setup script below, paste it into the router, then press Test."
      : "Not connected yet – enter your relay's address and read token below."],
    error: ["bad", `Can't get the router's data: ${esc(status.message)}`],
    waiting: ["warn", "Connected to the relay – waiting for the router's first report (install the router script below)."],
    late: ["warn", r ? `Router <b>${esc(r.router)}</b> hasn't reported for ${ago(status.lag)} – is it online? Its scheduler is “netmon-feed”.` : ""],
    ok: ["ok", r ? (direct ? `Router <b>${esc(r.router)}</b> · ${esc(r.iface)} · read directly every minute · router up ${duration(r.uptime)}`
      : `Router <b>${esc(r.router)}</b> · ${esc(r.iface)} · last report ${ago(status.lag)} ago · router up ${duration(r.uptime)}`) : ""],
  }[status.state] || ["", ""];
  // Direct reading: a router restart while NetMon wasn't reading lost some traffic.
  const gaps = (status.gaps || []).slice(-3).map((g) => `<div class="gap">The router restarted on ${esc(dateTime(g.to))}
    while NetMon wasn't reading it – its traffic between ${esc(dateTime(g.from))} and the restart is unknown.</div>`).join("");
  $("router-status").innerHTML = `<span class="dot ${line[0]}"></span><span>${line[1]}${gaps}</span>`;
  const hasData = ["ok", "late"].includes(status.state);
  $("router-cards").hidden = !hasData;
  $("router-history").hidden = !hasData;
}

function renderRouter() {
  renderRouterStatus();
  const summary = state.router?.summary;
  if (summary) {
    const measured = summary.month.drx + summary.month.dtx > 0;
    const q = state.usage?.quota;
    const withQuota = q && q.enabled && q.source === "router";
    $("router-cards").className = `cards ${withQuota ? "four" : "three"}`;
    $("router-cards").innerHTML = routerCard("Today", summary.day, measured) + routerCard("This week", summary.week, measured)
      + routerCard("This month", summary.month, measured) + (withQuota ? quotaCard(q) : "");
  }
  routerBars.render();
  const estimated = (routerBars.data?.buckets || []).some((b) => b.estimated);
  $("router-hint").textContent = "Everything that went through the router's internet connection – all devices – including hours when this PC was off."
    + (estimated ? " Pale bars are estimates: nothing read the router for a while, so that traffic is shared out evenly over those hours." : "");
}

function renderRouterLegend() {
  $("router-legend").innerHTML = routerBars.series.map((s) => `<span><i class="swatch ${s.cls}"></i>${s.label}</span>`).join("");
}

function requestRouter() {
  if (state.settings.router_enabled) send({ type: "router_usage", range: state.routerRange });
}

function dateTime(seconds) {
  return new Date(seconds * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function routerNote(id, text, kind = "") {
  $(id).textContent = text;
  $(id).className = `form-note ${kind}`;
}

onSegment("router-range", (value) => { state.routerRange = value; markSegment("router-range", value); requestRouter(); });
onSegment("router-mode", (value) => { routerBars.mode = value; markSegment("router-mode", value); routerBars.render(); });
onSegment("router-split", (value) => {
  routerBars.series = value === "origin" ? ORIGIN : DIRECTION;
  markSegment("router-split", value);
  renderRouterLegend();
  routerBars.render();
});

$("router-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = $("router-url").value.trim(), token = $("router-token").value.trim();
  if (!RELAY_URL.test(url)) return routerNote("router-note", "The address should look like https://netmon.example.com", "error");
  if (!RELAY_TOKEN.test(token)) return routerNote("router-note", "Paste the relay's read token (shown by its setup page, setup.php).", "error");
  send({ type: "settings", router_url: url, router_token: token });
  routerNote("router-note", "Saved – connecting…");
  document.activeElement.blur();
});

onSegment("router-way", (value) => {
  markSegment("router-way", value);
  showRouterWay(value);
  send({ type: "settings", router_mode: value });
});

function showRouterWay(way) {
  $("router-direct").hidden = way !== "direct";
  $("router-relay").hidden = way !== "relay";
}

$("direct-form").addEventListener("submit", (e) => {
  e.preventDefault();
  routerNote("direct-note", "");
  send({ type: "router_direct_script", host: $("direct-host").value.trim(), wan: $("direct-wan").value.trim(),
         list: $("direct-list").value.trim() });
});

$("direct-test").addEventListener("click", () => {
  const host = $("direct-host").value.trim(), wan = $("direct-wan").value.trim();
  routerNote("direct-note", "Testing…");
  state.directTest = { host, wan };
  send({ type: "router_test", mode: "direct", host, wan });
});

$("direct-copy").addEventListener("click", async () => {
  const text = $("direct-output").value;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    $("direct-output").select();
    document.execCommand("copy");
  }
  routerNote("direct-note", "Copied – paste it into the router's terminal, then press Test.", "ok");
});

$("router-test").addEventListener("click", () => {
  routerNote("router-note", "Testing…");
  send({ type: "router_test", url: $("router-url").value.trim(), token: $("router-token").value.trim() });
});

$("script-form").addEventListener("submit", (e) => {
  e.preventDefault();
  routerNote("script-note", "");
  send({ type: "router_script", url: $("router-url").value.trim(), token: $("script-token").value.trim(),
         wan: $("script-wan").value.trim(), list: $("script-list").value.trim() });
});

$("script-copy").addEventListener("click", async () => {
  const text = $("script-output").value;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    $("script-output").select();
    document.execCommand("copy");
  }
  routerNote("script-note", "Copied – paste it into the router's terminal.", "ok");
});

function fillRouterForm(s) {
  $("router-tab").hidden = !s.router_enabled;
  if (!s.router_enabled && state.view === "router") location.hash = "settings";
  markSegment("router-way", s.router_mode);
  showRouterWay(s.router_mode);
  if (!$("router-form").contains(document.activeElement)) {
    $("router-url").value = s.router_url || "";
    $("router-token").value = s.router_token || "";
  }
  if (!$("direct-form").contains(document.activeElement)) {
    if (s.router_host) $("direct-host").value = s.router_host;
    if (s.router_wan) $("direct-wan").value = s.router_wan;
  }
}

// ---------- connections page ----------

// Well-known ports, to say what a connection is for.
const PORTS = {
  20: "FTP data", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 67: "DHCP", 68: "DHCP", 80: "HTTP",
  110: "POP3", 123: "NTP", 135: "RPC", 137: "NetBIOS", 138: "NetBIOS", 139: "NetBIOS", 143: "IMAP", 161: "SNMP",
  389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 500: "IKE", 514: "Syslog", 587: "SMTP", 636: "LDAPS",
  853: "DNS over TLS", 993: "IMAPS", 995: "POP3S", 1194: "OpenVPN", 1433: "SQL Server", 1701: "L2TP", 1723: "PPTP",
  1900: "SSDP", 3306: "MySQL", 3389: "Remote Desktop", 3478: "STUN", 4500: "IPsec", 5222: "XMPP", 5228: "Google Play",
  5353: "mDNS", 5355: "LLMNR", 5900: "VNC", 8080: "HTTP", 8291: "Winbox", 8443: "HTTPS", 8728: "RouterOS API",
  8729: "RouterOS API", 51820: "WireGuard",
};
const STATE_CLASS = { Established: "ok", Active: "ok", Listening: "listen", "SYN sent": "warn", "SYN received": "warn" };
const LOCAL_SCOPES = ["loopback", "lan", "multicast"];
const SCOPE_LABEL = {
  loopback: ["This PC", "Loopback – a program talking to another program on this PC."],
  lan: ["Local network", "A device in your local network (or the router)."],
  multicast: ["Multicast", "Sent to many devices at once (discovery, streaming)."],
  special: ["Reserved address", "Not a normal internet address: inside the ISP (carrier-grade NAT), or the "
    + "“fake IP” a TUN-mode proxy (Clash, sing-box) hands out."],
};
// Every country with a flag (web/vendor/flags), for the Home country list.
const COUNTRY_CODES = ("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS "
  + "BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK "
  + "FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM "
  + "JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS "
  + "MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW "
  + "SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM "
  + "US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW").split(" ");
const FLAGS = new Set(COUNTRY_CODES);
const regionNames = new Intl.DisplayNames(["en"], { type: "region" });
const countryName = (cc) => { try { return regionNames.of(cc); } catch { return cc; } };

const conn = {
  data: null,           // the latest update from NetMon
  rows: new Map(),      // key -> {row, born, gone}: what's on screen, incl. just-closed connections
  places: {},           // remote IP -> {scope, cc, country, city, org, host...}
  apps: {},             // program id -> {name, exe, path, note, icon}
  sort: { key: "app", dir: 1 },
  query: "",
  country: "",          // show only this country ("" = all)
  place: null,          // show only this place {key, label} (from the map)
  paused: false,
  waiting: null,        // the update that arrived while paused
  selected: null,       // key of the connection in the detail panel
  open: new Set(),      // "By program": the programs opened up
  watching: false,
  detailHtml: "",
};

// NetMon only reads the connections while a window shows this tab or the map.
function updateWatch() {
  const want = ["connections", "map"].includes(state.view) && !document.hidden && state.ws?.readyState === WebSocket.OPEN;
  if (want === conn.watching) return;
  conn.watching = want;
  if (want) { conn.data = null; conn.rows.clear(); } // fresh start: nothing flashes as "new"
  send({ type: "connections_watch", on: want });
}

function onConnections(msg) {
  if (conn.paused) { conn.waiting = msg; return; }
  applyConnections(msg);
}

function applyConnections(msg) {
  const first = !conn.data;
  const seen = new Set();
  for (const row of msg.rows) {
    seen.add(row.key);
    const known = conn.rows.get(row.key);
    if (known) { known.row = row; known.gone = 0; }
    else conn.rows.set(row.key, { row, born: first ? 0 : msg.t, gone: 0 });
  }
  const places = msg.places, apps = msg.apps;
  for (const [key, entry] of conn.rows) {
    if (seen.has(key)) continue;
    if (!entry.gone) entry.gone = msg.t;
    if (msg.t - entry.gone > 3) { conn.rows.delete(key); continue; } // closed: shown in red for a moment
    const r = entry.row; // keep what we knew about it while it fades out
    if (r.remote && !places[r.remote] && conn.places[r.remote]) places[r.remote] = conn.places[r.remote];
    if (!apps[r.app] && conn.apps[r.app]) apps[r.app] = conn.apps[r.app];
  }
  conn.places = places;
  conn.apps = apps;
  conn.data = msg;
  if (document.hidden) return;
  if (state.view === "connections") renderConnections();
  if (state.view === "map") renderMap();
}

const placeOf = (r) => (r.remote ? conn.places[r.remote] || { scope: "internet" } : null);
const appOf = (r) => conn.apps[r.app] || { name: `Process ${r.pid}`, exe: "", icon: false };
const service = (port) => PORTS[port] || "";
const isLocal = (p) => Boolean(p) && LOCAL_SCOPES.includes(p.scope);
const isDomestic = (p) => Boolean(p?.cc) && p.cc === conn.data?.home;

function shown(entry) {
  const r = entry.row, s = state.settings;
  if (!r.remote) return Boolean(s.conn_show_listening) && !conn.country && !conn.place;
  const p = placeOf(r);
  if (s.conn_hide_local && isLocal(p)) return false;
  if (conn.place && (p.lat == null || placeKey(p) !== conn.place.key)) return false;
  return !conn.country || p.cc === conn.country;
}

function haystack(r) {
  const p = placeOf(r) || {}, app = appOf(r);
  return [app.name, app.exe, app.note, r.pid, r.proto, r.remote, r.rport, r.local, r.lport, service(r.rport || r.lport),
    r.state, p.host, p.cc, p.country, p.city, p.region, p.org, p.asn ? `AS${p.asn}` : ""].join(" ").toLowerCase();
}

function matches(entry, words) {
  if (!words.length) return true;
  const text = haystack(entry.row);
  return words.every((w) => text.includes(w));
}

const SORTS = {
  app: (r) => appOf(r).name.toLowerCase(),
  remote: (r) => (r.remote ? (placeOf(r).host || r.remote).toLowerCase() : "￿"),
  port: (r) => r.rport || r.lport,
  country: (r) => { const p = placeOf(r); return p?.country ? p.country.toLowerCase() : `￿${p?.scope || "~"}`; },
  org: (r) => (placeOf(r)?.org || "￿").toLowerCase(),
  state: (r) => r.state || "UDP",
  age: (r) => (r.opened ? -r.opened : -Infinity), // ascending = open the shortest first
  speed: (r) => -((r.down || 0) + (r.up || 0)),    // ascending = fastest first
  data: (r) => -((r.rx || 0) + (r.tx || 0)),
};

function sortEntries(list) {
  const value = SORTS[conn.sort.key], dir = conn.sort.dir;
  return list.sort((a, b) => {
    const x = value(a.row), y = value(b.row);
    return (a.row.pid === 0) - (b.row.pid === 0) // already-closed connections always at the bottom
      || (x < y ? -1 : x > y ? 1 : 0) * dir || (a.row.key < b.row.key ? -1 : 1);
  });
}

// ---- cells

const flag = (cc) => (FLAGS.has(cc) ? `<img class="flag" src="vendor/flags/${cc}.svg" alt="">` : "");

function appIcon(id, app) {
  if (app.icon) return `<img class="icon" src="/api/icon/${id}?token=${encodeURIComponent(TOKEN)}" alt="">`;
  const letter = (app.name || "?").replace(/^\W+/, "").charAt(0).toUpperCase() || "?";
  return `<span class="icon generic" aria-hidden="true">${esc(letter)}</span>`;
}

function listenLabel(r) {
  if (r.local === "0.0.0.0" || r.local === "::") return "on all addresses";
  return r.local === "127.0.0.1" || r.local === "::1" ? "on this PC only" : `on ${r.local}`;
}

function remoteCell(r, p) {
  if (!r.remote) {
    return `<div class="muted">${r.proto === "UDP" ? "UDP socket" : "Listening"}</div><div class="sub">${esc(listenLabel(r))}</div>`;
  }
  const main = p.host || r.remote;
  return `<div class="ellipsis" title="${esc(main)}">${esc(main)}</div>`
    + (p.host ? `<div class="sub ellipsis" title="${esc(r.remote)}">${esc(r.remote)}</div>` : "");
}

function placeCell(p) {
  if (!p) return "";
  if (p.scope !== "internet") {
    const [label, about] = SCOPE_LABEL[p.scope];
    return `<div class="muted" title="${esc(about)}">${label}</div>`;
  }
  if (!p.cc) return `<div class="muted">Internet</div>`;
  const where = [...new Set([p.city, p.region].filter(Boolean))].join(", ");
  const home = isDomestic(p);
  return `<div class="place"><i class="dot ${home ? "dom" : "intl"}" title="${home ? "Domestic" : "International"}"></i>`
    + `${flag(p.cc)}<span class="ellipsis">${esc(p.country || p.cc)}</span></div>`
    + (where ? `<div class="sub ellipsis" title="${esc(where)}">${esc(where)}</div>` : "");
}

function orgCell(p) {
  if (!p?.org) return "";
  return `<div class="ellipsis" title="${esc(p.org)}">${esc(p.org)}</div><div class="sub">AS${p.asn}</div>`;
}

function stateCell(r, entry) {
  if (entry.gone) return `<span class="state gone">Closed</span>`;
  const label = r.state || (r.proto === "UDP" ? "Open" : "");
  return `<span class="state ${STATE_CLASS[r.state] || ""}">${esc(label)}</span>`;
}

function speedCell(r) {
  if (r.rx === undefined) return "";
  if (!r.down && !r.up) return `<span class="muted">idle</span>`;
  return `<div>↓ ${rate(r.down)}</div><div class="sub">↑ ${rate(r.up)}</div>`;
}

function dataCell(r) {
  if (r.rx === undefined) return "";
  return `<div title="↓ ${size(r.rx)} · ↑ ${size(r.tx)}">${size(r.rx + r.tx)}</div>`;
}

function connRow(entry, child = false) {
  const r = entry.row, p = placeOf(r), app = appOf(r);
  const port = r.rport || r.lport;
  const cls = [entry.gone ? "gone" : "", r.pid === 0 ? "closing" : "", entry.born && entry.born === conn.data.t ? "new" : "",
    conn.selected === r.key ? "selected" : "", child ? "child" : ""].filter(Boolean).join(" ");
  const age = r.opened ? duration(conn.data.t - r.opened) : "–";
  const who = r.pid === 0 ? "closed, expires soon" : `${esc(app.exe || "—")} · ${r.pid}`;
  const program = child // "By program": the group above already names it
    ? `<div class="sub">PID ${r.pid}</div>`
    : `<div class="app">${appIcon(r.app, app)}<div class="ellipsis"><div class="ellipsis" title="${esc(app.note || app.name)}">${esc(app.name)}</div><div class="sub">${who}</div></div></div>`;
  return `<tr data-key="${esc(r.key)}" class="${cls}">
    <td>${program}</td>
    <td>${remoteCell(r, p || {})}</td>
    <td class="num"><div>${port}</div><div class="sub">${r.proto}${service(port) ? ` · ${service(port)}` : ""}</div></td>
    <td>${placeCell(p)}</td>
    <td>${orgCell(p)}</td>
    <td class="num cap">${speedCell(r)}</td>
    <td class="num cap">${dataCell(r)}</td>
    <td class="num"><div>${age}</div><div class="sub">${stateCell(r, entry)}</div></td>
  </tr>`;
}

function groupRow(id, entries) {
  const app = appOf(entries[0].row);
  const live = entries.filter((e) => !e.gone && e.row.remote).length;
  const listening = entries.filter((e) => !e.row.remote).length;
  const countries = [...new Set(entries.map((e) => placeOf(e.row)?.cc).filter(Boolean))];
  const counts = [live ? `${live} connection${live === 1 ? "" : "s"}` : "", listening ? `${listening} listening` : ""]
    .filter(Boolean).join(" · ");
  const sum = (field) => entries.reduce((total, e) => total + (e.gone ? 0 : e.row[field] || 0), 0);
  const traffic = conn.data.capture
    ? `<span class="gtraffic">↓ ${rate(sum("down"))} · ↑ ${rate(sum("up"))} · ${size(sum("rx") + sum("tx"))}</span>` : "";
  return `<tr class="group${conn.open.has(id) ? " open" : ""}" data-app="${id}"><td colspan="8"><div class="group-line">
    <span class="caret" aria-hidden="true"></span>${appIcon(id, app)}<b>${esc(app.name)}</b><span class="muted">${esc(app.exe)}</span>
    <span class="count">${counts}</span>${traffic}
    <span class="flags" title="${esc(countries.map(countryName).join(", "))}">${countries.slice(0, 10).map(flag).join("")}${countries.length > 10 ? ` +${countries.length - 10}` : ""}</span>
  </div></td></tr>`;
}

// ---- the page

function renderConnections() {
  renderGeoipBanner();
  if (!conn.data) {
    $("conn-rows").innerHTML = "";
    $("conn-empty").hidden = false;
    $("conn-empty").textContent = "Reading the connections…";
    return;
  }
  renderConnStats();
  renderVpn(conn.data.vpn);
  markSegment("conn-view", state.settings.conn_view);
  const words = conn.query.toLowerCase().split(/\s+/).filter(Boolean);
  const all = [...conn.rows.values()];
  const list = sortEntries(all.filter((e) => shown(e) && matches(e, words)));
  let html = "";
  if (state.settings.conn_view === "apps") {
    const groups = new Map();
    for (const e of list) {
      if (!groups.has(e.row.app)) groups.set(e.row.app, []);
      groups.get(e.row.app).push(e);
    }
    const ordered = [...groups].sort((a, b) => b[1].length - a[1].length
      || appOf(a[1][0].row).name.localeCompare(appOf(b[1][0].row).name));
    for (const [id, entries] of ordered) {
      html += groupRow(id, entries);
      if (conn.open.has(id)) html += entries.map((e) => connRow(e, true)).join("");
    }
  } else {
    html = list.map((e) => connRow(e)).join("");
  }
  $("conn-rows").innerHTML = html;
  $("conn-rows").closest("table").classList.toggle("no-capture", !conn.data.capture);
  $("conn-udp-note").hidden = Boolean(conn.data.capture);
  $("conn-empty").hidden = list.length > 0;
  if (!list.length) $("conn-empty").textContent = all.length ? "No connections match the filters." : "No connections right now.";
  $("conn-count").textContent = `Showing ${list.length} of ${all.length} sockets${conn.paused ? " · paused" : ""}`;
  $("conn-credit").textContent = conn.data.geoip ? "· IP geolocation by DB-IP" : "";
  for (const th of $("conn-head").children) {
    th.classList.toggle("sorted", th.dataset.sort === conn.sort.key);
    th.classList.toggle("desc", th.dataset.sort === conn.sort.key && conn.sort.dir < 0);
  }
  renderDetail();
}

function renderConnStats() {
  const live = [...conn.rows.values()].filter((e) => !e.gone && e.row.remote);
  let domestic = 0, international = 0, local = 0, unknown = 0;
  const programs = new Set(), countries = new Map();
  for (const e of live) {
    const p = placeOf(e.row);
    programs.add(e.row.app);
    if (isLocal(p)) local++;
    else if (!p.cc) unknown++;
    else {
      countries.set(p.cc, (countries.get(p.cc) || 0) + 1);
      if (isDomestic(p)) domestic++; else international++;
    }
  }
  const stat = (n, label) => `<div class="stat"><b>${n}</b><small>${label}</small></div>`;
  const part = (cls, n, label) => (n ? `<span><i class="dot ${cls}"></i>${label}<b>${n}</b></span>` : "");
  const home = conn.data.home;
  $("conn-stats").innerHTML = stat(live.length, "connections") + stat(programs.size, "programs") + stat(countries.size, "countries")
    + `<div class="split">${part("dom", domestic, home ? `Domestic (${esc(countryName(home))})` : "Domestic")}`
    + `${part("intl", international, "International")}${part("local", local, "This PC &amp; LAN")}${part("unknown", unknown, "Internet")}</div>`;

  // Country filter: the busiest countries; click one to show only it.
  const top = [...countries].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12);
  if (conn.country && !top.some(([cc]) => cc === conn.country)) top.push([conn.country, countries.get(conn.country) || 0]);
  $("conn-countries").hidden = !top.length && !conn.place;
  $("conn-countries").innerHTML = (conn.place ? `<button class="chip active" data-place-clear title="Show all places">`
      + `Place: ${esc(conn.place.label)} ×</button>` : "")
    + `<span class="muted">Countries</span>`
    + `<button class="chip${conn.country ? "" : " active"}" data-cc="">All</button>`
    + top.map(([cc, n]) => `<button class="chip${cc === conn.country ? " active" : ""}" data-cc="${cc}" title="${esc(countryName(cc))}">${flag(cc)}${cc}<b>${n}</b></button>`).join("");
}

function renderVpn(v) {
  const items = [];
  for (const a of v.adapters) items.push(["vpn", `VPN: ${a.name}`, `A VPN adapter is connected (${a.description}).`]);
  if (v.proxy.enabled) {
    items.push(["proxy", `System proxy ${v.proxy.server}`,
      "Windows' proxy is on: programs that use it connect to the proxy on this PC, so their real destinations "
      + "don't appear here – only the proxy program's own connections do."]);
  }
  if (v.proxy.script) items.push(["proxy", "Proxy script (PAC)", `Windows uses a proxy script: ${v.proxy.script}`]);
  if (v.programs.length) {
    const names = v.programs.map((p) => p.name + (p.active ? "" : " (idle)")).join(", ");
    items.push([v.programs.some((p) => p.active) ? "vpn" : "idle", `VPN app: ${names}`,
      "A known VPN or proxy program is running. “idle” = it has no connection open."]);
  }
  if (!items.length) {
    items.push(["none", "No VPN or proxy on this PC",
      "No VPN adapter, VPN program or system proxy found. A VPN on your router isn't visible from here – "
      + "this PC then sees the real addresses it talks to."]);
  }
  $("conn-vpn").innerHTML = items.map(([cls, text, about]) =>
    `<span class="vpn-pill ${cls}" title="${esc(about)}"><i></i>${esc(text)}</span>`).join("");
}

function renderDetail() {
  const el = $("conn-detail");
  const entry = conn.selected && conn.rows.get(conn.selected);
  if (!entry) {
    el.hidden = true;
    conn.detailHtml = "";
    return;
  }
  const r = entry.row, p = placeOf(r) || {}, app = appOf(r);
  const port = r.rport || r.lport;
  const copy = (text) => `<button class="copy" data-copy="${esc(text)}" title="Copy">Copy</button>`;
  const row = (label, value) => (value ? `<dt>${label}</dt><dd>${value}</dd>` : "");
  let where = "";
  if (r.remote && p.scope === "internet" && p.cc) {
    const home = isDomestic(p);
    where = `${flag(p.cc)}${esc(p.country)} <span class="tag ${home ? "dom" : "intl"}">${home ? "domestic" : "international"}</span>`
      + [p.region, p.city].filter(Boolean).map((v) => `<br>${esc(v)}`).join("");
  } else if (r.remote && SCOPE_LABEL[p.scope]) {
    where = `${SCOPE_LABEL[p.scope][0]}<br><small class="muted">${esc(SCOPE_LABEL[p.scope][1])}</small>`;
  }
  const html = `<header>${appIcon(r.app, app)}<div><b>${esc(app.name)}</b><small>${esc(app.exe || "—")} · PID ${r.pid}</small></div>
      <button class="close" id="conn-detail-close" aria-label="Close" title="Close (Esc)">×</button></header>
    ${app.path ? `<div class="path" title="${esc(app.path)}">${esc(app.path)}</div>` : ""}
    ${app.note ? `<p class="note">${esc(app.note)}</p>` : ""}
    <dl>
      ${r.remote ? row("Remote", `${p.host ? `${esc(p.host)}<br>` : ""}<span class="mono">${esc(r.remote)}</span> ${copy(r.remote)}`)
        : row("Listening", esc(listenLabel(r)))}
      ${row("Port", `${port} · ${r.proto}${service(port) ? ` · ${service(port)}` : ""}`)}
      ${row("Location", where)}
      ${row("Network", p.org ? `${esc(p.org)}<br><span class="muted">AS${p.asn}</span>` : "")}
      ${r.rx !== undefined ? row("Speed", `↓ ${rate(r.down)} · ↑ ${rate(r.up)}`) : ""}
      ${r.rx !== undefined ? row("Data", `↓ ${size(r.rx)} · ↑ ${size(r.tx)}`) : ""}
      ${row("Local", `<span class="mono">${esc(r.local)}:${r.lport}</span>`)}
      ${row("State", entry.gone ? "Closed" : esc(r.state || "Open (UDP)"))}
      ${row("Opened", r.opened ? `${clock(r.opened)} · ${duration(conn.data.t - r.opened)} ago` : "")}
    </dl>
    <div class="actions"><button class="button secondary" id="conn-only-app">Only this program</button>
      ${p.lat != null ? `<button class="button secondary" id="conn-on-map">Show on map</button>` : ""}</div>`;
  if (html !== conn.detailHtml) { // unchanged: leave it alone, so a text selection survives
    el.innerHTML = html;
    conn.detailHtml = html;
  }
  el.hidden = false;
}

// ---- GeoIP database (also on the Settings page)

const mb = (bytes) => `${Math.round(bytes / 1024 / 1024)} MB`;

function editionName(edition) {
  const [y, m] = edition.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

function geoipProgress(g) {
  return `<div class="progress geo"><i style="width:${Math.round(g.progress * 100)}%"></i></div>`
    + `<span class="muted">Downloading… ${g.total ? `${mb(g.downloaded)} of ${mb(g.total)}` : ""}</span>`
    + `<button class="button secondary" data-geoip="cancel">Cancel</button>`;
}

function geoipMessage(g) {
  return g.message ? `<span class="form-note ${g.ok ? "ok" : "error"}">${esc(g.message)}</span>` : "";
}

function renderGeoipBanner(id = "conn-geoip") {
  const g = state.geoip, el = $(id);
  if (!g || g.state === "ready") { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = g.state === "downloading"
    ? `<b>GeoIP database</b>${geoipProgress(g)}`
    : `<span><b>See where each connection goes</b> – country, city and network. This needs DB-IP's free GeoIP database
       (a one-time download of about 65 MB; lookups then stay on this PC).</span>
       <button class="button" data-geoip="download">Download</button>${geoipMessage(g)}`;
}

function renderGeoipSettings() {
  const g = state.geoip;
  if (!g) return;
  let html;
  if (g.state === "downloading") {
    html = `<span><b>GeoIP database</b></span>${geoipProgress(g)}`;
  } else if (g.state === "ready") {
    html = `<span><b>GeoIP database</b><small>DB-IP Lite (city + network), ${editionName(g.edition)} edition · ${mb(g.size)} on disk</small></span>`
      + (g.newer ? `<button class="button" data-geoip="download">Update to ${editionName(g.newer)}</button>`
        : `<button class="button secondary" data-geoip="download">Check for update</button>`)
      + `<button class="button secondary" data-geoip="remove">Remove</button>`;
  } else {
    html = `<span><b>GeoIP database</b><small>Not installed. Shows the country, city and network of each connection.
      About 65 MB to download, 130 MB on disk; DB-IP publishes a new edition every month.</small></span>
      <button class="button" data-geoip="download">Download</button>`;
  }
  $("geoip-settings").innerHTML = `<div class="geoip-line">${html}</div>${geoipMessage(g)}`
    + `<p class="hint">IP geolocation by <a href="https://db-ip.com" target="_blank" rel="noopener">DB-IP</a>, `
    + `licensed under Creative Commons Attribution 4.0.</p>`;
}

function renderGeoip() {
  renderGeoipSettings();
  if (state.view === "connections") renderConnections();
  if (state.view === "map") renderMap();
  if (state.view === "iplookup") renderGeoipBanner("iplookup-geoip");
}

document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-geoip]");
  if (!button) return;
  if (button.dataset.geoip === "remove"
      && !confirm("Remove the GeoIP database? Connections show no countries until you download it again.")) return;
  send({ type: `geoip_${button.dataset.geoip}` });
});

function fillHomeCountry(s) {
  const select = $("home-country");
  if (!select.options.length) {
    const names = COUNTRY_CODES.map((cc) => [cc, countryName(cc)]).sort((a, b) => a[1].localeCompare(b[1]));
    select.replaceChildren(new Option("Automatic", ""), ...names.map(([cc, name]) => new Option(name, cc)));
  }
  select.options[0].textContent = state.region ? `Automatic – from Windows (${countryName(state.region)})` : "Automatic – from Windows";
  if (document.activeElement !== select) select.value = s.home_country || "";
}

$("home-country").addEventListener("change", (e) => send({ type: "settings", home_country: e.target.value }));

// ---- interaction

let searchTimer = 0;
$("conn-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { conn.query = e.target.value; renderConnections(); }, 120);
});

$("conn-head").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  conn.sort = conn.sort.key === key ? { key, dir: -conn.sort.dir } : { key, dir: 1 };
  renderConnections();
});

$("conn-rows").addEventListener("click", (e) => {
  const group = e.target.closest("tr.group");
  if (group) {
    const id = Number(group.dataset.app);
    if (conn.open.has(id)) conn.open.delete(id); else conn.open.add(id);
    renderConnections();
    return;
  }
  const tr = e.target.closest("tr[data-key]");
  if (!tr) return;
  conn.selected = conn.selected === tr.dataset.key ? null : tr.dataset.key;
  renderConnections();
});

$("conn-detail").addEventListener("click", async (e) => {
  if (e.target.closest("#conn-detail-close")) {
    conn.selected = null;
    renderConnections();
  } else if (e.target.closest("#conn-on-map")) {
    const entry = conn.rows.get(conn.selected);
    const p = entry && placeOf(entry.row);
    if (!p || p.lat == null) return;
    map.target = placeKey(p);
    map.program = "";
    location.hash = "map";
  } else if (e.target.closest("#conn-only-app")) {
    const entry = conn.rows.get(conn.selected);
    if (!entry) return;
    const app = appOf(entry.row);
    $("conn-search").value = conn.query = app.exe || app.name;
    renderConnections();
  } else if (e.target.closest("[data-copy]")) {
    const button = e.target.closest("[data-copy]");
    try { await navigator.clipboard.writeText(button.dataset.copy); } catch { return; }
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = "Copy"; }, 1200);
  }
});

$("conn-countries").addEventListener("click", (e) => {
  if (e.target.closest("[data-place-clear]")) {
    conn.place = null;
    renderConnections();
    return;
  }
  const chip = e.target.closest("[data-cc]");
  if (!chip) return;
  conn.country = chip.dataset.cc === conn.country ? "" : chip.dataset.cc;
  renderConnections();
});

$("conn-pause").addEventListener("click", () => {
  conn.paused = !conn.paused;
  $("conn-pause").textContent = conn.paused ? "Resume" : "Pause";
  $("conn-pause").setAttribute("aria-pressed", String(conn.paused));
  $("conn-pause").classList.toggle("on", conn.paused);
  if (!conn.paused && conn.waiting) {
    const msg = conn.waiting;
    conn.waiting = null;
    applyConnections(msg);
  } else {
    renderConnections();
  }
});

onSegment("conn-view", (value) => send({ type: "settings", conn_view: value }));

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.view === "connections" && conn.selected) {
    conn.selected = null;
    renderConnections();
  }
});

// ---------- map page (flat map and 3D globe) ----------

// Arcs from home to every place this PC talks to; brightness and motion show traffic right now.
const NEON = { down: [15, 160, 196], up: [227, 109, 38], home: [61, 139, 253] };
const rgba = ([r, g, b], a) => `rgba(${r},${g},${b},${a})`;

const map = {
  globe: null,
  countries: null,     // GeoJSON of the world's countries (Natural Earth)
  loading: false,
  failed: "",
  places: [],          // what the globe shows; objects stay the same between updates so arcs don't restart
  byKey: new Map(),
  arcs: [],
  rings: [],
  seen: new Set(),     // place keys already shown (a new one gets a ripple)
  selected: "",        // key of the place opened in the side panel
  program: "",         // show only this program (app id), "" = all
  rotate: true,
  resumeTimer: 0,
  target: "",          // a place to open once the map is ready (from Connections' "Show on map")
  home: undefined,     // home country the dots were coloured for
};

const homeDots = (cc) => (f) => (cc && f.properties.cc === cc ? "rgba(70,190,140,0.9)" : "rgba(120,150,200,0.42)");

const placeKey = (p) => `${p.lat.toFixed(2)},${p.lon.toFixed(2)}`;
const mapView = () => (state.settings.map_view === "3d" ? "3d" : "2d");

function loadCountries() {
  map.countriesLoading ||= fetch("vendor/countries/countries-110m.json").then((r) => r.json()).then((data) => {
    map.countries = data;
    if (conn.data && state.view === "map") renderMap(); // the home point is known now
    return data;
  });
  return map.countriesLoading;
}

function homePoint() {
  const cc = conn.data?.home;
  const country = map.countries?.features.find((f) => f.properties.cc === cc);
  return country ? { lat: country.properties.ly, lng: country.properties.lx, cc } : null;
}

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

async function initGlobe() {
  if (map.globe || map.loading || map.failed) return;
  if (!window.Globe) { // the globe library (deferred) is still loading
    window.addEventListener("load", initGlobe, { once: true });
    return;
  }
  map.loading = true;
  try {
    if (!webglAvailable()) throw new Error("3D graphics (WebGL) aren't available in this window.");
    await loadCountries();
    const el = $("globe");
    const globe = new Globe(el, { animateIn: false, waitForGlobeReady: false });
    globe.width(el.clientWidth).height(el.clientHeight)
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true).atmosphereColor("#3d8bfd").atmosphereAltitude(0.17)
      .hexPolygonsData(map.countries.features)
      .hexPolygonResolution(3).hexPolygonMargin(0.25).hexPolygonUseDots(true).hexPolygonDotResolution(6)
      .hexPolygonAltitude(0.004)
      .hexPolygonColor(homeDots(map.home = conn.data?.home))
      .hexPolygonLabel(() => "")
      .arcsTransitionDuration(0)
      .arcStartLat((a) => a.from.lat).arcStartLng((a) => a.from.lng)
      .arcEndLat((a) => a.to.lat).arcEndLng((a) => a.to.lng)
      .arcColor((a) => a.color).arcStroke((a) => a.stroke)
      .arcDashLength((a) => a.dash).arcDashGap((a) => a.gap)
      .arcDashInitialGap((a) => a.offset).arcDashAnimateTime((a) => a.speed)
      .arcAltitudeAutoScale(0.45)
      .arcLabel((a) => placeTooltip(a.place))
      .onArcClick((a) => selectPlace(a.place.key))
      .pointsTransitionDuration(0)
      .pointLat((p) => p.lat).pointLng((p) => p.lon)
      .pointColor((p) => (map.selected && p.key !== map.selected ? "rgba(220,235,255,0.35)" : "rgba(225,240,255,0.95)"))
      .pointAltitude(0.006).pointRadius((p) => p.radius)
      .pointLabel((p) => placeTooltip(p))
      .onPointClick((p) => selectPlace(p.key))
      .ringLat((r) => r.lat).ringLng((r) => r.lng)
      .ringColor((r) => (t) => rgba(r.rgb, Math.max(0, 1 - t)))
      .ringMaxRadius((r) => r.size).ringPropagationSpeed(1.6).ringRepeatPeriod((r) => r.period);
    const material = globe.globeMaterial();
    material.color.set("#0c1626");
    material.emissive.set("#060c17");
    material.shininess = 6;
    const controls = globe.controls();
    controls.autoRotate = map.rotate;
    controls.autoRotateSpeed = 0.35;
    controls.addEventListener("start", pauseRotation); // the user is turning it: stop turning for a while
    new ResizeObserver(() => globe.width(el.clientWidth).height(el.clientHeight)).observe(el);
    map.globe = globe;
    const home = homePoint();
    globe.pointOfView({ lat: home ? home.lat - 8 : 25, lng: home ? home.lng : 30, altitude: 2.3 });
    renderMap();
    if (state.view !== "map" || document.hidden || mapView() !== "3d") globe.pauseAnimation();
  } catch (e) {
    map.failed = String(e.message || e);
    $("map-message").hidden = false;
    $("map-message").textContent = `The globe can't be shown: ${map.failed}`;
  } finally {
    map.loading = false;
  }
}

function pauseRotation() {
  if (!map.globe) return;
  map.globe.controls().autoRotate = false;
  clearTimeout(map.resumeTimer);
  if (map.rotate) map.resumeTimer = setTimeout(() => { if (map.globe) map.globe.controls().autoRotate = map.rotate; }, 30000);
}

function placeTooltip(p) {
  const speed = p.capture ? `<div>↓ ${rate(p.down)} · ↑ ${rate(p.up)}</div>` : "";
  const programs = [...p.apps].map((id) => conn.apps[id]?.name).filter(Boolean);
  return `<div class="globe-tip"><b>${flag(p.cc)}${esc(p.label)}</b>
    <div>${p.count} connection${p.count === 1 ? "" : "s"}${p.orgs.size ? ` · ${esc([...p.orgs][0])}` : ""}</div>${speed}
    <div class="muted">${esc(programs.slice(0, 4).join(", "))}${programs.length > 4 ? ` +${programs.length - 4}` : ""}</div></div>`;
}

// Group the current connections by place (city coordinates from GeoIP).
function collectPlaces() {
  const found = new Map();
  let local = 0, unplaced = 0;
  for (const entry of conn.rows.values()) {
    const r = entry.row;
    if (entry.gone || !r.remote || r.pid === 0) continue; // closed connections (time wait) aren't shown
    if (map.program && String(r.app) !== map.program) continue;
    const p = conn.places[r.remote];
    if (!p || p.scope !== "internet") { local++; continue; }
    if (p.lat == null) { unplaced++; continue; }
    const key = placeKey(p);
    let place = found.get(key);
    if (!place) {
      const city = [p.city, p.region].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i)[0];
      place = { key, lat: p.lat, lon: p.lon, cc: p.cc, country: p.country, city: p.city || "", region: p.region || "",
                label: city ? `${city}, ${p.country}` : p.country, count: 0, down: 0, up: 0, data: 0,
                apps: new Set(), orgs: new Set(), rows: [], capture: Boolean(conn.data.capture) };
      found.set(key, place);
    }
    place.count++;
    place.down += r.down || 0;
    place.up += r.up || 0;
    place.data += (r.rx || 0) + (r.tx || 0);
    place.apps.add(r.app);
    if (p.org) place.orgs.add(p.org);
    place.rows.push(entry);
  }
  return { found, local, unplaced };
}

function renderMap() {
  renderGeoipBanner("map-geoip");
  if (!conn.data) { $("map-stats").textContent = "Reading the connections…"; return; }
  fillMapPrograms();
  const { found, local, unplaced } = collectPlaces();
  const places = [...found.values()].sort((a, b) => (b.down + b.up) - (a.down + a.up) || b.count - a.count
    || a.label.localeCompare(b.label));
  const countries = new Set(places.map((p) => p.cc));
  $("map-stats").innerHTML = `<b>${places.length}</b> destination${places.length === 1 ? "" : "s"} in <b>${countries.size}</b> `
    + `countr${countries.size === 1 ? "y" : "ies"}`
    + (local ? `<span class="muted"> · ${local} local / LAN not on the map</span>` : "")
    + (unplaced ? `<span class="muted"> · ${unplaced} without a location</span>` : "");
  $("map-count").textContent = places.length ? `${places.length}` : "";
  map.places = places;
  if (map.selected && !found.has(map.selected) && !map.byKey.has(map.selected)) map.selected = "";
  renderPlaceList();
  renderPlaceDetail();
  updatePlaces();
  updateGlobe();
  if (flat.home !== conn.data.home) { // the home country's dots turn green
    flat.home = conn.data.home;
    flat.dirty = true;
  }
  if (map.target && found.has(map.target)) {
    const key = map.target;
    map.target = "";
    selectPlace(key);
  }
}

// What both views draw: one object per place (kept between updates, so dashes keep flowing
// instead of restarting), with its traffic level and the way its data flows.
function updatePlaces() {
  const home = homePoint();
  const busiest = Math.max(1, ...map.places.map((p) => p.down + p.up));
  const keep = new Map();
  for (const p of map.places) {
    const known = map.byKey.get(p.key);
    const place = known ? Object.assign(known, p) : p;
    keep.set(p.key, place);
    place.radius = 0.16 + 0.1 * Math.sqrt(p.count);
    place.offset ??= Math.random();
    const traffic = place.down + place.up;
    const atHome = !home || (Math.abs(place.lat - home.lat) < 1.2 && Math.abs(place.lon - home.lng) < 1.2);
    const incoming = place.down >= place.up; // dashes travel with the data: download flows home
    place.flow = atHome ? null : { traffic, incoming, level: traffic > 0 ? Math.sqrt(traffic / busiest) : 0,
                                   rgb: incoming ? NEON.down : NEON.up };
  }
  const newcomers = map.seen.size ? [...keep.keys()].filter((k) => !map.seen.has(k)) : [];
  map.byKey = keep;
  for (const key of keep.keys()) map.seen.add(key);
  const now = Date.now();
  map.rings = map.rings.filter((r) => r.until > now);
  for (const key of newcomers) {
    const p = keep.get(key);
    map.rings.push({ lat: p.lat, lng: p.lon, rgb: NEON.down, size: 2.5, period: 900, until: now + 3500 });
  }
}

function updateGlobe() {
  const globe = map.globe;
  if (!globe || mapView() !== "3d") return;
  const home = homePoint();
  if (map.home !== conn.data?.home) globe.hexPolygonColor(homeDots(map.home = conn.data?.home)); // home changed
  const arcs = [];
  for (const place of map.byKey.values()) {
    const f = place.flow;
    if (!f) continue;
    const dim = map.selected && place.key !== map.selected;
    const arc = place.arc || (place.arc = { offset: place.offset });
    arc.place = place;
    arc.from = f.incoming ? { lat: place.lat, lng: place.lon } : home;
    arc.to = f.incoming ? home : { lat: place.lat, lng: place.lon };
    const strong = dim ? 0.12 : f.traffic > 0 ? 0.55 + 0.45 * f.level : 0.3;
    arc.color = [rgba(f.rgb, strong * 0.15), rgba(f.rgb, strong)];
    arc.stroke = (f.traffic > 0 ? 0.22 + 0.9 * f.level : 0.12) + (place.key === map.selected ? 0.35 : 0);
    arc.dash = f.traffic > 0 ? 0.35 : 1;
    arc.gap = f.traffic > 0 ? 0.2 : 0;
    arc.speed = f.traffic > 0 ? Math.round(3600 - 2600 * f.level) : 0;
    arcs.push(arc);
  }
  const rings = home ? [{ lat: home.lat, lng: home.lng, rgb: NEON.home, size: 3.5, period: 1600 }, ...map.rings] : map.rings;
  globe.arcsData(arcs).pointsData([...map.byKey.values()]).ringsData(rings);
}

function renderPlaceList() {
  const el = $("map-places");
  if (!map.places.length) {
    el.innerHTML = `<p class="empty">${conn.data?.geoip ? "No internet connections right now." : "Download the GeoIP database to place connections on the globe."}</p>`;
    return;
  }
  el.innerHTML = map.places.map((p) => `<button class="dest${p.key === map.selected ? " active" : ""}" data-key="${p.key}">
      ${flag(p.cc) || '<span class="flag"></span>'}
      <span class="ellipsis"><span class="ellipsis">${esc(p.label)}</span><small>${p.count} conn. · ${esc([...p.apps].map((id) => conn.apps[id]?.name).filter(Boolean).slice(0, 2).join(", "))}</small></span>
      <span class="num">${p.capture ? `${p.down + p.up ? rate(p.down + p.up) : "idle"}<small>${size(p.data)}</small>` : ""}</span>
    </button>`).join("");
}

function renderPlaceDetail() {
  const el = $("map-place");
  const place = map.selected && (map.places.find((p) => p.key === map.selected));
  $("map-list").hidden = Boolean(place);
  el.hidden = !place;
  if (!place) return;
  const programs = [...place.apps].map((id) => ({ id, app: conn.apps[id] })).filter((a) => a.app);
  const rows = place.rows.slice().sort((a, b) => ((b.row.down || 0) + (b.row.up || 0)) - ((a.row.down || 0) + (a.row.up || 0)));
  el.innerHTML = `<header class="panel-head"><button class="back" id="map-back" title="All destinations">‹</button>
      <h2 class="ellipsis">${flag(place.cc)}${esc(place.city || place.country)}</h2></header>
    <p class="where">${esc([place.region, place.country].filter((v) => v && v !== place.city).join(", "))}</p>
    <div class="place-stats">
      <div><b>${place.count}</b><small>connections</small></div>
      ${place.capture ? `<div><b>${rate(place.down)}</b><small>download</small></div><div><b>${rate(place.up)}</b><small>upload</small></div>` : ""}
    </div>
    ${place.orgs.size ? `<p class="orgs">${esc([...place.orgs].join(" · "))}</p>` : ""}
    <h3>Programs</h3>
    <div class="place-apps">${programs.map(({ id, app }) => `<span>${appIcon(id, app)}${esc(app.name)}</span>`).join("")}</div>
    <h3>Connections</h3>
    <div class="place-conns">${rows.slice(0, 40).map(({ row: r }) => {
      const p = conn.places[r.remote] || {};
      return `<div><span class="ellipsis" title="${esc(p.host || r.remote)}">${esc(p.host || r.remote)}<small>${esc(appOf(r).name)} · ${r.proto} ${r.rport}</small></span>`
        + `<span class="num">${r.rx === undefined ? "" : r.down || r.up ? rate(r.down + r.up) : "idle"}</span></div>`;
    }).join("")}${rows.length > 40 ? `<p class="muted">+${rows.length - 40} more</p>` : ""}</div>
    <button class="button secondary" id="map-to-connections">Show in Connections</button>`;
}

function selectPlace(key) {
  map.selected = key;
  const place = map.byKey.get(key) || map.places.find((p) => p.key === key);
  if (place && mapView() === "3d" && map.globe) {
    pauseRotation();
    map.globe.pointOfView({ lat: place.lat, lng: place.lon, altitude: 1.7 }, 1200);
  } else if (place && mapView() === "2d") {
    flatFocus(place);
  }
  renderPlaceList();
  renderPlaceDetail();
  updateGlobe();
}

function fillMapPrograms() {
  const select = $("map-program");
  const apps = new Map();
  for (const entry of conn.rows.values()) {
    const r = entry.row, p = conn.places[r.remote];
    if (!entry.gone && p?.lat != null) apps.set(String(r.app), appOf(r).name);
  }
  if (map.program && !apps.has(map.program)) apps.set(map.program, conn.apps[map.program]?.name || "Program");
  const options = [["", "All programs"], ...[...apps].sort((a, b) => a[1].localeCompare(b[1]))];
  const key = JSON.stringify(options);
  if (select.dataset.key !== key) {
    select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
    select.dataset.key = key;
  }
  if (document.activeElement !== select) select.value = map.program;
}

function showMap() {
  const three = mapView() === "3d";
  $("globe").hidden = !three;
  $("flatmap").hidden = three;
  $("map-rotate").hidden = !three;
  $("map-message").hidden = !three || !map.failed;
  markSegment("map-view", mapView());
  if (three) {
    flatStop();
    initGlobe();
    map.globe?.resumeAnimation();
  } else {
    map.globe?.pauseAnimation();
    flatStart();
  }
  if (conn.data) renderMap();
}

function hideMap() { // the map draws only while it's seen
  map.globe?.pauseAnimation();
  flatStop();
}

onSegment("map-view", (value) => {
  state.settings.map_view = value;
  send({ type: "settings", map_view: value });
  showMap();
});

$("map-places").addEventListener("click", (e) => {
  const row = e.target.closest(".dest");
  if (row) selectPlace(row.dataset.key);
});

$("map-place").addEventListener("click", (e) => {
  if (e.target.closest("#map-back")) {
    map.selected = "";
    renderPlaceList();
    renderPlaceDetail();
    updateGlobe();
  } else if (e.target.closest("#map-to-connections")) {
    const place = map.places.find((p) => p.key === map.selected);
    if (!place) return;
    conn.place = { key: place.key, label: place.label };
    conn.country = "";
    location.hash = "connections";
  }
});

$("map-program").addEventListener("change", (e) => {
  map.program = e.target.value;
  map.selected = "";
  renderMap();
});

$("map-rotate").addEventListener("click", () => {
  map.rotate = !map.rotate;
  $("map-rotate").setAttribute("aria-pressed", String(map.rotate));
  $("map-rotate").classList.toggle("on", map.rotate);
  clearTimeout(map.resumeTimer);
  if (map.globe) map.globe.controls().autoRotate = map.rotate;
});

$("map-home").addEventListener("click", () => {
  if (mapView() === "2d") return flatHome();
  const home = homePoint();
  if (!map.globe || !home) return;
  pauseRotation();
  map.globe.pointOfView({ lat: home.lat - 8, lng: home.lng, altitude: 2.3 }, 1200);
});

// ---------- map page: flat (2D) view ----------

// The Natural Earth projection (Šavrič et al.): the whole world at once, with shapes kept fairly true.
function projectNE(lon, lat) {
  const l = (lon * Math.PI) / 180, p = (lat * Math.PI) / 180, p2 = p * p, p4 = p2 * p2;
  return [l * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4))),
    p * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))];
}
const FLAT_X = projectNE(180, 0)[0];        // half the map's width (at the equator)
const FLAT_TOP = projectNE(0, 84)[1];       // northern edge
const FLAT_BOTTOM = projectNE(0, -57)[1];   // southern edge: Antarctica is left out
const FLAT_DOTS = 260;                      // land dots across the world at the first zoom level
const FLAT_MAX_ZOOM = 8;

const flat = {
  canvas: $("flatmap"),
  ctx: $("flatmap").getContext("2d"),
  layer: document.createElement("canvas"), // the land dots, redrawn only when the view moves
  dirty: true,
  w: 0, h: 0, dpr: 1,
  k: 1, x: 0, y: 0,                        // zoom and pan (pixels)
  goal: null,                              // {k, x, y} the view glides to
  dots: new Map(),                         // "level|home" -> {step, xy: Float32Array, home: Uint8Array}
  raf: 0, last: 0,
  drag: null,
  hover: null,
};

const polygonsOf = (f) => (f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates);

// Land dots for a zoom level: the countries are painted onto a small hidden canvas, one pixel per dot,
// and the painted pixels become dots (the home country's in green).
function flatDots(level) {
  const home = conn.data?.home || "";
  const key = `${level}|${home}`;
  if (flat.dots.has(key) || !map.countries) return flat.dots.get(key);
  const cols = FLAT_DOTS * 2 ** level, step = (2 * FLAT_X) / cols;
  const rows = Math.ceil((FLAT_TOP - FLAT_BOTTOM) / step);
  const mask = document.createElement("canvas");
  mask.width = cols;
  mask.height = rows;
  const m = mask.getContext("2d", { willReadFrequently: true });
  const paint = (features, color) => {
    m.fillStyle = color;
    m.beginPath();
    for (const f of features) {
      for (const polygon of polygonsOf(f)) {
        for (const ring of polygon) {
          ring.forEach(([lon, lat], i) => {
            const [x, y] = projectNE(lon, lat);
            const px = (x + FLAT_X) / step, py = (FLAT_TOP - y) / step;
            if (i) m.lineTo(px, py); else m.moveTo(px, py);
          });
          m.closePath();
        }
      }
    }
    m.fill("evenodd");
  };
  paint(map.countries.features, "#f00");
  paint(map.countries.features.filter((f) => home && f.properties.cc === home), "#0f0");
  const pixels = m.getImageData(0, 0, cols, rows).data;
  const xy = [], homes = [];
  for (let r = 0; r < rows; r++) {
    const shift = (r % 2) * 0.5; // every other row shifted half a step: a honeycomb of dots
    for (let c = 0; c < cols; c++) {
      const i = (r * cols + Math.min(cols - 1, Math.floor(c + shift))) * 4;
      if (pixels[i] < 110 && pixels[i + 1] < 110) continue; // sea
      xy.push(-FLAT_X + (c + 0.5 + shift) * step, FLAT_TOP - (r + 0.5) * step);
      homes.push(pixels[i + 1] > 110 ? 1 : 0);
    }
  }
  const dots = { step, xy: new Float32Array(xy), home: Uint8Array.from(homes) };
  flat.dots.set(key, dots);
  return dots;
}

// Map units <-> screen pixels.
function flatScale() {
  return Math.min(flat.w / (2 * FLAT_X), flat.h / (FLAT_TOP - FLAT_BOTTOM)) * 0.97 * flat.k;
}

function flatPoint(lon, lat) {
  const [x, y] = projectNE(lon, lat), s = flatScale();
  return [flat.w / 2 + flat.x + x * s, flat.h / 2 + flat.y - (y - (FLAT_TOP + FLAT_BOTTOM) / 2) * s];
}

function flatClamp(view) {
  const s = Math.min(flat.w / (2 * FLAT_X), flat.h / (FLAT_TOP - FLAT_BOTTOM)) * 0.97 * view.k;
  const spareX = Math.max(0, (2 * FLAT_X * s - flat.w) / 2), spareY = Math.max(0, ((FLAT_TOP - FLAT_BOTTOM) * s - flat.h) / 2);
  view.x = Math.max(-spareX - 40, Math.min(spareX + 40, view.x));
  view.y = Math.max(-spareY - 40, Math.min(spareY + 40, view.y));
  return view;
}

function flatResize() {
  const box = flat.canvas.getBoundingClientRect();
  if (!box.width || !box.height) return;
  flat.dpr = window.devicePixelRatio || 1;
  flat.w = box.width;
  flat.h = box.height;
  for (const c of [flat.canvas, flat.layer]) {
    c.width = Math.round(box.width * flat.dpr);
    c.height = Math.round(box.height * flat.dpr);
  }
  flat.dirty = true;
}

function flatDrawLayer() {
  const ctx = flat.layer.getContext("2d");
  ctx.setTransform(flat.dpr, 0, 0, flat.dpr, 0, 0);
  ctx.clearRect(0, 0, flat.w, flat.h);
  const level = flat.k >= 4.5 ? 2 : flat.k >= 2.2 ? 1 : 0;
  const dots = flatDots(level);
  if (!dots) return;
  const s = flatScale(), cx = flat.w / 2 + flat.x, cy = flat.h / 2 + flat.y, mid = (FLAT_TOP + FLAT_BOTTOM) / 2;
  const radius = Math.max(0.55, dots.step * s * 0.34);
  for (const home of [0, 1]) {
    ctx.fillStyle = home ? "rgba(70,190,140,0.9)" : "rgba(120,150,200,0.42)";
    ctx.beginPath();
    for (let i = 0; i < dots.home.length; i++) {
      if (dots.home[i] !== home) continue;
      const x = cx + dots.xy[2 * i] * s, y = cy - (dots.xy[2 * i + 1] - mid) * s;
      if (x < -4 || y < -4 || x > flat.w + 4 || y > flat.h + 4) continue;
      ctx.moveTo(x + radius, y);
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
    }
    ctx.fill();
  }
  flat.dirty = false;
}

// An arc between two screen points, bowed upwards like a flight path.
function flatCurve(a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], dist = Math.hypot(dx, dy) || 1;
  let nx = -dy / dist, ny = dx / dist;
  if (ny > 0) { nx = -nx; ny = -ny; }
  const lift = Math.min(dist * 0.28, 220);
  return [a, [(a[0] + b[0]) / 2 + nx * lift, (a[1] + b[1]) / 2 + ny * lift], b];
}

function flatOnCurve([a, c, b], t) {
  const u = 1 - t;
  return [u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]];
}

function flatFrame(now) {
  flat.raf = requestAnimationFrame(flatFrame);
  if (now - flat.last < 33) return; // about 30 frames a second is plenty
  flat.last = now;
  if (flat.goal) { // glide towards a new view
    const g = flat.goal, ease = 0.22;
    flat.k += (g.k - flat.k) * ease;
    flat.x += (g.x - flat.x) * ease;
    flat.y += (g.y - flat.y) * ease;
    if (Math.abs(g.k - flat.k) < 0.002 && Math.abs(g.x - flat.x) < 0.5 && Math.abs(g.y - flat.y) < 0.5) {
      Object.assign(flat, { k: g.k, x: g.x, y: g.y });
      flat.goal = null;
    }
    flat.dirty = true;
  }
  if (!flat.w) flatResize();
  if (!flat.w) return;
  if (flat.dirty) flatDrawLayer();
  const ctx = flat.ctx;
  ctx.setTransform(flat.dpr, 0, 0, flat.dpr, 0, 0);
  ctx.clearRect(0, 0, flat.w, flat.h);
  ctx.drawImage(flat.layer, 0, 0, flat.w, flat.h);
  const home = homePoint();
  const homeXY = home && flatPoint(home.lng, home.lat);
  const seconds = now / 1000;

  // arcs: brighter, thicker and moving = traffic now; dashes travel with the data
  for (const place of map.byKey.values()) {
    const f = place.flow;
    if (!homeXY || !f) continue;
    const there = flatPoint(place.lon, place.lat);
    const curve = f.incoming ? flatCurve(there, homeXY) : flatCurve(homeXY, there);
    place.curve = curve;
    const dim = map.selected && place.key !== map.selected;
    const strong = dim ? 0.12 : f.traffic > 0 ? 0.55 + 0.45 * f.level : 0.3;
    const width = (f.traffic > 0 ? 1.1 + 2.6 * f.level : 0.9) + (place.key === map.selected ? 1.2 : 0);
    const [a, c, b] = curve;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.quadraticCurveTo(c[0], c[1], b[0], b[1]);
    ctx.setLineDash([]);
    ctx.lineWidth = width * 3.2;
    ctx.strokeStyle = rgba(f.rgb, strong * 0.16); // glow
    ctx.stroke();
    const gradient = ctx.createLinearGradient(a[0], a[1], b[0], b[1]);
    gradient.addColorStop(0, rgba(f.rgb, strong * 0.25));
    gradient.addColorStop(1, rgba(f.rgb, strong));
    ctx.strokeStyle = gradient;
    ctx.lineWidth = width;
    if (f.traffic > 0) {
      ctx.setLineDash([12, 8]);
      ctx.lineDashOffset = -seconds * (30 + 150 * f.level) - place.offset * 20;
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // destinations, with names for the busiest
  const labels = [];
  const ranked = [...map.byKey.values()].sort((a, b) => (b.down + b.up) - (a.down + a.up) || b.count - a.count);
  for (const place of ranked) {
    const [x, y] = flatPoint(place.lon, place.lat);
    const r = 2.2 + 1.1 * Math.sqrt(place.count);
    place.screen = [x, y, r];
    const dim = map.selected && place.key !== map.selected;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.fillStyle = dim ? "rgba(220,235,255,0.35)" : "rgba(225,240,255,0.95)";
    ctx.fill();
    if (place.key === map.selected || place === flat.hover) {
      ctx.beginPath();
      ctx.arc(x, y, r + 4, 0, 2 * Math.PI);
      ctx.strokeStyle = "rgba(225,240,255,0.8)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    if (labels.length < 8 && !dim) labels.push([place, x, y, r]);
  }
  ctx.font = "11px 'Segoe UI Variable Text', 'Segoe UI', sans-serif";
  ctx.textBaseline = "middle";
  const taken = [];
  for (const [place, x, y, r] of labels) {
    const text = place.city || place.country;
    const width = ctx.measureText(text).width, box = [x + r + 5, y - 7, x + r + 7 + width, y + 7];
    if (box[2] > flat.w || taken.some((t) => box[0] < t[2] && box[2] > t[0] && box[1] < t[3] && box[3] > t[1])) continue;
    taken.push(box);
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(8,11,17,0.85)";
    ctx.strokeText(text, box[0], y);
    ctx.fillStyle = "rgba(200,215,235,0.9)";
    ctx.fillText(text, box[0], y);
  }

  // home, and a ripple for each new destination
  if (homeXY) {
    const phase = (now % 1600) / 1600;
    ctx.beginPath();
    ctx.arc(homeXY[0], homeXY[1], 4 + 16 * phase, 0, 2 * Math.PI);
    ctx.strokeStyle = rgba(NEON.home, 0.9 * (1 - phase));
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(homeXY[0], homeXY[1], 4, 0, 2 * Math.PI);
    ctx.fillStyle = rgba(NEON.home, 1);
    ctx.fill();
  }
  const clock = Date.now();
  for (const ring of map.rings) {
    const age = 1 - (ring.until - clock) / 3500;
    if (age < 0 || age > 1) continue;
    const [x, y] = flatPoint(ring.lng, ring.lat);
    ctx.beginPath();
    ctx.arc(x, y, 4 + 22 * age, 0, 2 * Math.PI);
    ctx.strokeStyle = rgba(NEON.down, 0.9 * (1 - age));
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }
}

function flatHit(mx, my) {
  let best = null, bestDist = Infinity;
  for (const place of map.byKey.values()) { // a dot first...
    if (!place.screen) continue;
    const [x, y, r] = place.screen, d = Math.hypot(mx - x, my - y);
    if (d <= r + 5 && d < bestDist) { best = place; bestDist = d; }
  }
  if (best) return best;
  for (const place of map.byKey.values()) { // ... then an arc
    if (!place.curve) continue;
    for (let t = 0; t <= 1; t += 1 / 30) {
      const [x, y] = flatOnCurve(place.curve, t);
      const d = Math.hypot(mx - x, my - y);
      if (d < 6 && d < bestDist) { best = place; bestDist = d; }
    }
  }
  return best;
}

function flatTip(place, mx, my) {
  const tip = $("map-tip");
  if (!place) { tip.hidden = true; return; }
  tip.innerHTML = placeTooltip(place);
  tip.hidden = false;
  const x = mx + 16 + tip.offsetWidth > flat.w ? mx - tip.offsetWidth - 16 : mx + 16;
  tip.style.left = `${x}px`;
  tip.style.top = `${Math.max(4, Math.min(my - tip.offsetHeight / 2, flat.h - tip.offsetHeight - 4))}px`;
}

function flatStart() {
  loadCountries().then(() => { flat.dirty = true; });
  if (!flat.raf) flat.raf = requestAnimationFrame(flatFrame);
}

function flatStop() {
  cancelAnimationFrame(flat.raf);
  flat.raf = 0;
  $("map-tip").hidden = true;
}

function flatHome() {
  flat.goal = { k: 1, x: 0, y: 0 };
}

function flatFocus(place) {
  if (flat.k < 1.5) return; // the whole world is in view anyway
  const [x, y] = flatPoint(place.lon, place.lat);
  flat.goal = flatClamp({ k: flat.k, x: flat.x - (x - flat.w / 2), y: flat.y - (y - flat.h / 2) });
}

new ResizeObserver(flatResize).observe(flat.canvas);

flat.canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const box = flat.canvas.getBoundingClientRect(), mx = e.clientX - box.left, my = e.clientY - box.top;
  const base = flat.goal || flat;
  const k = Math.max(1, Math.min(FLAT_MAX_ZOOM, base.k * 1.25 ** (-e.deltaY / 100)));
  const f = k / base.k; // keep the point under the mouse where it is
  const x = mx - flat.w / 2 - (mx - flat.w / 2 - base.x) * f;
  const y = my - flat.h / 2 - (my - flat.h / 2 - base.y) * f;
  flat.goal = flatClamp({ k, x, y });
  flatTip(null);
}, { passive: false });

flat.canvas.addEventListener("mousedown", (e) => {
  flat.goal = null;
  flat.drag = { x: e.clientX, y: e.clientY, startX: flat.x, startY: flat.y, moved: false };
});

window.addEventListener("mousemove", (e) => {
  const box = flat.canvas.getBoundingClientRect(), mx = e.clientX - box.left, my = e.clientY - box.top;
  if (flat.drag) {
    const dx = e.clientX - flat.drag.x, dy = e.clientY - flat.drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) flat.drag.moved = true;
    if (flat.drag.moved) {
      Object.assign(flat, flatClamp({ k: flat.k, x: flat.drag.startX + dx, y: flat.drag.startY + dy }));
      flat.dirty = true;
      flatTip(null);
    }
    return;
  }
  if (e.target !== flat.canvas) return;
  flat.hover = flatHit(mx, my);
  flat.canvas.style.cursor = flat.hover ? "pointer" : "grab";
  flatTip(flat.hover, mx, my);
});

window.addEventListener("mouseup", (e) => {
  const drag = flat.drag;
  flat.drag = null;
  if (!drag || drag.moved || e.target !== flat.canvas) return;
  const box = flat.canvas.getBoundingClientRect();
  const place = flatHit(e.clientX - box.left, e.clientY - box.top);
  if (place) selectPlace(place.key);
});

flat.canvas.addEventListener("mouseleave", () => { flat.hover = null; flatTip(null); });
flat.canvas.addEventListener("dblclick", flatHome);


// ---------- general settings, export ----------

function renderSystem() {
  const s = state.system;
  if (!s) return;
  if (document.activeElement !== $("autostart")) $("autostart").checked = s.autostart;
  $("data-dir").textContent = s.data_dir;
  $("db-size").textContent = `This PC's usage and per-program history (the database is ${size(s.db_size)}). `
    + "The router's data and your settings stay.";
}

$("autostart").addEventListener("change", (e) => send({ type: "autostart", on: e.target.checked }));

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) send({ type: "open_folder", what: open.dataset.open });
  const button = e.target.closest("[data-export]");
  if (!button) return;
  const query = {
    usage: `usage.csv?range=${state.usageRange}&`,
    programs: `programs.csv?period=${state.usageProgramsPeriod}&`,
    router: `router.csv?range=${state.routerRange}&`,
    connections: "connections.csv?",
    all: "netmon-data.zip?",
  }[button.dataset.export];
  const link = document.createElement("a"); // the window asks where to save it
  link.href = `/api/export/${query}token=${encodeURIComponent(TOKEN)}`;
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
});

$("delete-history").addEventListener("click", () => {
  if (!confirm("Delete this PC's usage history, including per-program usage?\n\n"
    + "The router's data and your settings stay. This can't be undone – export the data first if you want to keep it.")) return;
  send({ type: "delete_history" });
});

// ---------- quota settings ----------

function fillQuotaForm(s) {
  const form = $("quota-form");
  if (form.contains(document.activeElement)) return; // don't overwrite what's being typed
  $("quota-period").value = s.quota_period;
  $("quota-days").value = s.quota_days;
  $("quota-start").value = s.quota_start || "";
  $("quota-count").value = s.quota_count;
  const inMB = s.quota_limit_gb < 1;
  $("quota-unit").value = inMB ? "MB" : "GB";
  $("quota-limit").value = inMB ? Math.round(s.quota_limit_gb * 1024) : +s.quota_limit_gb.toFixed(3);
  $("quota-source").value = s.router_enabled ? s.quota_source : "pc";
  $("quota-domestic").value = s.quota_domestic_percent;
  form.classList.toggle("days", s.quota_period === "days");
  form.classList.toggle("router", Boolean(s.router_enabled));
  form.classList.toggle("domestic", domesticKnown(s, $("quota-source").value));
  quotaHint($("quota-source").value);
}

// The domestic share is known from the router's counters, or for this PC from the packet capture.
const domesticKnown = (s, source) => (source === "router" ? Boolean(s.router_enabled) : Boolean(s.capture_enabled));

$("quota-period").addEventListener("change", (e) => {
  $("quota-form").classList.toggle("days", e.target.value === "days");
  if (e.target.value === "days" && !$("quota-start").value && state.usage) $("quota-start").value = state.usage.summary.today;
});

$("quota-source").addEventListener("change", (e) => {
  $("quota-form").classList.toggle("domestic", domesticKnown(state.settings, e.target.value));
  quotaHint(e.target.value);
});

function quotaHint(source) {
  const s = state.settings;
  $("quota-hint").textContent = source === "router"
    ? "Counts everything that goes through the router's internet connection – all devices, also while this PC is off. 1 GB = 1024 MB."
    : "Counts this PC's traffic on its physical adapters – other devices sharing your internet package are not included, "
      + "so your ISP may show more."
      + (s.capture_enabled && s.usage_exclude_lan ? " Local network traffic isn't counted." : "")
      + (s.capture_enabled ? " Domestic traffic is measured by the packet capture." : "") + " 1 GB = 1024 MB.";
}

$("quota-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const note = (text, error = false) => { $("quota-note").textContent = text; $("quota-note").classList.toggle("error", error); };
  const period = $("quota-period").value;
  const limit = Number($("quota-limit").value);
  const days = Number($("quota-days").value);
  const start = $("quota-start").value.trim();
  if (!(limit > 0)) return note("Enter a limit larger than 0.", true);
  const change = { type: "settings", quota_period: period, quota_count: $("quota-count").value,
    quota_limit_gb: $("quota-unit").value === "MB" ? limit / 1024 : limit };
  if (state.settings.router_enabled) change.quota_source = $("quota-source").value;
  if (domesticKnown(state.settings, change.quota_source || "pc")) {
    const domestic = Number($("quota-domestic").value);
    if (!Number.isInteger(domestic) || domestic < 0 || domestic > 100) return note("Domestic traffic: 0 to 100 %.", true);
    change.quota_domestic_percent = domestic;
  }
  if (period === "days") {
    if (!Number.isInteger(days) || days < 1 || days > 366) return note("Length must be 1 to 366 days.", true);
    const [, , m, d] = start.match(/^(1[3-4]\d\d)\/(\d{1,2})\/(\d{1,2})$/) || [];
    if (!m || m < 1 || m > 12 || d < 1 || d > (m <= 6 ? 31 : 30)) {
      return note("Start date: a Jalali year/month/day, like 1405/07/01.", true);
    }
    Object.assign(change, { quota_days: days, quota_start: start });
    state.quotaStartSent = start; // checked when the server answers (e.g. 30 Esfand in a common year)
  }
  send(change);
  note("Saved.");
  document.activeElement.blur();
});

// ---------- ping page ----------

const PING_COLORS = { internet: "#3987e5", gateway: "#199e70", loss: "#f85149" }; // = --inet, --gw, --bad
const PING_KINDS = [["internet", "Internet", "inet"], ["gateway", "Gateway", "gw"]];
const PING_RANGES = { "1h": "the last hour", "24h": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days" };
const PING_POINTS = { 1: "each ping", 60: "per minute", 600: "per 10 minutes", 3600: "per hour" };

const pingHist = {
  data: null,      // the latest report: {range, start, end, step, series, stats, outages, days...}
  chart: null,
  drawTimer: 0,
};

// Round trips: "<1" under a millisecond (the gateway), one decimal below 10, whole numbers above.
const msText = (v) => (v == null ? "–" : v < 1 ? "&lt;1" : v < 10 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v)));
const msUnit = (v) => (v == null ? "–" : `${msText(v)}<span class="unit">ms</span>`);
const lossText = (pct) => (pct == null ? "–" : pct === 0 ? "0" : pct < 0.1 ? "&lt;0.1" : pct < 10 ? pct.toFixed(1) : String(Math.round(pct)));

function pingDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec / 3600) % 24, m = Math.floor(sec / 60) % 60, s = sec % 60;
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} m`;
  if (m) return `${m} m ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

function pingQuality(kind, v) {
  if (v == null) return "bad";
  const [good, fair] = kind === "gateway" ? [5, 20] : [60, 120];
  return v < good ? "good" : v < fair ? "fair" : "bad";
}

function dayOf(t) { // the report's local day holding t: [midnight, "21 Mehr", "Monday 21 Mehr 1405"]
  const days = pingHist.data?.days || [];
  let found = null;
  for (const day of days) if (day[0] <= t) found = day;
  return found;
}

function pingCard(kind, label, cls) {
  const live = kind === "internet" ? state.ping : state.pingGateway;
  const d = pingHist.data;
  const st = d?.stats?.[kind];
  const host = live?.target || d?.hosts?.[kind] || "";
  const auto = kind === "gateway" && live?.auto && host ? " · automatic" : "";
  let now;
  if (kind === "gateway" && !state.settings.ping_record) now = '<span class="problem">Only pinged while recording</span>';
  else if (!live || live.target !== host || (!live.count && !live.problem)) now = '<span class="problem">Waiting…</span>';
  else if (!live.count) now = `<span class="dot bad"></span><span class="problem">${esc(live.problem)}</span>`;
  else if (live.last == null) now = `<span class="dot bad"></span><span class="problem">${esc(live.problem || "timeout")}</span>`;
  else now = `<span class="dot ${pingQuality(kind, live.last)}"></span>${msText(live.last)}<span class="unit">ms</span>`;
  const stat = (name, value, sub = "") => `<div><small>${name}</small><b>${value}</b>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;
  const stats = !st || !st.sent ? `<p class="empty">Nothing recorded in ${PING_RANGES[d?.range] || "this period"}.</p>`
    : `<div class="ping-stats">
        ${stat("Average", msUnit(st.average), `best ${msText(st.best)} · worst ${msText(st.worst)}`)}
        ${stat(st.p95_exact ? "p95" : "p95 ≈", msUnit(st.p95), "95% of pings faster")}
        ${stat("Jitter", msUnit(st.jitter), "change between pings")}
        ${stat("Loss", `${lossText(st.loss)}<span class="unit">%</span>`, `${st.lost.toLocaleString("en")} of ${st.sent.toLocaleString("en")}`)}
        ${stat("Outages", String(st.outages), st.outages ? `${pingDuration(st.outage_time)} in all` : "none")}
      </div>`;
  return `<article class="card ping-card">
    <header><span class="swatch ${cls}"></span>${label}<span class="spacer"></span><span class="muted label">${esc(host)}${auto}</span></header>
    <div class="big">${now}</div>
    ${stats}
  </article>`;
}

function renderPingCards() {
  $("ping-cards").innerHTML = PING_KINDS.map(([kind, label, cls]) => pingCard(kind, label, cls)).join("");
}

function pingTimeLabel(t) {
  const d = pingHist.data, day = dayOf(t);
  const hm = (x) => clock(x, false);
  if (d.step === 1) return clock(t);
  const span = d.step === 60 ? hm(t) : `${hm(t)}–${hm(t + d.step)}`;
  return day ? `${day[2]} · ${span}` : span;
}

function pingTooltip() {
  let tip;
  return {
    hooks: {
      init(u) {
        tip = document.createElement("div");
        tip.className = "tooltip";
        tip.hidden = true;
        u.over.appendChild(tip);
      },
      setCursor(u) {
        const d = pingHist.data, i = u.cursor.idx;
        if (!d || i == null || u.cursor.left < 0) { tip.hidden = true; return; }
        const t = d.step === 1 ? d.start + i : d.start + i * d.step;
        let html = `<div class="time">${esc(pingTimeLabel(t))}</div>`;
        for (const [kind, label, cls] of PING_KINDS) {
          const s = d.series[kind];
          let value, detail = "";
          if (!s.sent[i]) value = "no pings";
          else if (d.step === 1) value = s.lost[i] ? "lost" : `${msText(s.avg[i])} ms`;
          else {
            value = s.avg[i] == null ? "all lost" : `${msText(s.avg[i])} ms`;
            detail = (s.avg[i] == null ? "" : `${msText(s.lo[i])}–${msText(s.hi[i])} ms (min–p95), max ${msText(s.max[i])} · `)
              + `${s.lost[i]} of ${s.sent[i]} lost`;
          }
          html += `<div class="row"><span class="swatch ${cls}"></span>${label}<b>${value}</b></div>`;
          if (detail) html += `<div class="detail">${detail}</div>`;
        }
        const out = d.outages.filter((o) => o.start < t + d.step && o.end > t);
        for (const o of out.slice(0, 3)) {
          html += `<div class="row outage"><span class="swatch loss"></span>${o.kind === "internet" ? "Internet" : "Gateway"} outage<b>${pingDuration(o.duration)}${o.ongoing ? " so far" : ""}</b></div>`;
        }
        tip.innerHTML = html;
        tip.hidden = false;
        const x = u.cursor.left, y = u.cursor.top, w = tip.offsetWidth, h = tip.offsetHeight;
        tip.style.left = `${x + 16 + w > u.over.clientWidth ? x - w - 16 : x + 16}px`;
        tip.style.top = `${Math.max(0, Math.min(y - h / 2, u.over.clientHeight - h))}px`;
      },
    },
  };
}

// Red shading under the lines where an outage was; red ticks along the top where pings were lost.
function drawOutages(u) {
  const d = pingHist.data;
  if (!d) return;
  const { ctx } = u, { top, height } = u.bbox;
  ctx.save();
  ctx.fillStyle = "rgba(248,81,73,0.16)";
  for (const o of d.outages) {
    const x0 = u.valToPos(Math.max(o.start, d.start), "x", true), x1 = u.valToPos(Math.min(o.end, d.end), "x", true);
    ctx.fillRect(x0, top, Math.max(2 * devicePixelRatio, x1 - x0), height);
  }
  ctx.restore();
}

function drawLoss(u) {
  const d = pingHist.data;
  if (!d) return;
  const { ctx } = u, { top } = u.bbox, dpr = devicePixelRatio;
  const width = Math.max(2 * dpr, u.valToPos(d.start + d.step, "x", true) - u.valToPos(d.start, "x", true));
  ctx.save();
  ctx.fillStyle = PING_COLORS.loss;
  const n = d.series.internet.sent.length;
  for (let i = 0; i < n; i++) {
    let lost = 0, sent = 0;
    for (const [kind] of PING_KINDS) { lost += d.series[kind].lost[i]; sent += d.series[kind].sent[i]; }
    if (!lost) continue;
    ctx.globalAlpha = Math.min(1, 0.12 + 25 * lost / sent); // one lost ping in an hour stays faint; 4% or more is solid
    const x = u.data[0][i];
    ctx.fillRect(u.valToPos(x, "x", true) - width / 2, top, width, 5 * dpr);
  }
  ctx.restore();
}

function pingSplits(u, axis, min, max) { // 7 and 30 days: one label per day, as many as fit
  const d = pingHist.data;
  const days = (d?.days || []).map((day) => day[0]).filter((t) => t >= min && t <= max);
  const per = u.bbox.width / devicePixelRatio / Math.max(1, (max - min) / 86400);
  const every = Math.max(1, Math.ceil(64 / per));
  return days.filter((_, i) => i % every === 0);
}

function makePingChart() {
  const el = $("ping-chart");
  pingHist.chart?.destroy();
  pingHist.chart = null;
  if (!el.clientWidth) return; // hidden page: made when it shows
  const axis = { stroke: COLORS.axis, grid: { stroke: COLORS.grid, width: 1 }, ticks: { show: false }, font: "11px Segoe UI" };
  const daily = pingHist.data && pingHist.data.step >= 600;
  const edge = { stroke: "transparent", width: 0, points: { show: false } }; // band edges (min, p95)
  const width = pingHist.data?.step === 1 ? 1.25 : 2; // a ping a second: thinner, or the hour is a solid smear
  const line = (kind, label) => ({ label, stroke: PING_COLORS[kind], width, points: { show: false } });
  pingHist.chart = new uPlot({
    width: el.clientWidth,
    height: el.clientHeight,
    padding: [12, 8, 0, 0],
    legend: { show: false },
    cursor: {
      y: false,
      drag: { x: false, y: false, setScale: false },
      points: { size: (u, i) => (i >= 5 ? 8 : 0), width: 2, fill: COLORS.bg, stroke: (u, i) => [, , , , , PING_COLORS.internet, PING_COLORS.gateway][i] },
    },
    scales: { x: { time: true, auto: false }, y: { auto: false } },
    axes: [
      daily
        ? { ...axis, space: 64, splits: pingSplits, values: (u, splits) => splits.map((t) => dayOf(t)?.[1] || "") }
        : { ...axis, space: 70, values: (u, splits) => splits.map((t) => clock(t, false)) },
      { ...axis, size: 62, space: 30, values: (u, splits) => splits.map((v) => `${v} ms`) },
    ],
    series: [{}, { ...edge }, { ...edge }, { ...edge }, { ...edge }, line("internet", "Internet"), line("gateway", "Gateway")],
    bands: [{ series: [1, 2], fill: "rgba(57,135,229,0.18)" }, { series: [3, 4], fill: "rgba(25,158,112,0.18)" }],
    hooks: { drawClear: [drawOutages], draw: [drawLoss] },
    plugins: [pingTooltip()],
  }, [[], [], [], [], [], [], []], el);
  pingHist.layout = `${daily}|${width}`;
  renderPingChart();
}

function renderPingChart() {
  const d = pingHist.data;
  if (!d || document.hidden || state.view !== "ping") return;
  if (!pingHist.chart || pingHist.layout !== `${d.step >= 600}|${d.step === 1 ? 1.25 : 2}`) return makePingChart();
  const n = d.series.internet.avg.length;
  const xs = new Array(n);
  for (let i = 0; i < n; i++) xs[i] = d.step === 1 ? d.start + i : d.start + i * d.step + d.step / 2;
  const s = (kind, key) => d.series[kind][key];
  // Scale to the 99th percentile, so one lone spike doesn't flatten everything else (it runs off the top).
  const values = [];
  for (const [kind] of PING_KINDS) for (const key of ["avg", "hi"]) for (const v of s(kind, key)) if (v != null) values.push(v);
  values.sort((a, b) => a - b);
  const p99 = values.length ? values[Math.min(values.length - 1, Math.floor(values.length * 0.99))] : 50;
  pingHist.chart.batch(() => {
    pingHist.chart.setData([xs, s("internet", "hi"), s("internet", "lo"), s("gateway", "hi"), s("gateway", "lo"),
      s("internet", "avg"), s("gateway", "avg")], false);
    pingHist.chart.setScale("x", { min: d.start, max: d.end });
    pingHist.chart.setScale("y", { min: 0, max: Math.max(10, p99 * 1.25) });
  });
}

function schedulePingChart() { // live pings arrive twice a second; draw at most every half second
  if (pingHist.drawTimer) return;
  pingHist.drawTimer = setTimeout(() => { pingHist.drawTimer = 0; renderPingChart(); }, 500);
}

new ResizeObserver(() => {
  const el = $("ping-chart");
  if (pingHist.chart && el.clientWidth) pingHist.chart.setSize({ width: el.clientWidth, height: el.clientHeight });
  else if (!pingHist.chart && el.clientWidth) renderPingChart();
}).observe($("ping-chart"));

function renderPingOutages() {
  const d = pingHist.data, el = $("ping-outages");
  if (!d) return;
  if (!d.outages.length) {
    el.innerHTML = `<p class="empty">No outages in ${PING_RANGES[d.range]}.</p>`;
    return;
  }
  const cause = (o) => (o.kind === "gateway" ? "Local network"
    : o.cause === "local" ? "Local network <span class=\"muted\">– the gateway was down too</span>"
      : o.cause === "internet" ? "Internet <span class=\"muted\">– the gateway answered</span>" : "–");
  const rows = d.outages.map((o) => `<tr class="${o.ongoing ? "ongoing" : ""}">
      <td>${esc(o.label)}</td>
      <td class="num">${o.ongoing ? "going on · " : ""}${pingDuration(o.duration)}</td>
      <td><span class="cause"><span class="swatch ${o.kind === "internet" ? "inet" : "gw"}"></span>${o.kind === "internet" ? "Internet" : "Gateway"}
        <span class="muted">${esc(o.host)}</span></span></td>
      <td>${cause(o)}</td>
      <td class="num">${o.lost.toLocaleString("en")}</td>
    </tr>`).join("");
  el.innerHTML = `<table class="table"><thead><tr><th>Started</th><th class="num">Lasted</th><th>Target</th>`
    + `<th>Where</th><th class="num">Lost pings</th></tr></thead><tbody>${rows}</tbody></table>`
    + (d.more_outages ? `<p class="hint">…and ${d.more_outages} older ones in this period.</p>` : "");
}

function renderPingHint() {
  const d = pingHist.data;
  if (!d) return;
  let text = `One ping a second to each target; the chart shows ${PING_POINTS[d.step]}`
    + (d.step > 1 ? " (line = average, band = fastest to 95th percentile)" : "") + ". Red: lost pings and outages.";
  if (d.first_label) text += ` Recording since ${d.first_label}.`;
  if (d.step > 1 && Object.values(d.stats).some((s) => s.sent && !s.p95_exact)) {
    text += " p95 ≈: single pings are kept for a week, so older periods use the per-minute values.";
  }
  $("ping-hint").textContent = text;
}

function renderPingPage() {
  $("ping-off").hidden = Boolean(state.settings.ping_record);
  markSegment("ping-range", state.pingRange);
  renderPingCards();
  renderPingChart();
  renderPingOutages();
  renderPingHint();
}

function requestPing() {
  send({ type: "ping_history", range: state.pingRange });
}

// A live ping (1-hour view): put it on the chart right away; the rest updates once a minute.
function addLivePing(msg) {
  const d = pingHist.data;
  if (!d || d.step !== 1 || msg.t == null || !d.series[msg.kind]) return;
  if (msg.t >= d.end) { // slide the hour along
    const shift = Math.min(msg.t - d.end + 1, d.end - d.start);
    for (const series of Object.values(d.series)) {
      for (const [key, list] of Object.entries(series)) {
        list.splice(0, shift);
        for (let k = 0; k < shift; k++) list.push(key === "sent" || key === "lost" ? 0 : null);
      }
    }
    d.start += shift;
    d.end += shift;
  }
  const i = msg.t - d.start;
  if (i < 0) return;
  const s = d.series[msg.kind];
  s.sent[i] = 1;
  s.lost[i] = msg.last == null ? 1 : 0;
  s.avg[i] = msg.last;
  schedulePingChart();
}

onSegment("ping-range", (value) => {
  state.pingRange = value;
  markSegment("ping-range", value);
  requestPing();
});

$("delete-ping").addEventListener("click", () => {
  if (!confirm("Delete the ping history – every recorded ping and outage?\n\nYour usage history stays. This can't be undone.")) return;
  send({ type: "delete_ping_history" });
});

// ---------- settings page ----------

const HOST = /^[A-Za-z0-9.\-:]{1,253}$/;

function renderPing() {
  const el = $("ping-readout");
  const p = state.ping;
  const target = (state.settings.ping_target || "").trim();
  const message = (html) => { el.innerHTML = `<div class="message">${html}</div>`; };
  if (!state.settings.ping_visible && !state.settings.ping_record) {
    return message("Not pinging: the ping widget and the ping history are both off.");
  }
  if (!p || p.target !== target || (!p.count && !p.problem)) return message(`Waiting for the first reply from ${esc(target)}…`);
  if (!p.count) return message(`<span class="dot bad"></span>${esc(target)}: ${esc(p.problem)}`);

  const ms = (v) => (v == null ? "–" : `${v < 1 ? "&lt;1" : Math.round(v)}<span class="unit">ms</span>`);
  const quality = p.last == null ? "bad" : p.last < 60 ? "good" : p.last < 120 ? "fair" : "bad";
  const stat = (label, value) => `<div class="stat"><small>${label}</small><b>${value}</b></div>`;
  el.innerHTML = [
    stat("Now", `<span class="dot ${quality}"></span>${p.last == null ? esc(p.problem || "timeout") : ms(p.last)}`),
    stat("Average", ms(p.average)),
    stat("Best", ms(p.best)),
    stat("Worst", ms(p.worst)),
    stat("Jitter", ms(p.jitter)),
    stat("Loss", `${Math.round(p.loss)}<span class="unit">%</span>`),
    p.address && p.address !== target ? stat("Address", esc(p.address)) : "",
  ].join("");
}

function pingNote(text, error = false) {
  const note = $("ping-note");
  note.textContent = text;
  note.classList.toggle("error", error);
}

$("ping-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("ping-target"), gateway = $("ping-gateway");
  const value = input.value.trim(), router = gateway.value.trim();
  if (!HOST.test(value)) return pingNote("Internet target: enter an IP address or a host name.", true);
  if (router && !HOST.test(router)) return pingNote("Gateway: enter an IP address, or leave it empty.", true);
  send({ type: "settings", ping_target: value, ping_gateway: router });
  pingNote(`Saved – now pinging ${value} and ${router || "the gateway found automatically"}`);
  input.blur();
  gateway.blur();
});

for (const box of document.querySelectorAll("[data-setting]")) {
  box.addEventListener("change", () => send({ type: "settings", [box.dataset.setting]: box.checked }));
}

function ipLookupNote(text, error = false) {
  const note = $("iplookup-note");
  note.textContent = text;
  note.classList.toggle("error", error);
}

function renderIpLookup(msg) {
  const el = $("iplookup-result");
  if (!msg) { el.hidden = true; return; }
  if (!msg.ok) {
    el.hidden = true;
    ipLookupNote(msg.message, true);
    return;
  }
  ipLookupNote("");
  const row = (label, value) => (value ? `<dt>${label}</dt><dd>${value}</dd>` : "");
  const where = [msg.region, msg.city].filter(Boolean).join(", ");
  el.hidden = false;
  el.innerHTML = `<h2>${flag(msg.cc)}${esc(msg.country || msg.cc || "Unknown")}</h2>
    <dl>
      ${row("Address", `<span class="mono">${esc(msg.address)}</span>`)}
      ${row("Location", esc(where))}
      ${row("Network", msg.org ? `${esc(msg.org)}<br><span class="muted">AS${msg.asn}</span>` : "")}
      ${row("Coordinates", msg.lat != null ? `${msg.lat.toFixed(2)}, ${msg.lon.toFixed(2)}` : "")}
    </dl>`;
}

$("iplookup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("iplookup-address");
  const value = input.value.trim();
  if (!value) return ipLookupNote("Enter an IP address.", true);
  $("iplookup-result").hidden = true;
  ipLookupNote("Looking up…");
  send({ type: "ip_lookup", address: value });
});

// ---------- pages ----------

const VIEWS = ["live", "usage", "ping", "router", "connections", "iplookup", "map", "settings"];

function showView() {
  const wanted = location.hash.slice(1);
  state.view = VIEWS.includes(wanted) ? wanted : "live";
  for (const el of document.querySelectorAll(".view")) el.hidden = el.id !== `view-${state.view}`;
  for (const tab of document.querySelectorAll(".tab[data-view]")) tab.classList.toggle("active", tab.dataset.view === state.view);
  if (state.view === "usage") requestUsage();
  if (state.view === "ping") { renderPingPage(); requestPing(); }
  if (state.view === "router") { renderRouter(); requestRouter(); }
  if (state.view === "connections") renderConnections();
  if (state.view === "iplookup") renderGeoipBanner("iplookup-geoip");
  if (state.view === "map") showMap(); else hideMap(); // the globe draws only while it's seen
  updateWatch();
  render();
}

function render() {
  if (document.hidden) return; // minimized: keep collecting, skip drawing
  renderAdapters(); // also fills the adapter picker and status bar, seen on every page
  if (state.view === "live") {
    renderCards();
    renderChart();
    renderPrograms();
  } else if (state.view === "settings") {
    renderPing();
    renderCaptureStatus();
  } else if (state.view === "ping") {
    renderPingCards(); // "now" changes every second; the chart follows the live pings itself
  }
  // The usage page changes once a minute; it is drawn when its data arrives.
}

function setStatus(live) {
  const el = $("status");
  el.className = `status ${live ? "live" : "lost"}`;
  el.lastElementChild.textContent = live ? "Live" : "Disconnected – is NetMon running? Retrying…";
}

function markSegment(id, value) {
  for (const b of $(id).querySelectorAll("button")) b.classList.toggle("active", b.dataset.value === String(value));
}

function onSegment(id, handler) {
  $(id).addEventListener("click", (e) => {
    const button = e.target.closest("button");
    if (button) handler(button.dataset.value);
  });
}

function applySettings(settings) {
  const unitsChanged = settings.units !== state.settings.units;
  state.settings = settings;
  markSegment("units", settings.units);
  for (const box of document.querySelectorAll("[data-setting]")) box.checked = Boolean(settings[box.dataset.setting]);
  const target = $("ping-target"), gateway = $("ping-gateway");
  if (document.activeElement !== target) target.value = settings.ping_target || "";
  if (document.activeElement !== gateway) gateway.value = settings.ping_gateway || "";
  $("ping-off").hidden = Boolean(settings.ping_record);
  if (state.quotaStartSent) {
    if (settings.quota_start !== state.quotaStartSent) {
      $("quota-note").textContent = `${state.quotaStartSent} isn't a date in the Jalali calendar.`;
      $("quota-note").classList.add("error");
    }
    state.quotaStartSent = null;
  }
  fillQuotaForm(settings);
  fillRouterForm(settings);
  fillHomeCountry(settings);
  if (state.view === "connections") renderConnections(); // filters may have changed
  if (state.view === "map" && $("flatmap").hidden !== (mapView() === "3d")) showMap(); // 2D / 3D switched
  if (unitsChanged) makeChart(); // axis steps differ between bits and bytes
}

// ---------- connection to NetMon ----------

function onMessage(msg) {
  if (msg.type === "hello") {
    state.started = msg.started;
    $("version").textContent = `v${msg.version}`;
    state.times = []; state.downs = []; state.ups = [];
    for (const [t, down, up] of msg.history) addPoint(t, down, up);
    if (msg.tick) state.tick = msg.tick;
    state.ping = msg.ping;
    state.pingGateway = msg.ping_gateway;
    state.usage = msg.usage;
    state.router = msg.router;
    state.geoip = msg.geoip;
    state.region = msg.region;
    state.capture = msg.capture;
    state.programs = msg.programs;
    state.system = msg.system;
    $("about-version").textContent = `v${msg.version}`;
    renderSystem();
    applySettings(msg.settings);
    renderGeoipSettings();
    conn.watching = false; // a new connection to NetMon: ask for the connections again
    updateWatch();
    if (state.view === "usage") requestUsage();
    if (state.view === "ping") requestPing();
    if (state.view === "router") { renderRouter(); requestRouter(); }
  } else if (msg.type === "tick") {
    addPoint(msg.t, msg.down, msg.up);
    state.tick = msg;
  } else if (msg.type === "settings") {
    applySettings(msg.settings);
  } else if (msg.type === "ping") {
    if (msg.kind === "gateway") state.pingGateway = msg; else state.ping = msg;
    addLivePing(msg);
    if (document.hidden) return;
    if (state.view === "settings") {
      renderPing();
      $("ping-gateway").placeholder = state.pingGateway?.auto && state.pingGateway.target
        ? `Automatic (${state.pingGateway.target})` : "Automatic";
    }
    if (state.view === "ping") renderPingCards();
    return;
  } else if (msg.type === "ping_saved") {
    if (state.view === "ping") requestPing(); // a minute was saved, or an outage began or ended
    return;
  } else if (msg.type === "ping_history") {
    if (msg.range !== state.pingRange) return; // an answer for a range no longer shown
    pingHist.data = msg;
    if (state.view === "ping") renderPingPage();
    return;
  } else if (msg.type === "navigate") {
    location.hash = msg.view; // the page switch happens in showView()
    return;
  } else if (msg.type === "usage") {
    usageBars.data = msg;
    if (state.view === "usage" && msg.range === state.usageRange) renderUsage();
    return;
  } else if (msg.type === "usage_summary") {
    state.usage = msg;
    if (state.view === "usage") requestUsage(); // new minute saved: refresh the bars too
    if (state.view === "router") renderRouter(); // the router page may show the quota
    return;
  } else if (msg.type === "router_summary") {
    state.router = msg;
    if (state.view === "router") { renderRouter(); requestRouter(); }
    return;
  } else if (msg.type === "router_usage") {
    routerBars.data = msg;
    if (state.view === "router" && msg.range === state.routerRange) renderRouter();
    return;
  } else if (msg.type === "system") {
    state.system = msg;
    renderSystem();
    return;
  } else if (msg.type === "programs") {
    state.programs = msg;
    if (state.view === "live" && !document.hidden) renderPrograms();
    return;
  } else if (msg.type === "capture") {
    state.capture = msg;
    renderCaptureStatus();
    if (state.view === "live") renderPrograms();
    if (state.view === "usage") renderUsageHint();
    return;
  } else if (msg.type === "usage_programs") {
    state.usagePrograms = msg;
    if (state.view === "usage") renderUsagePrograms();
    return;
  } else if (msg.type === "connections") {
    onConnections(msg);
    return;
  } else if (msg.type === "geoip") {
    state.geoip = msg;
    renderGeoip();
    return;
  } else if (msg.type === "ip_lookup") {
    renderIpLookup(msg);
    return;
  } else if (msg.type === "router_test") {
    const direct = state.settings.router_mode === "direct";
    routerNote(direct ? "direct-note" : "router-note", msg.message, msg.ok ? "ok" : "error");
    if (direct && msg.ok && state.directTest) {  // it works: read this router from now on
      send({ type: "settings", router_mode: "direct", router_host: state.directTest.host, router_wan: state.directTest.wan });
    }
    return;
  } else if (msg.type === "router_script" && msg.for === "direct") {
    $("direct-output").hidden = !msg.ok;
    $("direct-copy").disabled = !msg.ok;
    if (msg.ok) $("direct-output").value = msg.script;
    routerNote("direct-note", msg.ok ? "Ready – copy it into the router's terminal, then press Test." : msg.message,
               msg.ok ? "ok" : "error");
    return;
  } else if (msg.type === "router_script") {
    $("script-output").hidden = !msg.ok;
    $("script-copy").disabled = !msg.ok;
    if (msg.ok) $("script-output").value = msg.script;
    routerNote("script-note", msg.ok ? "Ready – copy it, or check it first." : msg.message, msg.ok ? "ok" : "error");
    return;
  }
  render();
}

function send(message) {
  if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(message));
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  state.ws = ws;
  ws.onopen = () => setStatus(true);
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => { setStatus(false); conn.watching = false; setTimeout(connect, 2000); };
}

onSegment("units", (value) => send({ type: "settings", units: value }));
onSegment("range", (value) => { state.range = Number(value); markSegment("range", value); render(); });
$("adapter").addEventListener("change", (e) => send({ type: "settings", adapter: e.target.value || null }));
document.addEventListener("visibilitychange", () => {
  updateWatch(); // minimized: NetMon stops reading the connections
  if (document.hidden) hideMap(); else if (state.view === "map") showMap();
  render();
});
window.addEventListener("hashchange", showView);
// The page itself never scrolls - each view scrolls inside. If anything (focusing a hidden
// control, say) scrolls it anyway, everything would slide out of the window: put it back.
window.addEventListener("scroll", () => { if (scrollX || scrollY) scrollTo(0, 0); });

markSegment("range", state.range);
markSegment("units", state.settings.units);
markSegment("usage-range", state.usageRange);
markSegment("usage-mode", usageBars.mode);
markSegment("usage-split", state.usageSplit);
markSegment("usage-programs-period", state.usageProgramsPeriod);
markSegment("ping-range", state.pingRange);
renderUsageLegend();
$("map-rotate").classList.toggle("on", map.rotate);
markSegment("router-range", state.routerRange);
markSegment("router-split", "direction");
markSegment("router-mode", routerBars.mode);
renderRouterLegend();
makeChart();
showView();
connect();

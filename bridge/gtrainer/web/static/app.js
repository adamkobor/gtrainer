"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
};

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

const S = {
  meta: null,
  status: null,
  prevButtons: new Set(),
  learn: null, // PS button waiting for a Ride button press
  sim: { pressed: new Set(), latched: new Set(), paddle_l: 0, paddle_r: 0 },
  edit: { name: null, doc: null, parent: null, yaml: "", formDirty: false, yamlDirty: false, tab: "form" },
};

const METERS = [
  { key: "power_w", label: "Power", max: 600, unit: "W" },
  { key: "power_ftp", label: "% of FTP", max: 1.5, unit: "%", pct: true },
  { key: "power_gear", label: "% of gear", max: 1.2, unit: "%", pct: true },
  { key: "cadence_rpm", label: "Cadence", max: 130, unit: "rpm" },
  { key: "speed_kph", label: "Speed", max: 60, unit: "km/h" },
  { key: "paddle_l", label: "Paddle L", max: 1, unit: "", sim: true, bipolar: true },
  { key: "paddle_r", label: "Paddle R", max: 1, unit: "", sim: true, bipolar: true },
];

// ---------------------------------------------------------------- live view
function buildLive() {
  const meters = $("#meters");
  for (const m of METERS) {
    const row = el("div", { class: "meter", id: `m-${m.key}` },
      el("span", {}, m.label),
      el("div", { class: m.bipolar ? "bar bipolar" : "bar" }, el("div")),
      el("span", { class: "val" }, "–"));
    if (m.sim) {
      const slider = el("input", { type: "range", min: -1, max: 1, step: 0.01, value: 0, class: "hidden", title: "Release to snap back to center" });
      slider.addEventListener("input", () => { S.sim[m.key] = Number(slider.value); postSim(); });
      slider.addEventListener("change", () => { slider.value = 0; S.sim[m.key] = 0; postSim(); });
      row.querySelector(".bar").after(slider);
    }
    meters.append(row);
  }

  const ride = $("#ride-buttons");
  for (const b of S.meta.ride_buttons) {
    const key = el("div", { class: "key", id: `rb-${b}`, title: "Simulation: click/hold, shift-click to latch" }, b);
    key.addEventListener("pointerdown", (e) => {
      if (!S.status?.sim) return;
      if (e.shiftKey) {
        S.sim.latched.has(b) ? S.sim.latched.delete(b) : S.sim.latched.add(b);
      } else {
        S.sim.pressed.add(b);
        key.setPointerCapture(e.pointerId);
      }
      postSim();
    });
    const release = () => { if (S.sim.pressed.delete(b)) postSim(); };
    key.addEventListener("pointerup", release);
    key.addEventListener("pointercancel", release);
    ride.append(key);
  }

  const simGears = $("#sim-gears");
  for (const g of [null, 1, 2, 3, 4, 5, 6]) {
    const b = el("button", { type: "button", "data-gear": g ?? "" }, g ?? "none");
    b.addEventListener("click", () => api("POST", "/api/sim/gear", { gear: g }).catch(() => {}));
    simGears.append(b);
  }

  const out = $("#out-buttons");
  for (const b of S.meta.ps_buttons) out.append(el("div", { class: "key", id: `pb-${b}` }, b));
}

function postSim() {
  const buttons = [...new Set([...S.sim.pressed, ...S.sim.latched])];
  api("POST", "/api/sim", { buttons, paddle_l: S.sim.paddle_l, paddle_r: S.sim.paddle_r }).catch(() => {});
}

function render(st) {
  S.status = st;
  // header
  const devices = $("#devices");
  devices.replaceChildren();
  if (st.sim) devices.append(el("span", { class: "chip warn" }, "SIM"));
  for (const d of st.devices) {
    const cls = d.warning ? "warn" : d.status === "connected" ? "ok" : "";
    let label = `${d.kind === "kickr" ? "KICKR" : "Ride"}: ${d.status}`;
    if (d.kind === "ride" && st.signals.ride_battery != null) label += ` · ${st.signals.ride_battery}%`;
    devices.append(el("span", { class: `chip ${cls}`, title: [d.name, d.detail, d.warning].filter(Boolean).join("\n") }, label));
  }
  const layers = $("#layers");
  layers.replaceChildren(...st.layers.map((l) => el("span", { class: "chip layer" }, `layer: ${l}`)));
  const err = $("#error");
  err.textContent = st.error;
  err.classList.toggle("hidden", !st.error);
  $("#sim-banner").classList.toggle("hidden", !st.sim);
  const ftp = $("#ftp");
  if (document.activeElement !== ftp && Number(ftp.value) !== st.rider.ftp_w) ftp.value = st.rider.ftp_w;
  const active = $("#active-profile");
  if (document.activeElement !== active && active.value !== st.profile) active.value = st.profile;

  renderGt7(st);

  // meters
  for (const m of METERS) {
    const row = $(`#m-${m.key}`);
    const v = st.signals[m.key];
    const fill = row.querySelector(".bar div");
    if (m.bipolar) {
      const half = Math.min(50, Math.abs(v / m.max) * 50);
      fill.style.left = `${v < 0 ? 50 - half : 50}%`;
      fill.style.width = `${half}%`;
    } else {
      fill.style.width = `${Math.min(100, (v / m.max) * 100)}%`;
    }
    row.querySelector(".val").textContent = m.pct ? `${Math.round(v * 100)} %`
      : m.max === 1 ? (v > 0 ? "+" : "") + v.toFixed(2) : `${Math.round(v)} ${m.unit}`;
    const slider = row.querySelector("input");
    if (slider) {
      slider.classList.toggle("hidden", !st.sim);
      row.querySelector(".bar").classList.toggle("hidden", st.sim);
    }
  }

  // ride buttons + learn
  const pressed = new Set(st.signals.buttons);
  for (const b of S.meta.ride_buttons) {
    const k = $(`#rb-${b}`);
    k.classList.toggle("on", pressed.has(b));
    k.classList.toggle("clickable", st.sim);
  }
  if (S.learn) {
    const fresh = [...pressed].find((b) => !S.prevButtons.has(b));
    if (fresh) finishLearn(fresh);
  }
  S.prevButtons = pressed;

  // outputs
  const o = st.output;
  const outPressed = new Set(o.buttons);
  for (const b of S.meta.ps_buttons) $(`#pb-${b}`).classList.toggle("on", outPressed.has(b));
  $("#out-l2").style.width = `${(o.l2 / 255) * 100}%`;
  $("#out-r2").style.width = `${(o.r2 / 255) * 100}%`;
  const place = (dot, x, y) => {
    dot.style.left = `${50 + (x / 32767) * 50}%`;
    dot.style.top = `${50 + (y / 32767) * 50}%`;
  };
  place($("#out-left"), o.left_x, o.left_y);
  place($("#out-right"), o.right_x, o.right_y);
  $("#chiaki-info").textContent = `Sending to chiaki-ng at ${st.chiaki.addr}` + (st.chiaki.errors ? ` · ${st.chiaki.errors} send errors` : "");
}

function renderGt7(st) {
  const s = st.signals;
  const gt7 = st.gt7;
  let status = "off (start with --gt7)";
  if (gt7) status = gt7.status === "live" ? (s.gt7_driving ? "live · driving" : "live · not driving") : `${gt7.status}${gt7.host ? ` (${gt7.host})` : ""}`;
  else if (st.sim && st.sim_gear != null) status = "simulated";
  $("#gt7-status").textContent = status;
  $("#gt7-gear").textContent = s.gt7_live ? `${s.gear || "R/N"} / ${s.num_gears}` : "–";
  $("#gt7-target").textContent = `${s.gear_target_w} W (${Math.round((s.gear_target_w / st.rider.ftp_w) * 100)}% FTP)`;
  const r = st.resistance;
  $("#gt7-trainer").textContent = !r ? "not controlled"
    : r.target ? `${r.control === "ok" ? "" : r.control + " · "}grade ${r.target.grade_pct.toFixed(1)}% (${r.target.reason})` : r.control;
  const fg = $("#first-gear");
  if (document.activeElement !== fg) fg.value = Math.round(st.rider.first_gear_ftp * 100);
  const tc = $("#target-cadence");
  if (document.activeElement !== tc) tc.value = st.rider.target_cadence_rpm;
  $("#sim-gears").classList.toggle("hidden", !st.sim);
  document.querySelectorAll("#sim-gears button").forEach((b) => {
    b.classList.toggle("active", String(st.sim_gear ?? "") === b.dataset.gear);
  });
}

function connectWs() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => render(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 1000);
}

// ---------------------------------------------------------------- profiles
async function loadProfiles(selectEdit) {
  const { profiles, active } = await api("GET", "/api/profiles");
  for (const sel of [$("#active-profile"), $("#edit-profile")]) {
    const keep = sel.value;
    sel.replaceChildren(...profiles.map((p) => el("option", { value: p }, p)));
    sel.value = keep && profiles.includes(keep) ? keep : active;
  }
  $("#active-profile").value = active;
  if (selectEdit) $("#edit-profile").value = selectEdit;
  await loadEdit($("#edit-profile").value);
}

async function loadEdit(name) {
  const data = await api("GET", `/api/profiles/${name}`);
  const parentName = data.doc.extends;
  let parent = null;
  if (parentName) {
    try { parent = (await api("GET", `/api/profiles/${parentName}`)).resolved; } catch { parent = {}; }
  }
  Object.assign(S.edit, { name, doc: data.doc, parent, yaml: data.yaml, formDirty: false, yamlDirty: false });
  $("#yaml").value = data.yaml;
  $("#form-status").textContent = "";
  $("#yaml-status").textContent = "";
  renderForm();
}

function fmtBinding(b) {
  if (b === undefined) return "—";
  if (b === null) return "none";
  if (typeof b === "string") return b;
  if (Array.isArray(b)) return `any: ${b.join(", ")}`;
  if (b.button) return b.button;
  if (b.any) return `any: ${b.any.join(", ")}`;
  if (b.combo) return `combo: ${b.combo.join(" + ")}`;
  if (b.hold) return `hold ${b.hold} ${b.ms ?? 500} ms`;
  if (b.tap) return `tap ${b.tap} < ${b.ms ?? 500} ms`;
  if (b.toggle) return `toggle ${b.toggle}`;
  if (b.signal) return `${b.signal}${b.above != null ? ` > ${b.above}` : ""}${b.below != null ? ` < ${b.below}` : ""}`;
  return JSON.stringify(b);
}

function fmtTerm(t) {
  const clip = t.min != null || t.max != null ? `[${t.min ?? ""}..${t.max ?? ""}]` : "";
  return `${t.scale ?? 1}×${t.source}${clip}`;
}

function fmtAxis(a) {
  if (a === undefined) return "—";
  if (a === null) return "disabled";
  if (typeof a === "string") return a;
  const src = a.source ?? (a.combine || []).map(fmtTerm).join(" + ");
  return `${src} [${(a.in || [0, 1]).join(", ")}] → [${(a.out || [0, 1]).join(", ")}]`;
}

function markFormDirty() {
  S.edit.formDirty = true;
  $("#form-status").textContent = "unsaved changes";
}

function renderForm() {
  const { doc, parent } = S.edit;
  const hasParent = Boolean(doc.extends);
  $("#extends-name").textContent = doc.extends || "nothing (no extends)";
  doc.buttons = doc.buttons || {};
  doc.axes = doc.axes || {};

  // Buttons
  const tbody = $("#form-buttons");
  tbody.replaceChildren();
  for (const ps of S.meta.ps_buttons) {
    const own = Object.prototype.hasOwnProperty.call(doc.buttons, ps) ? doc.buttons[ps] : undefined;
    const inherited = parent?.buttons?.[ps];
    const cell = el("td");
    const advanced = own !== undefined && own !== null && typeof own !== "string";
    if (advanced) {
      cell.append(el("span", { class: "adv" }, fmtBinding(own)), " ", el("span", { class: "muted small" }, "(edit in YAML)"));
    } else {
      const sel = el("select");
      sel.append(el("option", { value: "__inherit" }, hasParent ? `inherit (${fmtBinding(inherited)})` : "—"));
      if (hasParent) sel.append(el("option", { value: "__none" }, "none (disable)"));
      for (const rb of S.meta.ride_buttons) sel.append(el("option", { value: rb }, rb));
      sel.value = own === undefined ? "__inherit" : own === null ? "__none" : own;
      sel.addEventListener("change", () => {
        if (sel.value === "__inherit") delete doc.buttons[ps];
        else doc.buttons[ps] = sel.value === "__none" ? null : sel.value;
        markFormDirty();
      });
      cell.append(sel);
      if (own === undefined && hasParent) cell.classList.add("inherited");
    }
    const learn = el("button", { type: "button", title: "Then press a Ride button" }, S.learn === ps ? "Press a Ride button…" : "Learn");
    learn.addEventListener("click", () => { S.learn = S.learn === ps ? null : ps; renderForm(); });
    tbody.append(el("tr", {}, el("td", {}, ps), cell, el("td", {}, advanced ? "" : learn)));
  }

  // Axes
  const abody = $("#form-axes");
  abody.replaceChildren();
  for (const axis of S.meta.ps_axes) {
    let own = Object.prototype.hasOwnProperty.call(doc.axes, axis) ? doc.axes[axis] : undefined;
    const inherited = parent?.axes?.[axis];
    const row = el("tr", {}, el("td", {}, axis));
    if (own === undefined || own === null) {
      const label = own === null ? "disabled" : hasParent ? `inherit (${fmtAxis(inherited)})` : "—";
      row.append(el("td", { colspan: 7, class: own === undefined ? "inherited" : "" }, label));
      const btns = el("td");
      const override = el("button", { type: "button" }, own === null ? "Enable" : inherited ? "Override" : "Add");
      override.addEventListener("click", () => {
        const base = typeof inherited === "string" ? { source: inherited } : inherited;
        doc.axes[axis] = base ? structuredClone(base) : { source: "power_w", in: [0, 400] };
        markFormDirty();
        renderForm();
      });
      btns.append(override);
      if (hasParent && own === undefined && inherited) {
        const dis = el("button", { type: "button" }, "Disable");
        dis.addEventListener("click", () => { doc.axes[axis] = null; markFormDirty(); renderForm(); });
        btns.append(" ", dis);
      }
      row.append(btns);
      abody.append(row);
      continue;
    }
    if (typeof own === "string") own = doc.axes[axis] = { source: own };
    const a = own;
    const num = (getter, setter, step = "any") => {
      const input = el("input", { type: "number", step, value: getter() });
      input.addEventListener("change", () => { setter(Number(input.value)); markFormDirty(); });
      return input;
    };
    const pair = (key) => {
      const def = [0, 1];
      const cur = () => a[key] || def.slice();
      return el("span", {},
        num(() => cur()[0], (v) => { const p = cur(); p[0] = v; a[key] = p; }), " … ",
        num(() => cur()[1], (v) => { const p = cur(); p[1] = v; a[key] = p; }));
    };
    let sourceCell;
    if (a.combine) {
      sourceCell = el("td", { class: "adv" }, a.combine.map(fmtTerm).join(" + "));
    } else {
      const sel = el("select");
      for (const s of S.meta.sources) sel.append(el("option", { value: s }, s));
      sel.value = a.source;
      sel.addEventListener("change", () => { a.source = sel.value; markFormDirty(); });
      sourceCell = el("td", {}, sel);
    }
    const curve = el("select", {}, el("option", { value: "linear" }, "linear"), el("option", { value: "expo" }, "expo"));
    curve.value = a.curve || "linear";
    curve.addEventListener("change", () => { a.curve = curve.value; markFormDirty(); });
    const remove = el("button", { type: "button" }, hasParent ? "Inherit" : "Remove");
    remove.addEventListener("click", () => { delete doc.axes[axis]; markFormDirty(); renderForm(); });
    row.append(
      sourceCell,
      el("td", {}, pair("in")),
      el("td", {}, pair("out")),
      el("td", {}, curve),
      el("td", {}, num(() => a.gamma ?? 2, (v) => { a.gamma = v; }, 0.1)),
      el("td", {}, num(() => a.deadzone ?? 0, (v) => { a.deadzone = v; }, 0.01)),
      el("td", {}, num(() => a.smooth_ms ?? 0, (v) => { a.smooth_ms = v; }, 10)),
      el("td", {}, remove),
    );
    abody.append(row);
  }
}

function finishLearn(rideButton) {
  const ps = S.learn;
  S.learn = null;
  const current = S.edit.doc.buttons[ps];
  if (current === undefined || current === null || typeof current === "string") {
    S.edit.doc.buttons[ps] = rideButton;
    markFormDirty();
  }
  renderForm();
}

async function saveForm() {
  const doc = structuredClone(S.edit.doc);
  for (const k of ["buttons", "axes"]) if (doc[k] && !Object.keys(doc[k]).length) delete doc[k];
  try {
    await api("PUT", `/api/profiles/${S.edit.name}/doc`, { doc });
    await loadEdit(S.edit.name);
    $("#form-status").innerHTML = '<span class="ok-text">saved</span>';
  } catch (e) {
    $("#form-status").innerHTML = "";
    $("#form-status").append(el("span", { class: "err-text" }, e.message));
  }
}

let validateTimer = null;
function onYamlInput() {
  S.edit.yamlDirty = true;
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    const res = await api("POST", `/api/profiles/${S.edit.name}/validate`, { yaml: $("#yaml").value });
    const status = $("#yaml-status");
    status.replaceChildren(res.ok ? el("span", { class: "ok-text" }, "valid · unsaved") : el("span", { class: "err-text" }, res.error));
  }, 350);
}

async function saveYaml() {
  const status = $("#yaml-status");
  try {
    await api("PUT", `/api/profiles/${S.edit.name}`, { yaml: $("#yaml").value });
    await loadEdit(S.edit.name);
    status.replaceChildren(el("span", { class: "ok-text" }, "saved"));
  } catch (e) {
    status.replaceChildren(el("span", { class: "err-text" }, e.message));
  }
}

function switchTab(tab) {
  if (tab === S.edit.tab) return;
  const dirty = S.edit.tab === "form" ? S.edit.formDirty : S.edit.yamlDirty;
  if (dirty && !confirm("Discard unsaved changes?")) return;
  S.edit.tab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  $("#tab-form").classList.toggle("hidden", tab !== "form");
  $("#tab-yaml").classList.toggle("hidden", tab !== "yaml");
  if (dirty) loadEdit(S.edit.name);
}

async function newProfile() {
  const name = prompt("New profile file name (a-z, 0-9, - or _):");
  if (!name) return;
  const parent = $("#edit-profile").value || "menu";
  const yaml = `name: ${name}\nextends: ${parent}\ndescription: ""\n`;
  try {
    await api("POST", `/api/profiles/${name}`, { yaml }); // 409 if it already exists
    await loadProfiles(name);
  } catch (e) {
    alert(e.message);
  }
}

// ---------------------------------------------------------------- boot
async function main() {
  S.meta = await api("GET", "/api/meta");
  buildLive();
  await loadProfiles();
  connectWs();

  $("#active-profile").addEventListener("change", async (e) => {
    try { await api("POST", `/api/profiles/${e.target.value}/activate`); } catch (err) { alert(err.message); }
  });
  $("#edit-profile").addEventListener("change", (e) => {
    const dirty = S.edit.formDirty || S.edit.yamlDirty;
    if (dirty && !confirm("Discard unsaved changes?")) { e.target.value = S.edit.name; return; }
    loadEdit(e.target.value);
  });
  $("#new-profile").addEventListener("click", newProfile);
  $("#first-gear").addEventListener("change", (e) => {
    api("PUT", "/api/rider", { first_gear_ftp: Number(e.target.value) / 100 }).catch(() => alert("1st gear must be 10–100% of FTP"));
  });
  $("#target-cadence").addEventListener("change", (e) => {
    api("PUT", "/api/rider", { target_cadence_rpm: Number(e.target.value) }).catch(() => alert("Cadence must be 50–130 rpm"));
  });
  $("#ftp").addEventListener("change", async (e) => {
    try { await api("PUT", "/api/rider", { ftp_w: Number(e.target.value) }); }
    catch (err) { alert("FTP must be between 50 and 600 W"); }
  });
  $("#form-save").addEventListener("click", saveForm);
  $("#yaml-save").addEventListener("click", saveYaml);
  $("#yaml").addEventListener("input", onYamlInput);
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
}

main();

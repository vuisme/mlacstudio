// MLAC Studio standalone page.
"use strict";

const $ = (id) => document.getElementById(id);

// qwen_image_2_1.generate.ASPECT_RATIOS
const RATIOS = {
  "1:1": [2048, 2048], "4:3": [2400, 1792], "3:4": [1792, 2400], "3:2": [2528, 1696],
  "2:3": [1696, 2528], "16:9": [2752, 1536], "9:16": [1536, 2752],
};
const STAGES = [["load", "Load"], ["denoise", "Denoise"], ["decode", "Decode"], ["save", "Save"]];
const FIELDS = ["prompt", "width", "height", "steps", "seed"];
const ROLE_LABELS = {
  base: "Base / edit", subject: "Subject", style: "Style", composition: "Composition",
  identity: "Identity", background: "Background", reference: "Reference",
};
const PRESET_PROMPTS = {
  transparent: (prompt) => `This is an RGBA image with transparency. ${prompt} The image has alpha channel and the background is transparent.`,
  "subject-extraction": (prompt) => `This is an RGBA image with transparency. Extract the main subject from the base image. ${prompt} Preserve identity, detail, and color. The image has alpha channel and the background is transparent.`,
};
let csrfToken = "";

const ui = {
  session: "", paths: {}, ratio: null, inputs: [], takes: [], queue: [],
  model: { status: "unloaded", pid: null, backend: "sd-server" }, idleTimeout: 300,
  models: { available: false },
  updates: { status: "idle", current_version: "-", channel: "stable" },
  sourcePreview: null, sourceDraftInitialized: false,
  selected: null, // gallery item id of the take in the preview
  live: null, // the running job's summary, for the stage bar
  maskInfo: null,
  capabilities: { multi_reference: true, max_references: 10, rgba: false, rgba_reported: false },
  referenceRoles: Object.keys(ROLE_LABELS),
};

const maskState = {
  tool: "brush", canvas: null, ctx: null, drawing: false, last: null,
  undo: [], redo: [], dirtyVersion: 0, savedVersion: 0, saveTimer: null,
  savePromise: Promise.resolve(), loadToken: 0,
};

// ── helpers ─────────────────────────────────────────────────────────────
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...kids.filter((kid) => kid !== null && kid !== undefined && kid !== false));
  return node;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("authentication required"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || (data && data.error)) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

const q = (path) => `${path}?session=${encodeURIComponent(ui.session)}`;
const snap32 = (v) => Math.round(v / 32) * 32;

function showError(node, err) {
  node.textContent = err ? err.message || String(err) : "";
  node.hidden = !err;
}

/** A small in-page dialog: a name to type, or a yes/no. Resolves to the text, true, or null. */
function ask({ title, message = "", value = null, ok = "OK" }) {
  const d = $("askDialog");
  $("askTitle").textContent = title;
  $("askMessage").textContent = message;
  $("askInput").hidden = value === null;
  $("askInput").value = value || "";
  $("askOk").textContent = ok;
  showError($("askError"), null);
  d.showModal();
  if (value !== null) $("askInput").select();
  return new Promise((resolve) => {
    const done = (result) => { d.close(); cleanup(); resolve(result); };
    const onOk = () => done(value === null ? true : $("askInput").value.trim() || null);
    const onCancel = () => done(null);
    const onKey = (e) => { if (e.key === "Enter" && value !== null) onOk(); };
    const cleanup = () => {
      $("askOk").removeEventListener("click", onOk);
      $("askCancel").removeEventListener("click", onCancel);
      $("askInput").removeEventListener("keydown", onKey);
      d.removeEventListener("cancel", onCancel);
    };
    $("askOk").addEventListener("click", onOk);
    $("askCancel").addEventListener("click", onCancel);
    $("askInput").addEventListener("keydown", onKey);
    d.addEventListener("cancel", onCancel);
  });
}

// ── the form ────────────────────────────────────────────────────────────
function settings() {
  const num = (id) => ($(id).value === "" ? null : Number($(id).value));
  return {
    prompt: $("prompt").value, ratio: ui.ratio, width: num("width"), height: num("height"),
    steps: num("steps") ?? 20, seed: num("seed") ?? 42, preset: $("preset").value,
  };
}

function applySettings(s) {
  $("prompt").value = s.prompt ?? "";
  $("width").value = s.width ?? "";
  $("height").value = s.height ?? "";
  $("steps").value = s.steps ?? 20;
  $("seed").value = s.seed ?? 42;
  $("preset").value = s.preset ?? "none";
  ui.ratio = s.ratio || null;
  update();
}

/** What size the CLI will render, mirroring generate.py and the pipeline's own default. */
function plannedSize() {
  const s = settings();
  let base = null;
  let source;
  if (s.ratio) {
    base = RATIOS[s.ratio];
    source = "chosen";
  } else if (ui.inputs.length) {
    const baseInput = ui.inputs.find((input) => input.role === "base") || ui.inputs[0];
    const r = (baseInput.width || 1) / (baseInput.height || 1);
    const w = Math.sqrt(1024 * 1024 * r);
    base = [snap32(w), snap32(w / r)];
    source = "follows the base image";
  } else {
    base = RATIOS["1:1"];
    source = "default 1:1";
  }
  const w = s.width || (base && base[0]);
  const h = s.height || (base && base[1]);
  return { w, h, source };
}

function update() {
  const edit = ui.inputs.length > 0;
  for (const span of $("modeSeg").children) span.classList.toggle("on", (span.dataset.m === "i2i") === edit);
  $("modeSeg").lastElementChild.textContent = edit ? `Edit · ${ui.inputs.length} image${ui.inputs.length === 1 ? "" : "s"}` : "Edit";
  $("promptCount").textContent = `${$("prompt").value.length} chars`;
  const maxRefs = Math.min(10, Number(ui.capabilities.max_references || 1));
  $("refCount").textContent = `${ui.inputs.length}/${maxRefs}`;
  $("fileInput").disabled = ui.inputs.length >= maxRefs;
  $("drop").classList.toggle("disabled", ui.inputs.length >= maxRefs);
  const alphaPreset = $("preset").value !== "none";
  $("rgbaWarning").hidden = !alphaPreset || ui.capabilities.rgba;
  $("rgbaWarning").textContent = ui.capabilities.rgba_reported
    ? "The active backend reports no RGBA output support, so this preset cannot be rendered."
    : "RGBA output support has not been reported by the backend. This preset can request transparency, but alpha is not guaranteed.";
  $("generate").disabled = alphaPreset && ui.capabilities.rgba_reported && !ui.capabilities.rgba;

  const { w, h, source } = plannedSize();
  $("ratioSrc").textContent = ui.ratio ? "chosen" : `auto · ${source}`;
  $("dims").textContent = w && h ? `${w} × ${h}` : "—";
  $("mp").textContent = w && h ? ((w * h) / 1e6).toFixed(2) : "—";
  const ok = (!w || w % 32 === 0) && (!h || h % 32 === 0);
  $("grid32").textContent = ok ? "ok" : "not a multiple";
  $("grid32").classList.toggle("warn", !ok);
  renderRatios();
  renderRefs();
  renderCommand();
  renderPipeline();
}

function renderRatios() {
  const options = [[null, "Auto"], ...Object.keys(RATIOS).map((k) => [k, k])];
  $("ratios").replaceChildren(...options.map(([key, label]) => {
    const [w, h] = key ? RATIOS[key] : [1, 1];
    const s = 22 / Math.max(w, h);
    return el("button", {
      type: "button", class: `ratio${key ? "" : " auto"}`, "aria-pressed": String(ui.ratio === key),
      onclick: () => { ui.ratio = key; update(); saveSoon(); },
    }, el("i", { style: `width:${Math.round(w * s)}px;height:${Math.round(h * s)}px` }), label);
  }));
}

function renderCommand() {
  const s = settings();
  const quote = (t) => (/^[\w./:=@+,-]+$/.test(t) ? t : `"${t.replace(/"/g, '\\"')}"`);
  const rawPrompt = PRESET_PROMPTS[s.preset] ? PRESET_PROMPTS[s.preset](s.prompt) : s.prompt;
  const tagged = resolveMentions(rawPrompt);
  const p = tagged.length > 90 ? `${tagged.slice(0, 90)}…` : tagged;
  const parts = [`<span class="k">sd-cli</span> --prompt ${escapeHtml(quote(p || "…"))}`];
  for (const input of ui.inputs) parts.push(`--ref-image ${escapeHtml(input.name)}`);
  if (ui.maskInfo) parts.push("--mask mask.png");
  const planned = plannedSize();
  if (planned.w) parts.push(`--width ${planned.w}`);
  if (planned.h) parts.push(`--height ${planned.h}`);
  if (s.steps !== 20) parts.push(`--steps ${s.steps}`);
  if (s.seed !== 42) parts.push(`--seed ${s.seed}`);
  $("cmd").innerHTML = parts.join(" \\\n  ");
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

let saveTimer;
function saveSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api("/api/session/save", { session: ui.session, settings: settings() }).catch(() => {}), 600);
}

// ── references ──────────────────────────────────────────────────────────
// ── @ mentions: a reference image named in the prompt ───────────────────
// The page writes @name; the server turns it into <imageN> by the list's order (web/server.py
// resolve_mentions), so a mention still points at its image after another is removed.
const MENTION_RE = /@([A-Za-z0-9._-]*[A-Za-z0-9_-])/g;
const mention = { start: -1, items: [], at: 0 };

function resolveMentions(text) {
  const index = new Map(ui.inputs.map((input, i) => [input.name, i + 1]));
  return text.replace(MENTION_RE, (m, name) => (index.has(name) ? `<image${index.get(name)}>` : m));
}

/** The "@query" being typed just before the caret, or null. An @ inside a word (an email) is not one. */
function mentionQuery() {
  const box = $("prompt");
  const before = box.value.slice(0, box.selectionStart);
  const m = /(^|[^A-Za-z0-9._-])@([A-Za-z0-9._-]*)$/.exec(before);
  return m ? { start: before.length - m[2].length - 1, query: m[2].toLowerCase() } : null;
}

function renderMentions() {
  const found = ui.inputs.length ? mentionQuery() : null;
  mention.items = found ? ui.inputs.filter((input) => input.name.toLowerCase().includes(found.query)) : [];
  mention.start = found ? found.start : -1;
  mention.at = Math.min(mention.at, Math.max(0, mention.items.length - 1));
  $("mentions").hidden = !mention.items.length;
  $("mentions").replaceChildren(...mention.items.map((input, i) => el("button", {
    type: "button", class: "mention", role: "option", "aria-selected": String(i === mention.at),
    onmousedown: (e) => { e.preventDefault(); pickMention(input.name); }, // before the textarea blurs
  },
  el("img", { src: input.thumb, alt: "" }),
  el("span", { class: "num", text: `<image${ui.inputs.indexOf(input) + 1}>` }),
  el("span", { class: "name", text: input.name }))));
}

function pickMention(name) {
  const box = $("prompt");
  const end = box.selectionStart;
  box.setRangeText(`@${name} `, mention.start, end, "end");
  $("mentions").hidden = true;
  box.dispatchEvent(new Event("input"));
  box.focus();
}

function insertMention(name) {
  const box = $("prompt");
  const at = box.selectionStart ?? box.value.length;
  const pad = at > 0 && !/\s/.test(box.value[at - 1]) ? " " : "";
  box.setRangeText(`${pad}@${name} `, at, box.selectionEnd ?? at, "end");
  box.dispatchEvent(new Event("input"));
  box.focus();
}

function onMentionKey(e) {
  if ($("mentions").hidden) return;
  const n = mention.items.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    mention.at = (mention.at + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    renderMentions();
  } else if (e.key === "Enter" || e.key === "Tab") {
    e.preventDefault();
    pickMention(mention.items[mention.at].name);
  } else if (e.key === "Escape") {
    e.preventDefault();
    $("mentions").hidden = true;
  }
}

function renderRefs() {
  $("refs").replaceChildren(...ui.inputs.map((input, index) => {
    const roleSelect = el("select", {
      "aria-label": `Role for ${input.original_name}`,
      onchange: async (event) => {
        await saveMaskNow();
        try {
          await api("/api/references/role", { session: ui.session, id: input.id, role: event.target.value });
        } finally {
          loadInputs();
        }
      },
    }, ...ui.referenceRoles.map((role) => el("option", {
      value: role, text: ROLE_LABELS[role] || role,
      selected: role === input.role,
      disabled: input.role === "base" && role !== "base",
    })));
    return el("div", {
      class: `ref${input.role === "base" ? " base" : ""}`,
      title: `${input.original_name} · ${input.width}×${input.height}${input.has_alpha ? " · alpha" : ""}`,
    },
    el("img", { src: input.thumb, alt: input.original_name, loading: "lazy", title: `Add @${input.name} to the prompt`, onclick: () => insertMention(input.name) }),
    el("span", { class: "ref-meta" },
      el("b", { text: `${index + 1}. ${input.name}` }),
      el("small", { text: `${input.width}x${input.height}${input.has_alpha ? " · RGBA" : ""}` })),
    roleSelect,
    el("span", { class: "ref-actions" },
      el("button", { type: "button", text: "^", title: "Move up", disabled: index === 0 || ui.inputs[index - 1]?.role === "base", onclick: () => moveReference(index, -1) }),
      el("button", { type: "button", text: "v", title: "Move down", disabled: input.role === "base" || index === ui.inputs.length - 1, onclick: () => moveReference(index, 1) }),
      el("button", {
        type: "button", text: "x", title: `Remove ${input.original_name}`,
        onclick: async () => {
          await saveMaskNow();
          await api("/api/references/remove", { session: ui.session, id: input.id });
          loadInputs();
        },
      })));
  }));
}

async function moveReference(index, offset) {
  const ordered = ui.inputs.map((input) => input.id);
  [ordered[index], ordered[index + offset]] = [ordered[index + offset], ordered[index]];
  ui.inputs = await api("/api/references/reorder", { session: ui.session, ordered_ids: ordered });
  update();
}

function setMaskTool(tool) {
  maskState.tool = tool;
  for (const [id, value] of [["maskBrush", "brush"], ["maskErase", "erase"]]) {
    const active = tool === value;
    $(id).classList.toggle("on", active);
    $(id).setAttribute("aria-pressed", String(active));
  }
}

function updateMaskButtons() {
  $("maskUndo").disabled = !maskState.undo.length;
  $("maskRedo").disabled = !maskState.redo.length;
}

function setMaskControlsDisabled(disabled) {
  for (const id of ["maskBrush", "maskErase", "maskSize", "maskFeather", "maskClear", "maskInvert"]) {
    $(id).disabled = disabled;
  }
  if (disabled) {
    $("maskUndo").disabled = true;
    $("maskRedo").disabled = true;
  } else {
    updateMaskButtons();
  }
}

function maskSnapshot() {
  const { canvas, ctx } = maskState;
  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

function rememberMask() {
  maskState.undo.push(maskSnapshot());
  const pixels = maskState.canvas.width * maskState.canvas.height;
  const limit = pixels > 8 * 1024 * 1024 ? 2 : pixels > 4 * 1024 * 1024 ? 4 : 12;
  if (maskState.undo.length > limit) maskState.undo.shift();
  maskState.redo.length = 0;
  updateMaskButtons();
}

function restoreMask(snapshot) {
  maskState.ctx.clearRect(0, 0, maskState.canvas.width, maskState.canvas.height);
  maskState.ctx.putImageData(snapshot, 0, 0);
  maskChanged();
}

function maskChanged() {
  maskState.dirtyVersion += 1;
  ui.maskInfo = { pending: true };
  $("maskStatus").textContent = "Unsaved";
  renderCommand();
  clearTimeout(maskState.saveTimer);
  maskState.saveTimer = setTimeout(() => saveMaskNow().catch((err) => {
    $("maskStatus").textContent = err.message;
  }), 500);
}

function maskPoint(event) {
  const rect = maskState.canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) * maskState.canvas.width / rect.width,
    y: (event.clientY - rect.top) * maskState.canvas.height / rect.height,
  };
}

function stampMask(x, y) {
  const ctx = maskState.ctx;
  const radius = Number($("maskSize").value) / 2;
  const feather = Number($("maskFeather").value) / 100;
  ctx.save();
  ctx.globalCompositeOperation = maskState.tool === "erase" ? "destination-out" : "source-over";
  if (feather > 0) {
    const inner = radius * (1 - feather);
    const gradient = ctx.createRadialGradient(x, y, inner, x, y, radius);
    gradient.addColorStop(0, "rgba(220, 28, 28, 1)");
    gradient.addColorStop(1, "rgba(220, 28, 28, 0)");
    ctx.fillStyle = gradient;
  } else {
    ctx.fillStyle = "rgb(220, 28, 28)";
  }
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function drawMaskSegment(from, to) {
  const radius = Number($("maskSize").value) / 2;
  const distance = Math.hypot(to.x - from.x, to.y - from.y);
  const count = Math.max(1, Math.ceil(distance / Math.max(1, radius / 3)));
  for (let i = 1; i <= count; i++) {
    const at = i / count;
    stampMask(from.x + (to.x - from.x) * at, from.y + (to.y - from.y) * at);
  }
}

function maskBlob() {
  const source = maskState.canvas;
  const white = document.createElement("canvas");
  white.width = source.width;
  white.height = source.height;
  const whiteCtx = white.getContext("2d");
  whiteCtx.drawImage(source, 0, 0);
  whiteCtx.globalCompositeOperation = "source-in";
  whiteCtx.fillStyle = "white";
  whiteCtx.fillRect(0, 0, white.width, white.height);

  const output = document.createElement("canvas");
  output.width = source.width;
  output.height = source.height;
  const outputCtx = output.getContext("2d", { alpha: false });
  outputCtx.fillStyle = "black";
  outputCtx.fillRect(0, 0, output.width, output.height);
  outputCtx.drawImage(white, 0, 0);
  return new Promise((resolve, reject) => output.toBlob(
    (blob) => blob ? resolve(blob) : reject(new Error("could not encode mask PNG")), "image/png"
  ));
}

async function saveMaskNow() {
  clearTimeout(maskState.saveTimer);
  if (!ui.inputs.length || !maskState.canvas?.width) return;
  const version = maskState.dirtyVersion;
  if (version === maskState.savedVersion) return maskState.savePromise;
  const blob = await maskBlob();
  $("maskStatus").textContent = "Saving...";
  maskState.savePromise = maskState.savePromise.catch(() => {}).then(async () => {
    const res = await fetch(q("/api/mask"), {
      method: "POST",
      headers: {
        "Content-Type": "image/png", "X-CSRF-Token": csrfToken,
        "X-Mask-Feather": $("maskFeather").value,
      },
      body: blob,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    if (version === maskState.dirtyVersion) {
      maskState.savedVersion = version;
      ui.maskInfo = data.mask;
      $("maskStatus").textContent = data.mask ? "Saved" : "No selection";
      renderCommand();
    }
    return data.mask;
  });
  return maskState.savePromise;
}

async function drawStoredMask(url, token) {
  const image = new Image();
  image.decoding = "async";
  await new Promise((resolve, reject) => {
    image.onload = resolve;
    image.onerror = () => reject(new Error("could not load the saved mask"));
    image.src = url;
  });
  if (token !== maskState.loadToken) return;
  const temp = document.createElement("canvas");
  temp.width = maskState.canvas.width;
  temp.height = maskState.canvas.height;
  const tempCtx = temp.getContext("2d");
  tempCtx.drawImage(image, 0, 0, temp.width, temp.height);
  const pixels = tempCtx.getImageData(0, 0, temp.width, temp.height);
  for (let i = 0; i < pixels.data.length; i += 4) {
    const value = pixels.data[i];
    pixels.data[i] = 220;
    pixels.data[i + 1] = 28;
    pixels.data[i + 2] = 28;
    pixels.data[i + 3] = value;
  }
  maskState.ctx.putImageData(pixels, 0, 0);
}

async function loadMaskEditor() {
  const input = ui.inputs.find((item) => item.role === "base");
  const token = ++maskState.loadToken;
  clearTimeout(maskState.saveTimer);
  maskState.undo.length = 0;
  maskState.redo.length = 0;
  maskState.dirtyVersion = 0;
  maskState.savedVersion = 0;
  ui.maskInfo = null;
  updateMaskButtons();
  $("maskEditor").hidden = !input;
  if (!input) {
    setMaskControlsDisabled(true);
    $("maskStatus").textContent = "No selection";
    renderCommand();
    return;
  }
  if (input.width * input.height > 16 * 1024 * 1024) {
    setMaskControlsDisabled(true);
    maskState.canvas.width = 1;
    maskState.canvas.height = 1;
    $("maskStatus").textContent = "Image too large to mask";
    $("maskCanvasWrap").hidden = true;
    return;
  }
  setMaskControlsDisabled(false);
  $("maskCanvasWrap").hidden = false;
  $("maskCanvasWrap").style.setProperty("--mask-ratio", String(input.width / input.height));
  $("maskSource").src = input.url;
  maskState.canvas.width = input.width;
  maskState.canvas.height = input.height;
  maskState.ctx.clearRect(0, 0, input.width, input.height);
  const data = await api(q("/api/mask"));
  if (token !== maskState.loadToken) return;
  ui.maskInfo = data.mask;
  if (data.mask) {
    $("maskFeather").value = data.mask.feather;
    $("maskFeatherValue").textContent = `${data.mask.feather}%`;
    await drawStoredMask(data.mask.url, token);
  }
  $("maskStatus").textContent = data.mask ? "Saved" : "No selection";
  renderCommand();
}

async function loadInputs() {
  ui.inputs = await api(q("/api/inputs"));
  update();
  await loadMaskEditor();
}

async function uploadFiles(files) {
  await saveMaskNow();
  const available = Math.max(0, Math.min(10, Number(ui.capabilities.max_references || 1)) - ui.inputs.length);
  if (!available) {
    termLine("[studio] The active backend reference limit has been reached.", "e");
    return;
  }
  for (const file of files.slice(0, available)) {
    const res = await fetch(q("/api/references/add"), {
      method: "POST", headers: { "X-Filename": encodeURIComponent(file.name), "X-CSRF-Token": csrfToken }, body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { termLine(`[studio] ${file.name}: ${data.error || res.status}`, "e"); break; }
  }
  loadInputs();
}

// ── takes ───────────────────────────────────────────────────────────────
async function loadTakes(selectNewest = false) {
  ui.takes = await api(q("/api/takes"));
  if (selectNewest || !ui.takes.some((t) => t.id === ui.selected)) ui.selected = ui.takes[0]?.id ?? null;
  renderTakes();
  showTake();
}

function renderTakes() {
  $("takeCount").textContent = `(${ui.takes.length})`;
  if (!ui.takes.length) {
    $("takes").replaceChildren(el("div", { class: "none", text: "No takes in this session yet." }));
    return;
  }
  $("takes").replaceChildren(...ui.takes.map((take) => {
    const p = take.params;
    const act = (label, glyph, fn) => el("span", {
      title: label, role: "button", tabindex: "0", text: glyph,
      onclick: (e) => { e.stopPropagation(); fn(take); },
    });
    return el("button", {
      type: "button", class: `take${take.id === ui.selected ? " on" : ""}`,
      onclick: () => { ui.selected = take.id; renderTakes(); showTake(); },
    },
    el("div", { class: "thumb" },
      el("img", { src: take.thumb, alt: "", loading: "lazy" }),
      el("div", { class: "flags" },
        el("b", { text: p.edit ? "EDIT" : "T2I" }),
        null),
      el("div", { class: "acts" },
        act("Use as reference", "↩", useTake), act("Reuse settings", "⟲", reuseTake), act("Delete", "✕", deleteTake))),
    el("div", { class: "meta" }, `seed ${p.seed} · ${p.width}×${p.height}`, el("span", { text: p.prompt || "" })));
  }));
}

function showTake() {
  const take = ui.takes.find((t) => t.id === ui.selected);
  const p = take?.params || {};
  $("frameImg").hidden = !take;
  $("frameEmpty").hidden = !!take;
  $("frameBadge").hidden = !take;
  $("useTake").disabled = $("reuseTake").disabled = !take;
  $("openTake").hidden = !take;
  $("stageRatio").hidden = !take;
  if (!take) {
    $("stageTitle").textContent = "No take yet";
    $("frame").style.setProperty("--r", plannedRatio());
    showEnhanced(null);
    return;
  }
  $("frameImg").src = take.url;
  $("openTake").href = take.url;
  $("frame").style.setProperty("--r", String(p.width / p.height));
  $("stageTitle").textContent = p.name || take.id;
  $("stageRatio").textContent = `${p.width}×${p.height}`;
  $("frameBadge").textContent = `seed ${p.seed} · ${p.steps} steps · ${p.mode}${p.elapsed ? ` · ${Math.round(p.elapsed)}s` : ""}`;
  showEnhanced(null);
}

function plannedRatio() {
  const { w, h } = plannedSize();
  return w && h ? String(w / h) : "1";
}

function showEnhanced() {
  $("enhancedBox").hidden = true;
}

async function useTake(take) {
  try {
    await saveMaskNow();
    await api("/api/takes/use", { session: ui.session, id: take.id });
    loadInputs();
  } catch (err) {
    termLine(`[studio] ${err.message}`, "e");
  }
}

function reuseTake(take) {
  applySettings(take.params.settings || take.params);
  saveSoon();
}

async function deleteTake(take) {
  const yes = await ask({ title: "Delete this take?", message: `${take.params.name} leaves the gallery. Takes made from it keep their history.`, ok: "Delete" });
  if (!yes) return;
  await api("/api/takes/delete", { id: take.id });
  loadTakes();
}

// ── queue, progress, terminal ───────────────────────────────────────────
function renderQueue() {
  renderModelStatus();
  if (!ui.queue.length) {
    $("queue").replaceChildren(el("div", { class: "none", text: "Nothing queued." }));
    return;
  }
  $("queue").replaceChildren(...ui.queue.map((job, i) => el("div", { class: "qi" },
    el("span", { class: `dot ${job.status === "running" ? "busy" : ""}` }),
    el("span", { text: job.status === "running" ? "Rendering" : job.status === "cancelling" ? "Cancelling" : `Queued #${i + 1}` }),
    el("button", { class: "link", type: "button", text: "cancel", onclick: () => api("/api/cancel", { id: job.id }) }),
    el("small", { text: `${job.prompt} · ${job.ratio || "auto"} · seed ${job.seed}` }))));
}

function renderModelStatus() {
  const state = ui.model?.status || "unloaded";
  const busy = state === "loading" || state === "rendering";
  const failed = state === "error";
  $("lamp").className = `dot ${failed ? "off" : busy ? "busy" : "on"}`;
  $("lampText").textContent = ui.queue.length && state === "unloaded" ? "queued" : state;
  $("modelDot").className = `dot ${failed || state === "unloaded" ? "off" : busy ? "busy" : "on"}`;
  $("modelState").textContent = `${state} · ${ui.model?.backend || "sd-server"}`;
  $("modelPid").textContent = ui.model?.pid ? `PID ${ui.model.pid}` : "";
  $("modelState").title = ui.model?.error || "";
}

function renderPipeline() {
  const job = ui.live;
  const at = job ? STAGES.findIndex(([key]) => key === job.progress.stage) : -1;
  const steps = job?.progress.total || $("steps").value || 20;
  $("pipeline").replaceChildren(...STAGES.map(([key, name], i) => {
    let cls = "";
    let width = null;
    if (job && i < at) cls = "done";
    else if (job && i === at) {
      cls = "now";
      width = key === "denoise" && job.progress.total ? (100 * job.progress.step) / job.progress.total : 35;
    }
    const sub = { load: "GGUF · CUDA", denoise: `${steps} steps`, decode: "VAE", save: "PNG" }[key];
    return el("div", { class: `stepc ${cls}` },
      el("div", { class: "bar" }, el("i", { style: width === null ? null : `width:${width}%` })), name, el("small", { text: sub }));
  }));
  $("progressActions").hidden = !job;
  $("frameLive").hidden = !job;
  $("generate").textContent = job ? "Add to queue" : "Generate";
  if (!job) return;
  const p = job.progress;
  const label = p.stage === "denoise" && p.total ? `step ${p.step}/${p.total}` : (STAGES.find(([k]) => k === p.stage)?.[1] ?? "Starting") + "…";
  $("frameLive").textContent = label;
  $("progressText").textContent = label;
}

let lastRewrites = false;
function termLine(text, cls) {
  const term = $("term");
  const line = el("span", { class: cls || null, text: `${text}\n` });
  if (lastRewrites && term.lastChild) term.lastChild.replaceWith(line);
  else term.append(line);
  while (term.childNodes.length > 2000) term.firstChild.remove();
  term.scrollTop = term.scrollHeight;
}

function onEvent(event) {
  const { type, data } = JSON.parse(event.data);
  if (type === "queue") {
    ui.queue = data;
    const running = data.find((j) => j.status === "running" || j.status === "cancelling");
    ui.live = running && running.session === ui.session ? running : null;
    renderQueue();
    renderPipeline();
  } else if (type === "progress" && ui.live && data.id === ui.live.id) {
    ui.live.progress = data.progress;
    ui.live.result = data.result;
    renderPipeline();
  } else if (type === "log" && data.session === ui.session) {
    const cls = { cmd: "m", done: "a", failed: "e", cancelled: "e" }[data.kind] || (data.line.startsWith("Saved ") ? "a" : null);
    termLine(data.line, cls);
    lastRewrites = data.rewrites;
  } else if (type === "job" && data.session === ui.session) {
    if (ui.live && ui.live.id === data.id) ui.live = null;
    renderPipeline();
    if (data.status === "failed") showError($("formError"), new Error(data.error || "the render failed"));
    loadTakes(data.status === "done");
  } else if (type === "inputs") {
    loadInputs();
  } else if (type === "model") {
    ui.model = data;
    if (data.capabilities) ui.capabilities = data.capabilities;
    renderModelStatus();
    update();
  } else if (type === "models") {
    ui.models = data;
    renderModels();
  } else if (type === "updates") {
    ui.updates = data;
    renderUpdates();
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = onEvent;
  source.onerror = () => {
    $("lamp").className = "dot off";
    $("lampText").textContent = "reconnecting…";
  };
}

// ── sessions and paths ──────────────────────────────────────────────────
function renderSessions(names) {
  $("sessionSelect").replaceChildren(...names.map((n) => el("option", { value: n, text: n, selected: n === ui.session })));
}

async function openSession(name, settingsOverride) {
  await saveMaskNow();
  const res = await api("/api/session/activate", { session: name });
  ui.session = res.name;
  applySettings(settingsOverride || res.settings || {});
  $("term").replaceChildren();
  const cfg = await api("/api/config");
  renderSessions(cfg.sessions);
  await Promise.all([loadInputs(), loadTakes()]);
}

function renderPaths(paths) {
  ui.paths = paths;
  $("modelPath").textContent = paths.transformer.path || "not set";
  $("modelBad").hidden = Object.values(paths).every((item) => item.ok);
}

const formatBytes = (value) => {
  if (!Number.isFinite(Number(value))) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
};

const UPDATE_BUSY = new Set(["checking", "downloading", "installing", "rolling_back", "restarting"]);

function renderUpdates() {
  const state = ui.updates || {};
  const latest = state.latest;
  const busy = UPDATE_BUSY.has(state.status);
  const total = Number(state.bytes_total || 0);
  const done = Number(state.bytes_downloaded || 0);
  const percent = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
  const labels = {
    idle: "Ready", checking: "Checking", available: "Available", up_to_date: "Up to date",
    skipped: "Skipped", downloading: "Downloading", installing: "Installing", restart_pending: "Restart ready",
    rolling_back: "Rolling back", restarting: "Restarting", error: "Error",
  };
  $("updateCurrent").textContent = state.current_version || "-";
  $("updateLatest").textContent = latest?.version || (state.startup_check_pending ? "Checking..." : state.current_version || "-");
  $("updateChannel").value = state.channel || "stable";
  $("updateChannel").disabled = busy;
  $("updateCheck").disabled = busy;
  $("updateState").textContent = labels[state.status] || state.status || "Ready";
  $("updateState").className = `update-state${state.status === "error" ? " error" : ["up_to_date", "restart_pending"].includes(state.status) ? " ok" : ""}`;
  $("updatesBadge").hidden = !["available", "restart_pending", "error"].includes(state.status);

  $("updateRelease").hidden = !latest;
  if (latest) {
    $("updateReleaseTitle").textContent = `MLAC Studio ${latest.version} / ${latest.channel}`;
    $("updateSize").textContent = `${formatBytes(latest.size)} / ${(latest.components || []).join(", ")}`;
    $("updateChangelog").textContent = latest.changelog || "Signed maintenance release.";
    $("updateMigration").hidden = !latest.data_migration;
    $("updateMigration").textContent = latest.data_migration ? `Data migration: ${latest.data_migration}. Back up your data before approving installation.` : "";
  }

  $("updateProgress").hidden = !(busy || total > 0 || state.status === "restart_pending");
  $("updateStage").textContent = state.stage || "Ready";
  $("updatePercent").textContent = total > 0 ? `${percent}%` : busy ? "Working" : "100%";
  $("updateProgressBar").value = total > 0 ? percent : (state.status === "restart_pending" ? 100 : 0);
  $("updateBytes").textContent = total > 0 ? `${formatBytes(done)} / ${formatBytes(total)}` : "Signed metadata and core files";
  $("updateComponent").textContent = state.component || "";
  showError($("updatesError"), state.error ? new Error(state.error) : null);

  $("updateSkip").hidden = !(latest && state.status === "available" && !latest.security_mandatory);
  $("updateInstall").hidden = !(latest && ["available", "skipped", "error"].includes(state.status) && !state.restart_required);
  $("updateInstall").disabled = busy;
  $("updateRestart").hidden = !state.restart_required;
  $("updateRestart").disabled = busy || !state.restart_available;
  $("updateRestart").title = state.restart_available ? "" : "The installed MLAC Studio bootstrap is required";
  $("updateRollback").hidden = !(state.rollback_version && !state.restart_required);
  $("updateRollback").textContent = state.rollback_version ? `Rollback to ${state.rollback_version}` : "Rollback";
  $("updateRollback").disabled = busy;
}

async function updateAction(path, body = {}) {
  try {
    showError($("updatesError"), null);
    ui.updates = await api(path, body);
    renderUpdates();
  } catch (err) {
    showError($("updatesError"), err);
  }
}

async function openUpdates() {
  try { ui.updates = await api("/api/updates/status"); } catch (err) { showError($("updatesError"), err); }
  renderUpdates();
  $("updatesDialog").showModal();
}

function renderHfSource() {
  const source = ui.models?.catalog?.source;
  if (!source) return;
  if (!ui.sourceDraftInitialized) {
    $("hfRepo").value = "";
    $("hfRevision").value = "";
    $("customSourceAck").checked = Boolean(source.responsibility_acknowledged);
    $("hfSourceFiles").replaceChildren(...(source.artifacts || []).map((artifact) => el("div", { class: "source-file" },
      el("label", { for: `hf-source-${artifact.id}` },
        el("b", { text: `${artifact.id} / ${(artifact.roles || [artifact.role]).filter(Boolean).join(", ")}` }),
        el("small", { text: artifact.unverified ? "UNVERIFIED - no SHA-256" : `${formatBytes(artifact.size)} / ${artifact.sha256}` }),
      ),
      el("div", { class: "source-entry", "data-artifact-id": artifact.id },
        el("input", { id: `hf-source-${artifact.id}`, "data-source-url": "", value: artifact.url, autocomplete: "off", placeholder: "Public HTTPS URL or HF repository path" }),
        el("input", { "data-expected-size": "", value: artifact.source_type === "custom" ? artifact.size || "" : "", inputmode: "numeric", autocomplete: "off", placeholder: "Optional bytes (custom)" }),
        el("input", { "data-sha256": "", value: artifact.source_type === "custom" ? artifact.sha256 || "" : "", autocomplete: "off", placeholder: "Optional SHA-256 (custom)" }),
      ),
    )));
    ui.sourceDraftInitialized = true;
  }
  const token = source.token || {};
  $("hfTokenStatus").textContent = !token.backend_available
    ? "Windows Credential Manager unavailable"
    : token.configured ? "Token stored securely" : "No token stored";
  $("hfTokenSet").disabled = !token.backend_available;
  $("hfTokenTest").disabled = !token.configured;
  $("hfTokenRemove").disabled = !token.configured;
  $("hfSourceReset").disabled = !source.override_enabled;
  const preview = ui.sourcePreview;
  const previewUnverified = Boolean(preview?.artifacts?.some((artifact) => artifact.verified === false));
  $("sourceTrustWarning").hidden = !(source.unverified || previewUnverified);
  $("hfSourcePreview").hidden = !preview;
  $("hfSourceConfirm").hidden = !preview;
  if (preview) {
    $("hfSourcePreview").replaceChildren(...preview.artifacts.map((artifact) => el("div", { class: "source-meta" },
      el("b", { text: artifact.id }),
      el("div", {}, el("code", { text: artifact.url }), el("br"), el("span", {
        text: artifact.verified === false
          ? `${artifact.size ? formatBytes(artifact.size) : "Unknown size"} / UNVERIFIED (no SHA-256)`
          : `${formatBytes(artifact.size)} / SHA-256 ${artifact.sha256}`,
      })),
    )));
  }
}

async function resolveHfSource() {
  const files = {};
  for (const row of $("hfSourceFiles").querySelectorAll("[data-artifact-id]")) {
    const url = row.querySelector("[data-source-url]").value.trim();
    if (!url) continue;
    files[row.dataset.artifactId] = {
      url,
      expected_size: row.querySelector("[data-expected-size]").value.trim() || null,
      sha256: row.querySelector("[data-sha256]").value.trim() || null,
    };
  }
  try {
    showError($("modelsError"), null);
    ui.sourcePreview = await api("/api/models/source/resolve", {
      repo_id: $("hfRepo").value.trim(), revision: $("hfRevision").value.trim(), files,
      responsibility_acknowledged: $("customSourceAck").checked,
    });
    renderHfSource();
  } catch (err) {
    showError($("modelsError"), err);
  }
}

async function confirmHfSource() {
  if (!ui.sourcePreview) return;
  await modelAction("/api/models/source/confirm", { confirmation_id: ui.sourcePreview.confirmation_id });
  ui.sourcePreview = null;
  ui.sourceDraftInitialized = false;
  renderModels();
}

async function tokenAction(path, body = {}) {
  try {
    showError($("modelsError"), null);
    const result = await api(path, body);
    if (result.catalog) ui.models = result;
    else $("hfTokenStatus").textContent = `Token accepted for ${result.name}`;
    $("hfToken").value = "";
    renderModels();
  } catch (err) {
    $("hfToken").value = "";
    showError($("modelsError"), err);
  }
}

function renderModels() {
  const state = ui.models || {};
  const catalog = state.catalog || {};
  const transfer = state.transfer || {};
  const legacyReady = Object.values(ui.paths || {}).length > 0 && Object.values(ui.paths).every((item) => item.ok);
  const hasActive = state.available ? Boolean(catalog.active_profile) : legacyReady;
  $("setupBanner").hidden = hasActive;
  $("generate").disabled = !hasActive;
  if (!state.available) {
    $("setupBanner").hidden = legacyReady;
    $("modelCatalog").replaceChildren(el("p", { class: "errors", text: state.error || "Model setup is unavailable." }));
    $("hardwareSummary").textContent = "Release manifest unavailable";
    return;
  }
  const hw = catalog.hardware || {};
  $("hardwareSummary").textContent = hw.gpu_name
    ? `${hw.gpu_name} / ${formatBytes((hw.vram_mib || 0) * 1024 * 1024)} VRAM / driver ${hw.driver_version || "unknown"}`
    : "No supported NVIDIA GPU was detected. Installation remains available, but inference may fail or be very slow.";
  const warnings = catalog.hardware_warnings || [];
  if (warnings.length) $("hardwareSummary").textContent += ` Warning: ${warnings.join(" ")}`;
  const busy = ["queued", "downloading", "verifying", "configuring", "cancelling"].includes(transfer.status);
  const activeProfile = (catalog.profiles || []).find((profile) => profile.active);
  $("modelTrustWarning").hidden = !catalog.active_model?.unverified;
  if (activeProfile) {
    $("modelPath").textContent = `${activeProfile.name} / ${activeProfile.model_version}`;
    $("modelBad").hidden = true;
  }
  const cards = (catalog.profiles || []).map((profile) => {
    const badges = el("div", { class: "profile-badges" },
      profile.active ? el("span", { class: "chip active", text: "Active" }) : null,
      profile.installed ? el("span", { class: "chip", text: "Installed" }) : null,
      profile.unverified ? el("span", { class: "chip warning", text: "UNVERIFIED" }) : null,
      catalog.recommended_profile === profile.id ? el("span", { class: "chip recommended", text: "Recommended" }) : null,
      !profile.compatible ? el("span", { class: "chip warning", text: "Not compatible" }) : null,
    );
    const licenses = el("div", { class: "license-list" }, ...(profile.licenses || []).map((license) => {
      const input = el("input", {
        type: "checkbox", class: "license-check", "data-id": license.id,
        "data-version": license.version, "data-model": license.model,
      });
      input.checked = Boolean(license.accepted);
      input.disabled = Boolean(license.accepted);
      return el("label", { class: "license-row" }, input, el("span", {},
        el("b", { text: `${license.name} (${license.version})` }),
        el("small", { text: license.text }),
      ));
    }));
    const actions = el("div", { class: "profile-actions" });
    if (!profile.installed) {
      actions.append(el("button", {
        class: "primary", type: "button", text: profile.download_bytes == null ? "Install (size unknown)" : `Install ${formatBytes(profile.download_bytes)}`,
        disabled: busy,
        onclick: async () => {
          const accepted = [...licenses.querySelectorAll(".license-check:checked")].map((node) => ({
            id: node.dataset.id, version: node.dataset.version, model: node.dataset.model,
          }));
          await modelAction("/api/models/install", { profile_id: profile.id, accepted_licenses: accepted, activate: !hasActive });
        },
      }));
    } else if (!profile.active) {
      actions.append(
        el("button", { class: "primary", type: "button", text: "Switch", disabled: busy, onclick: () => modelAction("/api/models/switch", { profile_id: profile.id }) }),
        el("button", { class: "btn danger-text", type: "button", text: "Delete", disabled: busy, onclick: () => deleteProfile(profile) }),
      );
    }
    return el("article", { class: `profile-card${profile.active ? " selected" : ""}` },
      el("div", { class: "profile-title" }, el("div", {}, el("h4", { text: profile.name }), el("code", { text: profile.model_version })), badges),
      el("p", { text: profile.description }), licenses, actions,
    );
  });
  $("modelCatalog").replaceChildren(...cards);
  renderHfSource();
  const visible = transfer.status && transfer.status !== "idle";
  $("downloadPanel").hidden = !visible;
  if (visible) {
    $("downloadTrustWarning").hidden = !transfer.unverified;
    $("downloadTitle").textContent = `${transfer.profile_id || "Model"} / ${transfer.stage || transfer.status}`;
    $("downloadPercent").textContent = transfer.indeterminate ? "indeterminate" : `${Number(transfer.percent || 0).toFixed(1)}%`;
    if (transfer.indeterminate) $("downloadProgress").removeAttribute("value");
    else $("downloadProgress").value = Number(transfer.percent || 0);
    $("downloadBytes").textContent = transfer.indeterminate
      ? `${formatBytes(transfer.bytes_downloaded)} downloaded / total unknown`
      : `${formatBytes(transfer.bytes_downloaded)} / ${formatBytes(transfer.bytes_total)}`;
    $("downloadSpeed").textContent = transfer.speed_bps ? `${formatBytes(transfer.speed_bps)}/s` : "-";
    $("downloadEta").textContent = Number.isFinite(transfer.eta_seconds) ? `ETA ${Math.ceil(transfer.eta_seconds)}s` : "ETA -";
    $("downloadFiles").replaceChildren(...(transfer.files || []).map((file) => el("div", { class: "download-file" },
      el("span", { text: `${file.name}${file.unverified ? " / UNVERIFIED" : ""}` }), el("span", { text: file.error || file.stage }),
      el("progress", file.indeterminate ? { max: 100 } : { max: 100, value: file.percent || 0 }),
      el("code", { text: `${formatBytes(file.bytes_downloaded)} / ${file.indeterminate ? "unknown" : formatBytes(file.bytes_total)}${file.speed_bps ? ` / ${formatBytes(file.speed_bps)}/s${Number.isFinite(file.eta_seconds) ? ` / ${Math.ceil(file.eta_seconds)}s` : ""}` : ""}` }),
    )));
    showError($("downloadError"), transfer.error ? new Error(transfer.error) : null);
    $("downloadCancel").hidden = !busy;
    $("downloadRetry").hidden = !["error", "cancelled", "paused"].includes(transfer.status);
  }
}

async function modelAction(path, body) {
  try {
    showError($("modelsError"), null);
    ui.models = await api(path, body);
    renderModels();
  } catch (err) {
    showError($("modelsError"), err);
  }
}

async function deleteProfile(profile) {
  const confirmation = await ask({
    title: `Delete ${profile.name}?`,
    message: `Type ${profile.id} to remove files used only by this profile. Shared and active model files are retained.`,
    value: "", ok: "Delete model",
  });
  if (confirmation) await modelAction("/api/models/delete", { profile_id: profile.id, confirmation });
}

function openModels() {
  $("idleTimeout").value = ui.idleTimeout;
  showError($("modelsError"), null);
  renderModels();
  $("modelsDialog").showModal();
}

function applyPreset() {
  update();
  saveSoon();
}

// ── wiring ──────────────────────────────────────────────────────────────
function wire() {
  for (const id of FIELDS) $(id).addEventListener("input", () => { update(); saveSoon(); });
  $("prompt").addEventListener("input", () => { mention.at = 0; renderMentions(); });
  $("prompt").addEventListener("keydown", onMentionKey);
  $("prompt").addEventListener("click", renderMentions);
  $("prompt").addEventListener("blur", () => { $("mentions").hidden = true; });
  $("preset").addEventListener("change", applyPreset);
  $("dice").addEventListener("click", () => { $("seed").value = Math.floor(Math.random() * 2 ** 31); update(); saveSoon(); });
  $("generate").addEventListener("click", async () => {
    showError($("formError"), null);
    try {
      await saveMaskNow();
      await api("/api/render", { session: ui.session, ...settings() });
    } catch (err) {
      showError($("formError"), err);
    }
  });
  $("cancel").addEventListener("click", () => ui.live && api("/api/cancel", { id: ui.live.id }));

  $("fileInput").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
  const drop = $("drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("drag"); uploadFiles([...e.dataTransfer.files]); });

  maskState.canvas = $("maskCanvas");
  maskState.ctx = maskState.canvas.getContext("2d", { willReadFrequently: true });
  $("maskBrush").addEventListener("click", () => setMaskTool("brush"));
  $("maskErase").addEventListener("click", () => setMaskTool("erase"));
  $("maskSize").addEventListener("input", () => {
    $("maskSizeValue").textContent = `${$("maskSize").value} px`;
  });
  $("maskFeather").addEventListener("input", () => {
    $("maskFeatherValue").textContent = `${$("maskFeather").value}%`;
    if (ui.maskInfo) maskChanged();
  });
  maskState.canvas.addEventListener("pointerdown", (e) => {
    if (!ui.inputs.length) return;
    e.preventDefault();
    maskState.canvas.setPointerCapture(e.pointerId);
    rememberMask();
    maskState.drawing = true;
    maskState.last = maskPoint(e);
    stampMask(maskState.last.x, maskState.last.y);
  });
  maskState.canvas.addEventListener("pointermove", (e) => {
    if (!maskState.drawing) return;
    e.preventDefault();
    const point = maskPoint(e);
    drawMaskSegment(maskState.last, point);
    maskState.last = point;
  });
  const finishStroke = (e) => {
    if (!maskState.drawing) return;
    maskState.drawing = false;
    maskState.last = null;
    if (e?.pointerId !== undefined && maskState.canvas.hasPointerCapture(e.pointerId)) {
      maskState.canvas.releasePointerCapture(e.pointerId);
    }
    maskChanged();
  };
  maskState.canvas.addEventListener("pointerup", finishStroke);
  maskState.canvas.addEventListener("pointercancel", finishStroke);
  $("maskUndo").addEventListener("click", () => {
    if (!maskState.undo.length) return;
    maskState.redo.push(maskSnapshot());
    restoreMask(maskState.undo.pop());
    updateMaskButtons();
  });
  $("maskRedo").addEventListener("click", () => {
    if (!maskState.redo.length) return;
    maskState.undo.push(maskSnapshot());
    restoreMask(maskState.redo.pop());
    updateMaskButtons();
  });
  $("maskClear").addEventListener("click", () => {
    rememberMask();
    maskState.ctx.clearRect(0, 0, maskState.canvas.width, maskState.canvas.height);
    maskChanged();
  });
  $("maskInvert").addEventListener("click", () => {
    rememberMask();
    const pixels = maskState.ctx.getImageData(0, 0, maskState.canvas.width, maskState.canvas.height);
    for (let i = 0; i < pixels.data.length; i += 4) {
      pixels.data[i] = 220;
      pixels.data[i + 1] = 28;
      pixels.data[i + 2] = 28;
      pixels.data[i + 3] = 255 - pixels.data[i + 3];
    }
    maskState.ctx.putImageData(pixels, 0, 0);
    maskChanged();
  });

  const current = () => ui.takes.find((t) => t.id === ui.selected);
  $("useTake").addEventListener("click", () => current() && useTake(current()));
  $("reuseTake").addEventListener("click", () => current() && reuseTake(current()));

  $("sessionSelect").addEventListener("change", (e) => openSession(e.target.value));
  $("sessionNew").addEventListener("click", async () => {
    const name = await ask({ title: "New session", value: `session-${$("sessionSelect").options.length + 1}`, ok: "Create" });
    if (name) openSession(name, {});
  });
  $("sessionDup").addEventListener("click", async () => {
    const name = await ask({ title: "Duplicate session", message: "Settings and references are copied; takes stay with the original.", value: `${ui.session}-copy`, ok: "Duplicate" });
    if (!name) return;
    try {
      await saveMaskNow();
      const res = await api("/api/session/duplicate", { session: ui.session, new_name: name });
      openSession(res.name);
    } catch (err) {
      termLine(`[studio] ${err.message}`, "e");
    }
  });
  $("sessionDel").addEventListener("click", async () => {
    const yes = await ask({ title: `Delete ${ui.session}?`, message: "Its settings, reference, and local takes will be deleted.", ok: "Delete" });
    if (!yes) return;
    await saveMaskNow();
    const res = await api("/api/session/delete", { session: ui.session });
    openSession(res.name);
  });

  $("modelsBtn").addEventListener("click", openModels);
  $("setupModelsBtn").addEventListener("click", openModels);
  $("modelsClose").addEventListener("click", () => $("modelsDialog").close());
  $("modelsDone").addEventListener("click", () => $("modelsDialog").close());
  $("downloadCancel").addEventListener("click", () => modelAction("/api/models/cancel", {}));
  $("downloadRetry").addEventListener("click", () => modelAction("/api/models/retry", {}));
  $("hfSourceResolve").addEventListener("click", resolveHfSource);
  $("hfSourceConfirm").addEventListener("click", confirmHfSource);
  $("hfSourceReset").addEventListener("click", async () => {
    await modelAction("/api/models/source/reset", {});
    ui.sourcePreview = null;
    ui.sourceDraftInitialized = false;
    renderModels();
  });
  $("hfTokenSet").addEventListener("click", () => tokenAction("/api/models/hf-token/set", { token: $("hfToken").value }));
  $("hfTokenTest").addEventListener("click", () => tokenAction("/api/models/hf-token/test"));
  $("hfTokenRemove").addEventListener("click", () => tokenAction("/api/models/hf-token/remove"));
  $("runtimeSave").addEventListener("click", async () => {
    try {
      const runtime = await api("/api/runtime", { idle_timeout: $("idleTimeout").value });
      ui.idleTimeout = runtime.idle_timeout;
      ui.model = runtime.model;
      renderModelStatus();
      showError($("modelsError"), null);
    } catch (err) {
      showError($("modelsError"), err);
    }
  });

  $("updatesBtn").addEventListener("click", openUpdates);
  $("updatesClose").addEventListener("click", () => $("updatesDialog").close());
  $("updateCheck").addEventListener("click", () => updateAction("/api/updates/check"));
  $("updateChannel").addEventListener("change", (event) => updateAction("/api/updates/channel", { channel: event.target.value }));
  $("updateSkip").addEventListener("click", () => updateAction("/api/updates/skip", { version: ui.updates?.latest?.version }));
  $("updateInstall").addEventListener("click", async () => {
    const migration = ui.updates?.latest?.data_migration;
    if (migration) {
      const approved = await ask({
        title: "Approve data migration?",
        message: `Back up MLAC Studio data first. The signed release requests: ${migration}`,
        ok: "Approve and install",
      });
      if (!approved) return;
    }
    await updateAction("/api/updates/install", { allow_data_migration: Boolean(migration) });
  });
  $("updateRestart").addEventListener("click", () => updateAction("/api/updates/restart"));
  $("updateRollback").addEventListener("click", async () => {
    const approved = await ask({
      title: `Rollback to ${ui.updates?.rollback_version}?`,
      message: "The previous signed core version will become active after a restart. Models and runtime files are unchanged.",
      ok: "Prepare rollback",
    });
    if (approved) await updateAction("/api/updates/rollback");
  });

  $("termTabs").addEventListener("click", (e) => {
    const tab = e.target.closest("button[data-tab]");
    if (!tab) return;
    for (const b of $("termTabs").children) b.setAttribute("aria-selected", String(b === tab));
    $("term").hidden = tab.dataset.tab !== "output";
    $("renderLog").hidden = tab.dataset.tab !== "render";
    $("termClear").hidden = tab.dataset.tab !== "output";
  });
  $("termClear").addEventListener("click", () => $("term").replaceChildren());
  $("logout").addEventListener("click", async () => {
    await saveMaskNow();
    await api("/api/logout", {});
    location.href = "/login";
  });
}

async function start() {
  const cfg = await api("/api/config");
  csrfToken = cfg.csrf;
  wire();
  renderPaths(cfg.paths);
  ui.model = cfg.model;
  ui.capabilities = cfg.capabilities || cfg.model?.capabilities || ui.capabilities;
  ui.referenceRoles = cfg.reference_roles || ui.referenceRoles;
  ui.models = cfg.models;
  ui.updates = cfg.updates;
  ui.idleTimeout = cfg.idle_timeout;
  renderModelStatus();
  renderModels();
  renderUpdates();
  if (cfg.models?.available && !cfg.models.catalog?.active_profile) openModels();
  ui.session = cfg.active;
  renderSessions(cfg.sessions.includes(cfg.active) ? cfg.sessions : [cfg.active, ...cfg.sessions]);
  applySettings(cfg.settings);
  await Promise.all([loadInputs(), loadTakes()]);
  connectEvents();
}

start().catch((err) => {
  $("lamp").className = "dot off";
  $("lampText").textContent = err.message;
});

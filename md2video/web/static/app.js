// md2video studio — frontend logic
const $ = (sel, root = document) => root.querySelector(sel);

const STAGE_LABEL = {
  queued: "Queued…",
  narrate: "Distilling storyboard",
  storyboard: "Ready to edit",
  render: "Rendering slides",
  tts: "Synthesizing voice",
  assemble: "Assembling video",
  done: "Done",
};

const ANIM_OPTIONS = {
  title: ["fade"],
  points: ["sequential", "together", "spotlight"],
  statement: ["spotlight", "together"],
  diagram: ["walkthrough", "whole"],
  table: ["together", "cards_sequential"],
  code: ["together", "lines"],
};
const PRESET_OPTIONS = [["dark_keynote", "Dark keynote"],
  ["editorial_light", "Editorial light"], ["minimal_statement", "Minimal"]];

const state = {
  selectedFile: null,
  selectedId: null,
  active: new Set(),   // ids currently building/queued (being polled)
  poller: null,
  languages: [],
  editor: { id: null, sb: null, dirty: false },
};

/* ----------------------------------------------------------------- */
/* Theme                                                              */
/* ----------------------------------------------------------------- */
function initTheme() {
  const saved = localStorage.getItem("md2video-theme");
  const sys = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = saved || sys;
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("md2video-theme", next);
  });
}

/* ----------------------------------------------------------------- */
/* Helpers                                                           */
/* ----------------------------------------------------------------- */
function fmtDuration(sec) {
  if (!sec && sec !== 0) return "—";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function fmtDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch { return ""; }
}
function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ----------------------------------------------------------------- */
/* Health + voices                                                   */
/* ----------------------------------------------------------------- */
async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    const pill = (ok, label) =>
      `<span class="pill ${ok ? "ok" : "bad"}"><span class="dot"></span>${label}</span>`;
    $("#health").innerHTML =
      pill(h.kokoro, h.kokoro ? "Kokoro" : "Kokoro offline") + pill(h.ffmpeg, "ffmpeg");
  } catch {
    $("#health").innerHTML = "";
  }
}

async function loadLanguages() {
  const sel = $("#languageSelect");
  try {
    const { languages } = await (await fetch("/api/languages")).json();
    state.languages = languages;
    sel.innerHTML = "";
    languages.forEach(l => {
      const opt = document.createElement("option");
      opt.value = l.code;
      opt.textContent = l.native;
      sel.appendChild(opt);
    });
  } catch {
    state.languages = [{ code: "en", native: "English", is_source: true }];
    sel.innerHTML = '<option value="en">English</option>';
  }
  sel.addEventListener("change", onLanguageChange);
}

function onLanguageChange() {
  const code = $("#languageSelect").value;
  const lang = state.languages.find(l => l.code === code);
  const note = $("#langNote");
  if (lang && !lang.is_source) {
    note.hidden = false;
    note.textContent = `Slides and narration are translated to ${lang.native} — needs an LLM (ANTHROPIC_API_KEY).`;
  } else {
    note.hidden = true;
  }
  loadVoices(code);
}

async function previewVoice() {
  const v = $("#voiceSelect").value;
  if (!v) return;
  const audio = $("#voiceAudio"), btn = $("#voicePreview");
  btn.classList.add("playing");
  audio.src = `/api/voices/${encodeURIComponent(v)}/sample?ts=${Date.now()}`;
  try { await audio.play(); } catch { btn.classList.remove("playing"); }
  audio.onended = () => btn.classList.remove("playing");
  audio.onerror = () => btn.classList.remove("playing");
}

async function loadVoices(language) {
  const sel = $("#voiceSelect");
  const qs = language ? "?language=" + encodeURIComponent(language) : "";
  try {
    const { voices } = await (await fetch("/api/voices" + qs)).json();
    sel.innerHTML = "";
    voices.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v.id;
      const tag = [v.accent, v.gender].filter(Boolean).join(" ");
      opt.textContent = tag ? `${v.name} · ${tag}` : v.name;
      sel.appendChild(opt);
    });
  } catch {
    sel.innerHTML = '<option value="af_heart">Heart</option>';
  }
}

/* ----------------------------------------------------------------- */
/* File picking                                                      */
/* ----------------------------------------------------------------- */
function initDropzone() {
  const dz = $("#dropzone"), input = $("#fileInput");
  const setFile = (file) => {
    if (!file) return;
    state.selectedFile = file;
    $(".dz-empty").hidden = true;
    $(".dz-file").hidden = false;
    $(".dz-filename").textContent = file.name;
    if (!$("#titleInput").value.trim())
      $("#titleInput").value = file.name.replace(/\.(md|markdown)$/i, "");
    $("#generateBtn").disabled = false;
  };
  const clear = () => {
    state.selectedFile = null; input.value = "";
    $(".dz-empty").hidden = false; $(".dz-file").hidden = true;
    $("#generateBtn").disabled = true;
  };

  dz.addEventListener("click", (e) => { if (!e.target.closest(".dz-clear")) input.click(); });
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => setFile(input.files[0]));
  $(".dz-clear").addEventListener("click", clear);

  ["dragenter", "dragover"].forEach(ev =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach(ev =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragging"); }));
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f && /\.(md|markdown)$/i.test(f.name)) setFile(f);
  });
}

/* ----------------------------------------------------------------- */
/* Create / generate                                                 */
/* ----------------------------------------------------------------- */
function showProgress(stage, pct) {
  let box = $("#inlineProgress");
  if (!box) {
    box = document.createElement("div");
    box.id = "inlineProgress"; box.className = "progress";
    box.innerHTML = `<div class="progress-head"><span class="progress-stage"></span>
      <span class="progress-pct"></span></div><div class="bar"><i></i></div>`;
    $("#createForm").after(box);
  }
  $(".progress-stage", box).textContent = STAGE_LABEL[stage] || stage || "Working…";
  $(".progress-pct", box).textContent = (pct || 0) + "%";
  $(".bar > i", box).style.width = (pct || 0) + "%";
}
function clearProgress() { $("#inlineProgress")?.remove(); }

function msg(text, kind = "info") {
  const el = $("#formMsg");
  el.hidden = !text; el.textContent = text; el.className = "form-msg " + kind;
}

async function onSubmit(e) {
  e.preventDefault();
  if (!state.selectedFile) return;
  msg("");
  const btn = $("#generateBtn");
  btn.disabled = true; $(".btn-label", btn).textContent = "Starting…";

  const fd = new FormData();
  fd.append("file", state.selectedFile);
  fd.append("title", $("#titleInput").value.trim());
  fd.append("voice", $("#voiceSelect").value);
  fd.append("language", $("#languageSelect").value);

  try {
    const res = await fetch("/api/videos", { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || "Build failed to start");
    const meta = await res.json();
    showProgress("queued", 0);
    state.active.add(meta.id);
    startPolling();
    await loadLibrary();
    // reset form
    $(".btn-label", btn).textContent = "Generate video";
  } catch (err) {
    msg(err.message || String(err), "error");
    btn.disabled = false; $(".btn-label", btn).textContent = "Generate video";
  }
}

/* ----------------------------------------------------------------- */
/* Polling active jobs                                               */
/* ----------------------------------------------------------------- */
function startPolling() {
  if (state.poller) return;
  state.poller = setInterval(pollActive, 1200);
  pollActive();
}
function stopPolling() {
  if (state.poller && state.active.size === 0) {
    clearInterval(state.poller); state.poller = null;
  }
}
async function pollActive() {
  for (const id of [...state.active]) {
    let job;
    try { job = await (await fetch("/api/jobs/" + id)).json(); }
    catch { continue; }
    updateCard(id, job);
    // inline progress tracks the most recent (only one builds at a time)
    if (["processing", "queued", "distilling"].includes(job.status)) {
      if (state.editor.id === id && !$("#editor").hidden)
        showEditorProgress(job.stage || "queued", job.progress);
      else
        showProgress(job.stage || "queued", job.progress);
    }

    if (job.status === "storyboard_ready") {
      state.active.delete(id);
      clearProgress();
      $("#generateBtn").disabled = !state.selectedFile;
      await loadLibrary();
      openEditor(id);
    } else if (job.status === "ready" || job.status === "error") {
      state.active.delete(id);
      clearProgress();
      $("#generateBtn").disabled = !state.selectedFile;
      if (job.status === "error") msg("Build failed: " + (job.error || "unknown error"), "error");
      await loadLibrary();
      if (job.status === "ready") { closeEditor(); selectVideo(id); }
    }
  }
  stopPolling();
}

function updateCard(id, job) {
  const card = $(`.card[data-id="${id}"]`);
  if (!card) return;
  const bar = $(".card-progress > i", card);
  if (bar) bar.style.width = (job.progress || 0) + "%";
  const badge = $(".badge", card);
  if (badge && (job.status === "processing" || job.status === "queued")) {
    badge.innerHTML = `<span class="spin"></span>${STAGE_LABEL[job.stage] || "Building"}`;
  }
}

/* ----------------------------------------------------------------- */
/* Library                                                           */
/* ----------------------------------------------------------------- */
async function loadLibrary() {
  let items = [];
  try { items = await (await fetch("/api/videos")).json(); } catch { /* ignore */ }
  const grid = $("#libraryGrid");
  $("#libCount").textContent = items.length;
  $("#libraryEmpty").style.display = items.length ? "none" : "block";
  grid.innerHTML = "";

  items.forEach(m => {
    const building = ["queued", "processing", "distilling"].includes(m.status);
    if (building) { state.active.add(m.id); }
    const card = document.createElement("div");
    card.className = "card" + (m.id === state.selectedId ? " active" : "");
    card.dataset.id = m.id;

    const thumb = m.status === "ready"
      ? `<img src="/media/${m.id}/poster.jpg?rev=${m.rev || 0}" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'▶'}))" />`
      : `<div class="ph">${m.status === "error" ? "⚠" : (m.status === "storyboard_ready" ? "✎" : "●")}</div>`;

    let badge = "";
    if (m.status === "ready") badge = `<span class="badge ready">Ready</span>`;
    else if (m.status === "error") badge = `<span class="badge error">Failed</span>`;
    else if (m.status === "storyboard_ready") badge = `<span class="badge building">Draft · edit</span>`;
    else badge = `<span class="badge building"><span class="spin"></span>${STAGE_LABEL[m.stage] || "Building"}</span>`;

    card.innerHTML = `
      <div class="card-thumb">
        ${thumb}
        ${m.status === "ready" ? `<div class="play-badge">
          <svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></div>` : ""}
      </div>
      ${building ? `<div class="card-progress"><i style="width:${m.progress || 0}%"></i></div>` : ""}
      <div class="card-body">
        <div class="card-title">${escapeHtml(m.title)}</div>
        <div class="card-foot">
          <span class="card-meta">${badge}${m.status === "ready" ? `<span>${fmtDuration(m.duration)}</span>` : ""}${m.language && m.language !== "en" ? `<span>${m.language_name || m.language}</span>` : ""}</span>
          <span class="card-actions">
            ${m.status === "ready" ? `<button class="edit" title="Edit & re-generate">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20h4l10-10-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg></button>
            <a class="dl" href="/media/${m.id}/download" title="Download" onclick="event.stopPropagation()">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4v10m0 0 4-4m-4 4-4-4M5 19h14"/></svg></a>` : ""}
            <button class="del" title="Delete">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg></button>
          </span>
        </div>
      </div>`;

    card.addEventListener("click", () => {
      if (m.status === "ready") selectVideo(m.id);
      else if (m.status === "storyboard_ready") openEditor(m.id);
    });
    $(".del", card).addEventListener("click", (e) => { e.stopPropagation(); deleteVideo(m.id, m.title); });
    const editBtn = $(".edit", card);
    if (editBtn) editBtn.addEventListener("click", (e) => { e.stopPropagation(); openEditor(m.id); });
    grid.appendChild(card);
  });

  if (state.active.size) startPolling();
}

async function selectVideo(id) {
  let m;
  try { m = await (await fetch("/api/videos/" + id)).json(); } catch { return; }
  if (m.status !== "ready") return;
  state.selectedId = id;
  const p = $("#player");
  p.classList.remove("empty");
  $(".player-loaded", p).hidden = false;
  const vid = $("#videoEl");
  vid.src = `/media/${id}/video.mp4`;
  $("#playerTitle").textContent = m.title;
  $("#playerSub").textContent =
    `${fmtDuration(m.duration)} · ${m.scene_count || "?"} scenes · ${m.language_name || "English"} · ${m.voice} · ${fmtDate(m.created_at)}`;
  $("#downloadBtn").href = `/media/${id}/download`;
  document.querySelectorAll(".card").forEach(c => c.classList.toggle("active", c.dataset.id === id));
  p.scrollIntoView({ behavior: "smooth", block: "start" });
  vid.play().catch(() => {});
}

async function deleteVideo(id, title) {
  if (!confirm(`Delete "${title}"? This can't be undone.`)) return;
  await fetch("/api/videos/" + id, { method: "DELETE" });
  state.active.delete(id);
  if (state.selectedId === id) {
    state.selectedId = null;
    $("#player").classList.add("empty");
    $("#videoEl").src = "";
  }
  loadLibrary();
}

/* ----------------------------------------------------------------- */
/* Storyboard editor (human-in-the-loop)                             */
/* ----------------------------------------------------------------- */
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }
function fillSelect(sel, pairs, value) {
  sel.innerHTML = "";
  pairs.forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = label; sel.appendChild(o);
  });
  if (value) sel.value = value;
}
function markDirty() { state.editor.dirty = true; $("#editorDirty").hidden = false; }

async function openEditor(id) {
  let sb;
  try { sb = await (await fetch(`/api/videos/${id}/storyboard`)).json(); } catch { return; }
  state.editor = { id, sb, dirty: false, undo: [] };
  $("#editorDirty").hidden = true;
  $("#editorProgress").hidden = true;
  $("#edUndo").disabled = true;
  document.querySelector(".layout").hidden = true;
  $("#editor").hidden = false;
  $("#editorName").textContent = (sb.meta && sb.meta.title) || "Storyboard";
  fillSelect($("#edPreset"), PRESET_OPTIONS, sb.style.preset);
  fillSelect($("#edTheme"), [["dark", "Dark"], ["light", "Light"]], sb.style.theme);
  fillSelect($("#toneSelect"), TONE_OPTIONS, (sb.meta && sb.meta.tone) || "conversational");
  $("#toneCustom").hidden = $("#toneSelect").value !== "custom";
  $("#askProposal").hidden = true; $("#askInput").value = "";
  renderEditorSlides();
  window.scrollTo(0, 0);
}

function closeEditor() {
  $("#editor").hidden = true;
  document.querySelector(".layout").hidden = false;
  state.editor = { id: null, sb: null, dirty: false };
}

const DEFAULT_ANIM = { title: "fade", points: "sequential", statement: "spotlight",
  diagram: "whole", table: "together", code: "together", image: "fade" };

function mintId(prefix) { return `${prefix}_${Math.random().toString(16).slice(2, 10)}`; }
function deepClone(o) { return JSON.parse(JSON.stringify(o)); }

function pushUndo() {
  state.editor.undo.push(deepClone(state.editor.sb));
  if (state.editor.undo.length > 30) state.editor.undo.shift();
  $("#edUndo").disabled = false;
}
function undo() {
  if (!state.editor.undo.length) return;
  state.editor.sb = state.editor.undo.pop();
  $("#edUndo").disabled = !state.editor.undo.length;
  markDirty(); renderEditorSlides();
}
// Wrap a structural mutation so it's undoable and re-rendered.
function mutate(fn) { pushUndo(); fn(); markDirty(); renderEditorSlides(); }

function defaultSlide(kind) {
  const s = { id: mintId("sl"), kind, animation: DEFAULT_ANIM[kind] || "fade",
    kicker: "", headline: "", points: [], narration_full: "", source_notes: [] };
  if (kind === "image") { s.headline = "";
    s.image = { prompt: "", negative_prompt: "", seed: null, model: "", steps: 4, path: "" };
    s.narration_full = ""; }
  else if (kind === "title") { s.headline = "New title";
    s.points = [{ id: mintId("p"), text: "", emphasis: [], narration: "" }];
    s.narration_full = "A short introduction."; }
  else if (kind === "statement") {
    s.points = [{ id: mintId("p"), text: "New statement", emphasis: [], narration: "Explain it." }]; }
  else { s.headline = "New section";
    s.points = [{ id: mintId("p"), text: "New point", emphasis: [], narration: "Explain it." }]; }
  return s;
}

function insertRow(index) {
  const row = document.createElement("div");
  row.className = "ed-insert";
  row.innerHTML = `<span class="lbl">+ add slide:</span>
    <button data-k="title">Title</button>
    <button data-k="points">Points</button>
    <button data-k="statement">Statement</button>
    <button data-k="image">Image</button>`;
  row.querySelectorAll("button").forEach(b => b.addEventListener("click", () =>
    mutate(() => state.editor.sb.slides.splice(index, 0, defaultSlide(b.dataset.k)))));
  return row;
}

function renderEditorSlides() {
  const { id, sb } = state.editor;
  const wrap = $("#editorSlides"); wrap.innerHTML = "";
  wrap.appendChild(insertRow(0));
  sb.slides.forEach((s, i) => {
    wrap.appendChild(renderSlideCard(s, i));
    wrap.appendChild(insertRow(i + 1));
  });
}

function renderSlideCard(s, i) {
  const { id, sb } = state.editor;
  const el = document.createElement("div");
  el.className = "ed-slide";
  const anims = ANIM_OPTIONS[s.kind] || ["fade"];
  const animOpts = anims.map(a =>
    `<option value="${a}"${a === s.animation ? " selected" : ""}>${a}</option>`).join("");

  let bodyHtml;
  if (s.kind === "image") {
    const im = s.image || {};
    bodyHtml = `
      <div class="ed-fieldlabel">Image prompt</div>
      <textarea class="ed-narr ed-img-prompt" placeholder="describe the image…">${escapeHtml(im.prompt || "")}</textarea>
      <div class="ed-row" style="gap:8px">
        <button class="ed-addbtn ed-img-gen" type="button">${im.path ? "Regenerate image" : "Generate image"}</button>
        <span class="ed-img-status muted"></span>
      </div>
      <div class="ed-fieldlabel">Narration (spoken)</div>
      <textarea class="ed-narr ed-narr-full" placeholder="narration">${escapeHtml(s.narration_full || "")}</textarea>`;
  } else if (s.kind === "title") {
    const sub = (s.points && s.points[0]) ? s.points[0].text : "";
    bodyHtml = `
      <div class="ed-fieldlabel">Subtitle</div>
      <input class="ed-input ed-subtitle" value="${escapeAttr(sub)}" placeholder="subtitle (optional)"/>
      <div class="ed-fieldlabel">Narration (spoken)</div>
      <textarea class="ed-narr ed-narr-full" placeholder="narration">${escapeHtml(s.narration_full || "")}</textarea>`;
  } else if (s.kind === "table") {
    const cards = ((s.table && s.table.cards) || []).map((c, j) => `
      <div class="ed-card" data-j="${j}">
        <button class="ed-unit-x" title="Remove card">×</button>
        <div class="ed-card-row">
          <input class="ed-input ed-c-label" value="${escapeAttr(c.label || "")}" placeholder="label"/>
          <input class="ed-input ed-c-value" value="${escapeAttr(c.value || "")}" placeholder="value"/>
        </div>
        <textarea class="ed-narr ed-c-narr" placeholder="narration (spoken)">${escapeHtml(c.narration || "")}</textarea>
      </div>`).join("");
    bodyHtml = `<div class="ed-points">${cards}</div><button class="ed-addbtn ed-addcard" type="button">+ Add card</button>`;
  } else if (s.kind === "diagram") {
    const steps = ((s.diagram && s.diagram.steps) || []).map((st, j) => `
      <div class="ed-step" data-j="${j}">
        <div class="ed-step-head"><span class="ed-step-reveal">reveal: ${escapeHtml((st.reveal || []).join(", "))}</span>
          <span class="ed-rowtools">
            <button class="ed-iconbtn ed-step-up" title="Move up">↑</button>
            <button class="ed-iconbtn ed-step-dn" title="Move down">↓</button>
            <button class="ed-iconbtn danger ed-step-x" title="Remove step">✕</button>
          </span></div>
        <textarea class="ed-narr ed-st-narr" placeholder="narration (spoken)">${escapeHtml(st.narration || "")}</textarea>
      </div>`).join("");
    bodyHtml = `${steps ? `<div class="ed-points">${steps}</div>`
      : `<div class="ed-fieldlabel">Narration (spoken)</div>
         <textarea class="ed-narr ed-narr-full" placeholder="narration">${escapeHtml(s.narration_full || "")}</textarea>`}
      <div class="ed-notes">Diagram structure is changed with the prompt below; here you edit each step's narration.</div>`;
  } else if (s.points && s.points.length) {
    const pts = s.points.map((p, j) => `
      <div class="ed-point" data-j="${j}">
        <button class="ed-unit-x" title="Remove point">×</button>
        <input class="ed-input ed-pt-text" value="${escapeAttr(p.text || "")}" placeholder="point (short)"/>
        <textarea class="ed-narr ed-pt-narr" placeholder="narration (spoken)">${escapeHtml(p.narration || "")}</textarea>
      </div>`).join("");
    bodyHtml = `<div class="ed-points">${pts}</div><button class="ed-addbtn ed-addpoint" type="button">+ Add point</button>`;
  } else {
    bodyHtml = `
      <div class="ed-fieldlabel">Narration (spoken)</div>
      <textarea class="ed-narr ed-narr-full" placeholder="narration">${escapeHtml(s.narration_full || "")}</textarea>`;
  }
  const notes = (s.source_notes && s.source_notes.length)
    ? `<div class="ed-notes">↩ ${s.source_notes.map(escapeHtml).join(" · ")}</div>` : "";
  el.innerHTML = `
    <div class="ed-prev"><img src="/api/videos/${id}/preview?slide_id=${s.id}&v=${sb.rev}" alt=""
         onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'stale',textContent:'Save to preview'}))"/></div>
    <div class="ed-fields">
      <div class="ed-row"><span class="ed-kind">${s.kind}</span>
        <select class="ed-input ed-anim">${animOpts}</select>
        <span class="ed-rowtools">
          <button class="ed-iconbtn ed-up" title="Move up"${i === 0 ? " disabled" : ""}>↑</button>
          <button class="ed-iconbtn ed-dn" title="Move down"${i === sb.slides.length - 1 ? " disabled" : ""}>↓</button>
          <button class="ed-iconbtn danger ed-del" title="Delete slide">🗑</button>
        </span></div>
      <input class="ed-input ed-headline ed-h" value="${escapeAttr(s.headline || "")}" placeholder="headline"/>
      ${bodyHtml}
      ${notes}
      <div class="ed-prompt-row">
        <input class="ed-prompt" placeholder="Describe a change to this slide…"/>
        <button class="ed-apply" type="button">Apply</button>
      </div>
    </div>`;

  // --- field wiring (in-memory edits, marked dirty) ---
  $(".ed-anim", el).addEventListener("change", e => { s.animation = e.target.value; markDirty(); });
  $(".ed-h", el).addEventListener("input", e => { s.headline = e.target.value; markDirty(); });
  const sub = $(".ed-subtitle", el);
  if (sub) sub.addEventListener("input", e => {
    if (!s.points || !s.points.length) s.points = [{ id: mintId("p"), text: "", emphasis: [], narration: "" }];
    s.points[0].text = e.target.value; markDirty();
  });
  el.querySelectorAll(".ed-point").forEach(pe => {
    const j = +pe.dataset.j;
    $(".ed-pt-text", pe).addEventListener("input", e => { s.points[j].text = e.target.value; markDirty(); });
    $(".ed-pt-narr", pe).addEventListener("input", e => { s.points[j].narration = e.target.value; markDirty(); });
    $(".ed-unit-x", pe).addEventListener("click", () => mutate(() => s.points.splice(j, 1)));
  });
  el.querySelectorAll(".ed-card").forEach(ce => {
    const j = +ce.dataset.j;
    $(".ed-c-label", ce).addEventListener("input", e => { s.table.cards[j].label = e.target.value; markDirty(); });
    $(".ed-c-value", ce).addEventListener("input", e => { s.table.cards[j].value = e.target.value; markDirty(); });
    $(".ed-c-narr", ce).addEventListener("input", e => { s.table.cards[j].narration = e.target.value; markDirty(); });
    $(".ed-unit-x", ce).addEventListener("click", () => mutate(() => s.table.cards.splice(j, 1)));
  });
  el.querySelectorAll(".ed-step").forEach(se => {
    const j = +se.dataset.j;
    $(".ed-st-narr", se).addEventListener("input", e => { s.diagram.steps[j].narration = e.target.value; markDirty(); });
    $(".ed-step-x", se).addEventListener("click", () => mutate(() => s.diagram.steps.splice(j, 1)));
    $(".ed-step-up", se).addEventListener("click", () => { if (j > 0) mutate(() => s.diagram.steps.splice(j - 1, 0, s.diagram.steps.splice(j, 1)[0])); });
    $(".ed-step-dn", se).addEventListener("click", () => { if (j < s.diagram.steps.length - 1) mutate(() => s.diagram.steps.splice(j + 1, 0, s.diagram.steps.splice(j, 1)[0])); });
  });
  const nf = $(".ed-narr-full", el);
  if (nf) nf.addEventListener("input", e => { s.narration_full = e.target.value; markDirty(); });
  const imgPrompt = $(".ed-img-prompt", el);
  if (imgPrompt) imgPrompt.addEventListener("input", e => {
    s.image = s.image || {}; s.image.prompt = e.target.value; markDirty();
  });
  const imgGen = $(".ed-img-gen", el);
  if (imgGen) imgGen.addEventListener("click", () => generateImage(i, imgGen));
  const addP = $(".ed-addpoint", el);
  if (addP) addP.addEventListener("click", () => mutate(() => s.points.push({ id: mintId("p"), text: "", emphasis: [], narration: "" })));
  const addC = $(".ed-addcard", el);
  if (addC) addC.addEventListener("click", () => mutate(() => {
    s.table = s.table || { layout: "cards", cards: [] };
    s.table.cards.push({ id: mintId("c"), label: "", value: "", narration: "" });
  }));

  // --- slide-level controls ---
  $(".ed-up", el).addEventListener("click", () => { if (i > 0) mutate(() => sb.slides.splice(i - 1, 0, sb.slides.splice(i, 1)[0])); });
  $(".ed-dn", el).addEventListener("click", () => { if (i < sb.slides.length - 1) mutate(() => sb.slides.splice(i + 1, 0, sb.slides.splice(i, 1)[0])); });
  $(".ed-del", el).addEventListener("click", () => {
    if (sb.slides.length <= 1) { alert("A storyboard needs at least one slide."); return; }
    if (!confirm("Delete this slide? (Undo is available.)")) return;
    mutate(() => sb.slides.splice(i, 1));
  });
  const applyBtn = $(".ed-apply", el);
  applyBtn.addEventListener("click", () => {
    const txt = $(".ed-prompt", el).value.trim();
    if (txt) applyPrompt(i, txt, applyBtn);
  });
  return el;
}

async function applyPrompt(index, promptText, btn) {
  const { id, sb } = state.editor;
  pushUndo();                                        // NL edits are undoable too
  if (state.editor.dirty) await saveStoryboard();   // persist manual edits first
  btn.disabled = true; btn.textContent = "…";
  try {
    const res = await fetch(`/api/videos/${id}/slides/${sb.slides[index].id}/revise`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: promptText }) });
    if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).detail) || "failed");
    const data = await res.json();
    sb.slides[index] = data.slide; sb.rev = data.rev;
    renderEditorSlides();   // rebuilds (refreshes preview against new rev)
  } catch (e) {
    alert("Couldn't apply that edit: " + (e.message || e));
    btn.disabled = false; btn.textContent = "Apply";
  }
}

const TONE_OPTIONS = [["conversational", "Conversational"], ["formal", "Formal"],
  ["energetic", "Energetic"], ["plain", "Plain & simple"], ["authoritative", "Authoritative"],
  ["custom", "Custom…"]];

async function ensureSaved() { if (state.editor.dirty) await saveStoryboard(); }

function loadSbIntoEditor(sb) {
  state.editor.sb = sb;
  state.editor.dirty = false;
  $("#editorDirty").hidden = true;
  renderEditorSlides();
}

function opLabel(o) {
  if (o.op === "add_slide") return `Add ${o.kind || "points"} slide${o.headline ? ` “${o.headline}”` : ""}`;
  if (o.op === "delete_slide") return `Delete a slide`;
  if (o.op === "reorder") return `Reorder slides`;
  if (o.op === "edit_slide") return `Edit a slide: ${o.instruction || ""}`;
  if (o.op === "retone") return `Re-tone the script (${o.tone || ""})`;
  if (o.op === "set_style") return `Set style ${o.preset || ""} ${o.theme || ""}`.trim();
  return o.op;
}

async function askPropose() {
  const q = $("#askInput").value.trim();
  if (!q) return;
  await ensureSaved();
  const btn = $("#askBtn"); btn.disabled = true; btn.textContent = "…";
  try {
    const data = await (await fetch(`/api/videos/${state.editor.id}/ask`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: q }) })).json();
    showProposal(data);
  } catch { alert("Ask failed."); }
  btn.disabled = false; btn.textContent = "Ask";
}

function showProposal(data) {
  const box = $("#askProposal");
  box.hidden = false;
  if (!data.ops || !data.ops.length) {
    box.innerHTML = `<div class="muted">${escapeHtml(data.summary || "No changes proposed.")}</div>`;
    return;
  }
  const destructive = data.ops.filter(o => o.op === "delete_slide" || o.op === "reorder").length;
  box.innerHTML = `<div class="ask-summary">${escapeHtml(data.summary || "Proposed changes")}</div>
    <ul class="ask-ops">${data.ops.map(o => `<li>${escapeHtml(opLabel(o))}</li>`).join("")}</ul>
    <div class="ask-actions">
      <button id="askApply" class="btn-primary" type="button">Apply ${data.ops.length} change${data.ops.length > 1 ? "s" : ""}</button>
      <button id="askCancel" class="btn-ghost" type="button">Cancel</button></div>`;
  $("#askApply").addEventListener("click", () => applyOps(data.ops, destructive));
  $("#askCancel").addEventListener("click", () => { box.hidden = true; });
}

async function applyOps(ops, destructive) {
  if (destructive > 1 && !confirm("This deletes/reorders multiple slides. Continue? (Undo is available.)")) return;
  pushUndo();
  try {
    const data = await (await fetch(`/api/videos/${state.editor.id}/ask/apply`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops }) })).json();
    loadSbIntoEditor(data.storyboard);
    $("#askProposal").hidden = true; $("#askInput").value = "";
  } catch { alert("Apply failed."); }
}

async function applyTone() {
  const sel = $("#toneSelect").value;
  const tone = sel === "custom" ? $("#toneCustom").value.trim() : sel;
  if (!tone) return;
  await ensureSaved();
  pushUndo();
  const btn = $("#toneBtn"); btn.disabled = true; btn.textContent = "…";
  try {
    const data = await (await fetch(`/api/videos/${state.editor.id}/retone`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tone }) })).json();
    loadSbIntoEditor(data.storyboard);
  } catch { alert("Retone failed."); }
  btn.disabled = false; btn.textContent = "Apply to script";
}

async function generateImage(index, btn) {
  const s = state.editor.sb.slides[index];
  const prompt = ((s.image && s.image.prompt) || "").trim();
  if (!prompt) { alert("Enter an image prompt first."); return; }
  await ensureSaved();                                 // persist the slide + prompt
  const statusEl = btn.parentElement.querySelector(".ed-img-status");
  btn.disabled = true;
  statusEl.textContent = "generating… (first run downloads the model — can take a few minutes)";
  try {
    await fetch(`/api/videos/${state.editor.id}/slides/${s.id}/image`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }) });
    for (let k = 0; k < 300; k++) {                    // poll up to ~10 min
      await new Promise(r => setTimeout(r, 2000));
      const st = await (await fetch(`/api/videos/${state.editor.id}/slides/${s.id}/image/status`)).json();
      if (st.status === "ready") {
        const sb = await (await fetch(`/api/videos/${state.editor.id}/storyboard`)).json();
        loadSbIntoEditor(sb); return;
      }
      if (st.status === "error") { statusEl.textContent = "error: " + (st.error || ""); btn.disabled = false; return; }
    }
    statusEl.textContent = "timed out"; btn.disabled = false;
  } catch { statusEl.textContent = "failed"; btn.disabled = false; }
}

async function saveStoryboard() {
  const { id, sb } = state.editor;
  const res = await fetch(`/api/videos/${id}/storyboard`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sb),
  });
  const out = await res.json();
  sb.rev = out.rev;
  state.editor.dirty = false; $("#editorDirty").hidden = true;
  renderEditorSlides();    // refresh previews against the new rev
}

function showEditorProgress(stage, pct) {
  const box = $("#editorProgress"); box.hidden = false;
  box.innerHTML = `<div class="progress-head"><span class="progress-stage">${STAGE_LABEL[stage] || stage}</span>
    <span class="progress-pct">${pct || 0}%</span></div><div class="bar"><i style="width:${pct || 0}%"></i></div>`;
}

async function generateFromEditor() {
  if (state.editor.dirty) await saveStoryboard();
  const id = state.editor.id;
  await fetch(`/api/videos/${id}/generate`, { method: "POST" });
  showEditorProgress("queued", 0);
  state.active.add(id); startPolling();
}

/* ----------------------------------------------------------------- */
/* Init                                                              */
/* ----------------------------------------------------------------- */
function init() {
  initTheme();
  initDropzone();
  $("#createForm").addEventListener("submit", onSubmit);
  $("#voicePreview").addEventListener("click", previewVoice);
  $("#editorClose").addEventListener("click", () => {
    if (state.editor.dirty && !confirm("Discard unsaved changes?")) return;
    closeEditor();
  });
  $("#edUndo").addEventListener("click", undo);
  $("#edSave").addEventListener("click", saveStoryboard);
  $("#edGenerate").addEventListener("click", generateFromEditor);
  $("#askBtn").addEventListener("click", askPropose);
  $("#toneBtn").addEventListener("click", applyTone);
  $("#toneSelect").addEventListener("change", e => { $("#toneCustom").hidden = e.target.value !== "custom"; });
  $("#edPreset").addEventListener("change", async e => {
    state.editor.sb.style.preset = e.target.value; await saveStoryboard();
  });
  $("#edTheme").addEventListener("change", async e => {
    state.editor.sb.style.theme = e.target.value; await saveStoryboard();
  });
  loadHealth();
  loadLanguages().then(() => loadVoices($("#languageSelect").value));
  loadLibrary();
}
init();

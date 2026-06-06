// md2video studio — frontend logic
const $ = (sel, root = document) => root.querySelector(sel);

const STAGE_LABEL = {
  queued: "Queued…",
  narrate: "Writing narration",
  render: "Rendering slides",
  tts: "Synthesizing voice",
  assemble: "Assembling video",
  done: "Done",
};

const state = {
  selectedFile: null,
  selectedId: null,
  active: new Set(),   // ids currently building/queued (being polled)
  poller: null,
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

async function loadVoices() {
  const sel = $("#voiceSelect");
  try {
    const { voices } = await (await fetch("/api/voices")).json();
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
    if (job.status === "processing" || job.status === "queued")
      showProgress(job.stage || "queued", job.progress);

    if (job.status === "ready" || job.status === "error") {
      state.active.delete(id);
      clearProgress();
      $("#generateBtn").disabled = !state.selectedFile;
      if (job.status === "error") msg("Build failed: " + (job.error || "unknown error"), "error");
      await loadLibrary();
      if (job.status === "ready") selectVideo(id);
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
    const building = m.status === "queued" || m.status === "processing";
    if (building) { state.active.add(m.id); }
    const card = document.createElement("div");
    card.className = "card" + (m.id === state.selectedId ? " active" : "");
    card.dataset.id = m.id;

    const thumb = m.status === "ready"
      ? `<img src="/media/${m.id}/poster.jpg" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'▶'}))" />`
      : `<div class="ph">${m.status === "error" ? "⚠" : "●"}</div>`;

    let badge = "";
    if (m.status === "ready") badge = `<span class="badge ready">Ready</span>`;
    else if (m.status === "error") badge = `<span class="badge error">Failed</span>`;
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
          <span class="card-meta">${badge}${m.status === "ready" ? `<span>${fmtDuration(m.duration)}</span>` : ""}</span>
          <span class="card-actions">
            ${m.status === "ready" ? `<a class="dl" href="/media/${m.id}/download" title="Download" onclick="event.stopPropagation()">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4v10m0 0 4-4m-4 4-4-4M5 19h14"/></svg></a>` : ""}
            <button class="del" title="Delete">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg></button>
          </span>
        </div>
      </div>`;

    card.addEventListener("click", () => { if (m.status === "ready") selectVideo(m.id); });
    $(".del", card).addEventListener("click", (e) => { e.stopPropagation(); deleteVideo(m.id, m.title); });
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
    `${fmtDuration(m.duration)} · ${m.scene_count || "?"} scenes · ${m.voice} · ${fmtDate(m.created_at)}`;
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
/* Init                                                              */
/* ----------------------------------------------------------------- */
function init() {
  initTheme();
  initDropzone();
  $("#createForm").addEventListener("submit", onSubmit);
  loadHealth();
  loadVoices();
  loadLibrary();
}
init();

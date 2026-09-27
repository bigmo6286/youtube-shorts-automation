/* Shorts console front-end: plain JS, no build step. */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { runs: [], currentRun: null, job: null, jobOffset: 0, settings: null, blueprintRun: null };

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.classList.toggle("error", isError); t.classList.remove("hidden");
  clearTimeout(t._timer);
  if (isError) { openDrawer(); t.onclick = () => t.classList.add("hidden"); }   // errors stay until dismissed
  else t._timer = setTimeout(() => t.classList.add("hidden"), 4000);
}
function openDrawer() { $("#drawer").classList.remove("collapsed"); $("#drawertoggle").textContent = "▼ Activity log"; }
function appendLog(lines) {
  const pre = $("#joblog");
  if (pre.dataset.empty !== "0") { pre.textContent = ""; pre.dataset.empty = "0"; }
  for (const line of lines) {
    const span = document.createElement("span");
    if (/^(ERROR|Traceback|\s+File )|Error:/.test(line)) span.className = "err";
    span.textContent = line + "\n"; pre.appendChild(span);
  }
  pre.scrollTop = 1e9;
}
const fmt = (n) => n == null ? "-" : Number(n).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- tabs
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "settings") { loadSettings(); loadMusic(); }
  if (b.dataset.tab === "studio") { loadOutputs(); loadMusic(); }
}));

// ---------------------------------------------------------------- overview
async function loadStatus() {
  const s = await api("/api/status");
  const card = (label, ok, text) => `<div class="card ${ok ? "ok" : "warn"}"><div class="label">${label}</div><div class="value">${text}</div></div>`;
  $("#statuscards").innerHTML = [
    card("ffmpeg", !!s.ffmpeg, s.ffmpeg ? "ready" : `missing <button id="installffmpeg">Install ffmpeg</button>`),
    card("TypeSafe", s.typesafe, s.typesafe ? "connected" : "key missing"),
    card("Script writer", !!s.script_backend, s.script_backend === "api" ? "Anthropic API" : s.script_backend === "claude_code" ? "Claude subscription" : "not configured"),
    card("Backgrounds", s.pexels, s.pexels ? "Pexels footage" : "generated gradient"),
    card("Music", s.music_tracks > 0, s.music_tracks > 0 ? `${s.music_tracks} track${s.music_tracks === 1 ? "" : "s"}` : "no tracks (Settings)"),
    card("Telegram", s.telegram, s.telegram ? "delivery on" : "not connected"),
    card("YouTube upload", s.youtube_upload, s.youtube_upload ? "ready" : "client secrets missing"),
    card("Latest run", !!s.latest_run, s.latest_run ? `${s.latest_run.shorts} Shorts, ${s.latest_run.blueprints} blueprints` : "none yet"),
    card("Produced", s.outputs > 0, `${s.outputs} Shorts`),
  ].join("");
  const inst = $("#installffmpeg");
  if (inst) inst.addEventListener("click", () => startJob("setup_ffmpeg"));
  if (s.job_running && (!state.job || state.job.id !== s.job_running.id)) attachJob(s.job_running.id);
}

async function loadRuns() {
  state.runs = await api("/api/runs");
  const tb = $("#runs tbody");
  tb.innerHTML = state.runs.map((r) => `<tr class="${r.id === state.currentRun ? "sel" : ""}">
      <td>${r.id}</td><td>${r.shorts}</td><td>${r.judged}</td><td>${r.blueprints}</td>
      <td><button data-open="${r.id}">open</button></td></tr>`).join("") || `<tr><td colspan="5" class="muted">No runs yet.</td></tr>`;
  $$("button[data-open]", tb).forEach((b) => b.addEventListener("click", () => openRun(b.dataset.open)));
  // Rebuild the Studio run picker only when its options changed, and keep whatever the user picked:
  // background refreshes (a job finishing, opening a run) must not reset the blueprint choice.
  const sel = $("#prod-run");
  const previousRun = sel.value;
  const html = state.runs.filter((r) => r.blueprints > 0).map((r) => `<option value="${r.id}">${r.id} (${r.blueprints} blueprints)</option>`).join("")
    || `<option value="">no analysed run yet</option>`;
  if (sel.innerHTML !== html) {
    sel.innerHTML = html;
    if (previousRun && [...sel.options].some((o) => o.value === previousRun)) sel.value = previousRun;
  }
  sel.onchange = () => fillBlueprints(sel.value, true);
  if (state.runs.length && !state.currentRun) openRun(state.runs[0].id);
  else if (sel.value && (sel.value !== state.blueprintRun || !$("#prod-blueprint").options.length)) fillBlueprints(sel.value);
}

async function openRun(id) {
  state.currentRun = id;
  const r = await api(`/api/runs/${id}`);
  $("#report").textContent = r.report || "(no report: run Analyze)";
  $("#bp-run").textContent = `From run ${id}`;
  const bps = (r.analysis && r.analysis.blueprints) || [];
  $("#blueprints").innerHTML = bps.map((b, i) => `<div class="bp ${b.stretch ? "stretch" : ""}">
      <div class="bphead"><span class="num">${i + 1}</span><b>${esc(b.format)}</b> × ${esc(b.topic)}
        <span class="tag">${esc(b.hook_style)} hook</span>${b.stretch ? '<span class="tag warn">stretch</span>' : ""}
        <span class="right">opportunity ${b.opportunity}</span></div>
      <div class="muted">${esc(b.why_it_works)}</div>
      <div class="ex">${(b.exemplars || []).map((e) => `<a href="${e.url}" target="_blank">${esc(e.title).slice(0, 60)}</a> <span class="muted">${fmt(e.views)} views · ${fmt(Math.round(e.views_per_hour))}/h</span>`).join("<br>")}</div>
      <button data-produce="${i + 1}" data-run="${id}">Produce this</button></div>`).join("")
    || `<p class="muted">No blueprints. Run the pipeline with a TypeSafe key.</p>`;
  $$("button[data-produce]").forEach((b) => b.addEventListener("click", () => {
    $("#prod-run").value = b.dataset.run;
    fillBlueprints(b.dataset.run, true).then(() => { $("#prod-blueprint").value = b.dataset.produce; });
    $$("nav button").find((x) => x.dataset.tab === "studio").click();
  }));
  const rows = r.shorts.filter((s) => !s.excluded.length).slice(0, 25);
  $("#topshorts tbody").innerHTML = rows.map((s, i) => `<tr>
      <td>${i + 1}</td><td>${s.score?.toFixed(3) ?? "-"}</td><td>${fmt(Math.round(s.views_per_hour || 0))}</td>
      <td>${esc(s.format || "?")}</td><td>${esc(s.hook_style || "?")}</td>
      <td><a href="${s.url}" target="_blank">${esc(s.title).slice(0, 70)}</a></td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">Run Rank first.</td></tr>`;
  loadRuns();
}

async function fillBlueprints(runId, userChangedRun = false) {
  const sel = $("#prod-blueprint");
  if (!runId) { sel.innerHTML = ""; state.blueprintRun = null; return; }
  const keep = userChangedRun ? null : sel.value;
  const r = await api(`/api/runs/${runId}`);
  const bps = (r.analysis && r.analysis.blueprints) || [];
  state.blueprintRun = runId;
  sel.innerHTML = bps.map((b, i) => `<option value="${i + 1}">${i + 1}. ${b.format} × ${b.topic} (${b.hook_style}${b.stretch ? ", stretch" : ""})</option>`).join("");
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

// ---------------------------------------------------------------- jobs
async function startJob(kind, params = {}) {
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ kind, params }) });
    toast(`Started ${kind}`);
    attachJob(job.id);
    if (kind !== "produce" && kind !== "upload") $$("nav button").find((x) => x.dataset.tab === "pipeline").click();
  } catch (e) { toast(e.message, true); }
}
function attachJob(id) {
  state.job = { id }; state.jobOffset = 0;
  $("#joblog").textContent = ""; $("#joblog").dataset.empty = "0"; $("#logfile").textContent = "";
  $("#jobpicker").value = "";
  openDrawer();
  pollJob();
}
async function pollJob() {
  if (!state.job) return;
  let j;
  try { j = await api(`/api/jobs/${state.job.id}?offset=${state.jobOffset}`); }
  catch (e) { appendLog([`(console unreachable: ${e.message}; retrying)`]); setTimeout(pollJob, 3000); return; }
  if (j.log.length) { appendLog(j.log); state.jobOffset = j.log_length; }
  $("#jobstatus").textContent = `${j.kind}: ${j.status}`;
  const pill = $("#jobpill");
  pill.classList.remove("hidden"); pill.textContent = `${j.kind} · ${j.status}`; pill.className = `pill ${j.status}`;
  if (j.status === "running" || j.status === "queued") { setTimeout(pollJob, 1500); return; }
  if (j.log_file) $("#logfile").textContent = `saved: ${j.log_file.split(/[\\/]/).slice(-2).join("/")}`;
  if (j.error) toast(`${j.kind} failed: ${j.error.split("\n")[0]}`, true); else toast(`${j.kind} finished`);
  state.job = null;
  setTimeout(() => pill.classList.add("hidden"), 6000);
  loadStatus(); loadRuns(); loadJobPicker();
  if (j.kind === "produce" || j.kind === "upload") loadOutputs();
  if (j.kind === "fetch_music") loadMusic();
}
async function loadJobPicker() {
  const jobs = await api("/api/jobs");
  const logs = await api("/api/logs");
  const sel = $("#jobpicker");
  sel.innerHTML = `<option value="">current job</option>`
    + jobs.map((j) => `<option value="job:${j.id}">${new Date(j.started * 1000).toLocaleTimeString()} ${j.kind} · ${j.status}</option>`).join("")
    + (logs.length ? `<option disabled>── earlier sessions ──</option>` + logs.map((l) => `<option value="log:${l.name}">${l.name.replace(".log", "")}</option>`).join("") : "");
}
$("#jobpicker").addEventListener("change", async (ev) => {
  const v = ev.target.value; if (!v) return;
  $("#joblog").textContent = ""; $("#joblog").dataset.empty = "0"; openDrawer();
  if (v.startsWith("job:")) { const j = await api(`/api/jobs/${v.slice(4)}`); $("#jobstatus").textContent = `${j.kind}: ${j.status}`; appendLog(j.log); if (j.error) appendLog([`ERROR: ${j.error}`]); }
  else { const l = await api(`/api/logs/${encodeURIComponent(v.slice(4))}`); $("#jobstatus").textContent = l.name; appendLog(l.text.split("\n")); }
});
$("#drawertoggle").addEventListener("click", () => {
  const d = $("#drawer"); d.classList.toggle("collapsed");
  $("#drawertoggle").textContent = d.classList.contains("collapsed") ? "▲ Activity log" : "▼ Activity log";
});
$("#logclear").addEventListener("click", () => { $("#joblog").textContent = ""; });
$("#logcopy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#joblog").textContent); toast("Log copied"); }
  catch (_) { const r = document.createRange(); r.selectNodeContents($("#joblog")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Log selected, press Ctrl+C"); }
});
$$("button[data-job]").forEach((b) => b.addEventListener("click", () => {
  const kind = b.dataset.job;
  if (kind === "produce") {
    const run = $("#prod-run").value; if (!run) return toast("Run and analyse the pipeline first", true);
    const bpSel = $("#prod-blueprint");
    const blueprint = Number(bpSel.value || 1);
    const label = bpSel.selectedOptions[0] ? bpSel.selectedOptions[0].textContent : `#${blueprint}`;
    toast(`Producing ${label}`);
    return startJob("produce", { run, blueprint, angle: $("#prod-angle").value, music: $("#prod-music").value, upload: $("#prod-upload").checked });
  }
  const params = { force: $("#opt-force").checked, api: $("#opt-api").checked };
  if (["judge", "rank", "analyze"].includes(kind) && state.currentRun) params.run = state.currentRun;
  startJob(kind, params);
}));

// ---------------------------------------------------------------- studio
async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch (_) {
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy"); ta.remove(); toast(ok ? `${what} copied` : "Copy blocked by the browser; select the text manually", !ok);
  }
}
const tagLine = (o) => (o.hashtags || []).map((h) => "#" + h.replace(/^#/, "")).join(" ");
// Description + hashtags, without repeating tags the description already carries.
function descPack(o) {
  const desc = (o.description || "").trim();
  const missing = (o.hashtags || []).map((h) => "#" + h.replace(/^#/, "")).filter((t) => !desc.toLowerCase().includes(t.toLowerCase()));
  return missing.length ? `${desc}\n\n${missing.join(" ")}` : desc;
}
// Title + description + hashtags for a manual upload, without repeating a title the description opens with.
function uploadPack(o) {
  const body = descPack(o);
  return body.toLowerCase().startsWith((o.title || "").toLowerCase()) ? body : `${o.title}\n\n${body}`;
}

async function loadOutputs() {
  const outs = await api("/api/outputs");
  state.outputs = Object.fromEntries(outs.map((o) => [o.dir, o]));
  const pct = (v) => v == null ? "-" : Math.round(v * 100) + "%";
  const writer = (o) => o.mode === "custom" ? "your own script" : o.backend === "claude_code" ? "written by Claude subscription" : "written by Anthropic API";
  $("#outputs").innerHTML = outs.map((o) => `<div class="out">
      ${o.video_url ? `<video src="${o.video_url}" controls preload="metadata"></video>` : `<div class="novideo">no video</div>`}
      <div class="outbody">
        <h3>${esc(o.title)}</h3>
        <div class="muted">${o.mode === "custom" ? "custom" : `${esc(o.blueprint.format)} × ${esc(o.blueprint.topic)} · ${esc(o.blueprint.hook_style)} hook`} · ${o.duration ? o.duration.toFixed(1) + "s" : ""} · ${writer(o)}${o.music ? ` · ♪ ${esc(o.music.title)}` : ""}</div>
        ${o.qa ? `<div class="qa">hook ${o.qa.hook_strength?.toFixed(1)}/3 · clarity ${o.qa.clarity?.toFixed(1)}/2 · on-format ${pct(o.qa.matches_format)} · payoff ${pct(o.qa.has_payoff)} · policy risk ${pct(o.qa.policy_risk)}</div>` : ""}
        <details><summary>Script</summary><p>${esc(o.script_text)}</p></details>
        <details><summary>Description &amp; hashtags</summary><p class="pre">${esc(o.description)}</p><p>${esc(tagLine(o))}</p></details>
        <div class="row tight">
          <button data-copy="title" data-dir="${o.dir}">Copy title</button>
          <button data-copy="desc" data-dir="${o.dir}">Copy description + hashtags</button>
          <button data-copy="all" data-dir="${o.dir}">Copy all for upload</button>
          <button data-telegram="${o.dir}">Send to Telegram</button>
          ${o.video_url ? `<a class="btn" href="${o.video_url}" download="${esc(o.title).replace(/[^\w ]+/g, "").trim() || "short"}.mp4">Download video</a>` : ""}
        </div>
        <div class="muted small">${esc(o.folder)}</div>
        ${o.youtube_id ? `<a class="tag ok" href="https://youtube.com/shorts/${o.youtube_id}" target="_blank">on YouTube: ${o.youtube_id}</a>`
          : `<button data-upload="${o.dir}">Upload to YouTube</button>`}
      </div></div>`).join("") || `<p class="muted">Nothing produced yet.</p>`;
  $$("button[data-upload]").forEach((b) => b.addEventListener("click", () => startJob("upload", { path: `output/${b.dataset.upload}` })));
  $$("button[data-telegram]").forEach((b) => b.addEventListener("click", () => startJob("telegram", { action: "send", path: `output/${b.dataset.telegram}` })));
  $$("button[data-copy]").forEach((b) => b.addEventListener("click", () => {
    const o = state.outputs[b.dataset.dir]; const kind = b.dataset.copy;
    if (kind === "title") copyText(o.title, "Title");
    else if (kind === "desc") copyText(descPack(o), "Description and hashtags");
    else copyText(uploadPack(o), "Title, description and hashtags");
  }));
}

// ---------------------------------------------------------------- telegram
$("#tgdiscover").addEventListener("click", async () => {
  try {
    const chats = await api("/api/telegram/discover");
    if (!chats.length) return toast("No chats yet: send your bot a message in Telegram, then try again", true);
    const chosen = chats[0];
    $(`input[name="TELEGRAM_CHAT_ID"]`).value = chosen.id;
    await api("/api/settings", { method: "POST", body: JSON.stringify({ paths: { TELEGRAM_CHAT_ID: chosen.id } }) });
    $("#tgstatus").textContent = chats.length === 1 ? `Chat id ${chosen.id} (${chosen.name || chosen.type}) saved.`
      : `Saved ${chosen.id} (${chosen.name || chosen.type}). Others seen: ${chats.slice(1).map((c) => `${c.id} ${c.name || c.type}`).join(", ")}. Change the field above if needed.`;
    toast("Chat id saved"); loadStatus();
  } catch (e) { toast(e.message, true); }
});
$("#tgtest").addEventListener("click", () => startJob("telegram", { action: "test" }));
$("#tg-onproduce").addEventListener("change", async (ev) => {
  try { await api("/api/settings", { method: "POST", body: JSON.stringify({ config: { notifications: { telegram: { on_produce: ev.target.checked } } } }) }); toast(ev.target.checked ? "Automatic Telegram delivery on" : "Automatic Telegram delivery off"); }
  catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- music library
async function loadMusic() {
  const tracks = await api("/api/music");
  state.music = tracks;
  const opts = `<option value="random">music: random</option><option value="none">music: none</option>`
    + tracks.map((t) => `<option value="${esc(t.file)}">music: ${esc(t.title).slice(0, 40)}</option>`).join("");
  $$(".musicpick").forEach((sel) => { const keep = sel.value; sel.innerHTML = opts; if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep; });
  const list = $("#musiclist");
  if (!list) return;
  list.innerHTML = tracks.map((t) => `<div class="track">
      <audio controls preload="none" src="/music/${encodeURIComponent(t.file)}"></audio>
      <span class="name" title="${esc(t.file)}">${esc(t.title)}${t.creator ? ` <span class="muted">by ${esc(t.creator)}</span>` : ""}</span>
      <span class="tag ${t.license === "own" || t.license === "cc0" ? "ok" : ""}">${esc(t.license)}</span>
      ${t.duration ? `<span class="muted">${Math.round(t.duration)}s</span>` : ""}
      <button data-deltrack="${esc(t.file)}" title="delete">✕</button></div>`).join("")
    || `<p class="muted">No tracks yet. Upload a file or fetch free ones.</p>`;
  $$("button[data-deltrack]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/music/${encodeURIComponent(b.dataset.deltrack)}`, { method: "DELETE" }); toast("Track removed"); loadMusic(); loadStatus();
  }));
}
$("#musicupload").addEventListener("click", async () => {
  const f = $("#musicfile").files[0]; if (!f) return toast("Choose an audio file first", true);
  const fd = new FormData(); fd.append("file", f);
  const res = await fetch("/api/music/upload", { method: "POST", body: fd });
  if (!res.ok) return toast((await res.json()).detail, true);
  toast(`Added ${f.name}`); $("#musicfile").value = ""; loadMusic(); loadStatus();
});
$("#musicfetch").addEventListener("click", () => {
  const query = $("#musicquery").value.trim() || "lofi chill";
  startJob("fetch_music", { action: "fetch", query, count: Number($("#musiccount").value || 5) });
});

// custom script: load a .txt into the textarea, then produce without generation
$("#cs-file").addEventListener("change", (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  const reader = new FileReader();
  reader.onload = () => { $("#cs-text").value = String(reader.result || ""); if (!$("#cs-title").value) $("#cs-title").value = f.name.replace(/\.[^.]+$/, ""); toast(`Loaded ${f.name}`); };
  reader.readAsText(f);
});
$("#cs-produce").addEventListener("click", () => {
  const text = $("#cs-text").value.trim();
  if (text.split(/\s+/).length < 5) return toast("Paste a script first (at least a few sentences)", true);
  toast("Producing your script");
  startJob("produce", { script_text: text, title: $("#cs-title").value, description: $("#cs-desc").value,
    hashtags: $("#cs-tags").value, keywords: $("#cs-keywords").value, music: $("#cs-music").value,
    upload: $("#cs-upload").checked });
});

// ---------------------------------------------------------------- settings
async function loadSettings() {
  const s = await api("/api/settings"); state.settings = s;
  $("#keysform").innerHTML = s.keys.map((k) => `<label class="keyrow"><span>${esc(k.label)} ${k.set ? `<span class="tag ok">set ${esc(k.hint)}</span>` : '<span class="tag warn">not set</span>'}</span>
      <span class="keyinput"><input type="password" name="${k.name}" placeholder="${k.set ? "paste to replace" : "paste key"}" autocomplete="off">
      ${k.set ? `<button type="button" data-clear="${k.name}" title="remove">✕</button>` : ""}</span></label>`).join("")
    + s.paths.map((p) => `<label class="keyrow"><span>${esc(p.label)}</span><span class="keyinput"><input name="${p.name}" value="${esc(p.value)}"></span></label>`).join("")
    + `<button class="primary" type="submit">Save keys</button>`;
  $$("button[data-clear]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/settings/keys/${b.dataset.clear}`, { method: "DELETE" }); toast("Key removed"); loadSettings(); loadStatus();
  }));
  $("#keysform").onsubmit = async (ev) => {
    ev.preventDefault();
    const keys = {}, paths = {};
    s.keys.forEach((k) => { const v = $(`input[name="${k.name}"]`).value; if (v.trim()) keys[k.name] = v; });
    s.paths.forEach((p) => { paths[p.name] = $(`input[name="${p.name}"]`).value; });
    try { await api("/api/settings", { method: "POST", body: JSON.stringify({ keys, paths }) }); toast("Keys saved"); loadSettings(); loadStatus(); }
    catch (e) { toast(e.message, true); }
  };
  $("#yt-status").textContent = s.client_secrets_present
    ? (s.youtube_token_present ? "client_secrets.json present and account already authorised." : "client_secrets.json present. The first upload opens a browser to authorise your channel.")
    : "No client_secrets.json yet.";
  const c = s.config;
  const set = (name, v) => { const el = $(`[name="${name}"]`); if (el) el.value = v ?? ""; };
  set("discovery.hashtags", (c.discovery.hashtags || []).join(", "));
  set("discovery.max_age_days", c.discovery.max_age_days); set("discovery.channels_to_follow", c.discovery.channels_to_follow); set("discovery.workers", c.discovery.workers);
  Object.entries(c.ranking.weights || {}).forEach(([k, v]) => set(`ranking.weights.${k}`, v));
  const tg = (c.notifications || {}).telegram || {};
  $("#tg-onproduce").checked = tg.on_produce !== false;
  const tgKey = s.keys.find((k) => k.name === "TELEGRAM_BOT_TOKEN");
  const tgChat = s.paths.find((p) => p.name === "TELEGRAM_CHAT_ID");
  $("#tgstatus").textContent = tgKey && tgKey.set ? (tgChat && tgChat.value ? `Configured for chat ${tgChat.value}.` : "Token saved; now find your chat id.") : "No bot token yet.";
  const intro = c.production.intro || {}, outro = c.production.outro || {};
  $(`[name="production.intro.enabled"]`).checked = intro.enabled !== false;
  set("production.intro.text", intro.text ?? "{title}"); set("production.intro.seconds", intro.seconds ?? 1.5);
  $(`[name="production.outro.enabled"]`).checked = outro.enabled !== false;
  set("production.outro.text", outro.text ?? "Follow for more"); set("production.outro.handle", outro.handle ?? ""); set("production.outro.seconds", outro.seconds ?? 2);
  const m = c.production.music || {};
  set("production.music.default", m.default === "none" ? "none" : "random");
  set("production.music.volume_db", m.volume_db ?? c.production.music_volume_db ?? -18);
  set("production.music.fade_seconds", m.fade_seconds ?? 1.5);
  $(`[name="production.music.duck"]`).checked = m.duck !== false;
  set("production.voice", c.production.voice); set("production.target_seconds", c.production.target_seconds);
  set("production.background_source", c.production.background_source); set("production.script_backend", c.production.script_backend || "auto");
  set("upload.privacy", c.upload.privacy);
}
$("#cfgform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const g = (name) => $(`[name="${name}"]`).value;
  const num = (name) => Number(g(name));
  const config = {
    discovery: { hashtags: g("discovery.hashtags").split(",").map((x) => x.trim().replace(/^#/, "")).filter(Boolean),
      max_age_days: num("discovery.max_age_days"), channels_to_follow: num("discovery.channels_to_follow"), workers: num("discovery.workers") },
    ranking: { weights: Object.fromEntries(["velocity", "engagement", "replicable", "hook", "evergreen"].map((k) => [k, num(`ranking.weights.${k}`)])) },
    production: { voice: g("production.voice"), target_seconds: num("production.target_seconds"),
      background_source: g("production.background_source"), script_backend: g("production.script_backend"),
      music: { default: g("production.music.default"), volume_db: num("production.music.volume_db"),
        fade_seconds: num("production.music.fade_seconds"), duck: $(`[name="production.music.duck"]`).checked },
      intro: { ...(state.settings.config.production.intro || {}), enabled: $(`[name="production.intro.enabled"]`).checked,
        text: g("production.intro.text") || "{title}", seconds: num("production.intro.seconds") || 1.5 },
      outro: { ...(state.settings.config.production.outro || {}), enabled: $(`[name="production.outro.enabled"]`).checked,
        text: g("production.outro.text") || "Follow for more", handle: g("production.outro.handle"), seconds: num("production.outro.seconds") || 2 } },
    upload: { privacy: g("upload.privacy") },
  };
  try { await api("/api/settings", { method: "POST", body: JSON.stringify({ config }) }); $("#cfgsaved").textContent = "saved"; toast("Settings saved"); }
  catch (e) { toast(e.message, true); }
});
$("#secretsbtn").addEventListener("click", async () => {
  const f = $("#secretsfile").files[0]; if (!f) return toast("Choose the JSON file first", true);
  const fd = new FormData(); fd.append("file", f);
  const res = await fetch("/api/settings/client-secrets", { method: "POST", body: fd });
  if (!res.ok) return toast((await res.json()).detail, true);
  toast("client_secrets.json saved"); loadSettings(); loadStatus();
});

// ---------------------------------------------------------------- boot
loadStatus(); loadRuns(); loadOutputs(); loadJobPicker(); loadMusic();
setInterval(loadStatus, 10000);

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
    card("Script writer", !!s.script_backend || !!(s.ollama && s.ollama.model_ready), (s.script_backend === "api" ? "Anthropic API" : s.script_backend === "claude_code" ? "Claude subscription" : (s.ollama && s.ollama.model_ready ? "free local model only" : "not configured"))
      + (s.ollama && s.ollama.model_ready ? ` · free fallback: ${s.ollama.model}` : (s.ollama && s.ollama.server ? " · Ollama running, model not pulled" : ""))),
    card("Backgrounds", s.pexels || s.ai_images, s.pexels ? `Pexels footage${s.ai_images ? " + AI images" : ""}` : s.ai_images ? "AI images" : "generated gradient"),
    card("Music", s.music_tracks > 0, s.music_tracks > 0 ? `${s.music_tracks} track${s.music_tracks === 1 ? "" : "s"}` : "no tracks (Settings)"),
    card("Telegram", s.telegram, s.telegram ? "delivery on" : "not connected"),
    card("YouTube upload", s.youtube_upload, s.youtube_upload ? "ready" : "client secrets missing"),
    card("Latest run", !!s.latest_run, s.latest_run ? `${s.latest_run.shorts} Shorts, ${s.latest_run.blueprints} blueprints` : "none yet"),
    card("Produced", s.outputs > 0, `${s.outputs} Shorts`),
    card("Scheduler", s.schedule && s.schedule.enabled, s.schedule && s.schedule.enabled
      ? `${s.schedule.done_today} done today${s.schedule.next && s.schedule.next[0] ? `, next ${s.schedule.next[0].kind} ${s.schedule.next[0].time}` : ""}` : "off (Settings)"),
  ].join("");
  loadSchedule();
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
  if (j.kind === "produce" || j.kind === "upload" || j.kind === "publish" || j.kind === "thumbnail") loadOutputs();
  if (j.kind === "fetch_music") loadMusic();
  if (j.kind === "channel") { loadSchedule(); loadOutputs(); }
  if (j.kind === "profile") loadProfiles();
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

async function loadOutputs(afterStatusSync = false) {
  // Ask the server to re-read privacy from YouTube (throttled); redraw once it has had time to finish,
  // so a video made public in YouTube Studio shows as public here.
  if (!afterStatusSync) {
    api("/api/outputs/refresh-status", { method: "POST" }).then(async (r) => {
      if (!r.started && !r.running) return;
      for (let i = 0; i < 45; i++) {                       // wait for YouTube's answer (up to ~90 s)
        await new Promise((res) => setTimeout(res, 2000));
        const s = await api("/api/outputs/refresh-status");
        if (!s.running) { if (s.changed) loadOutputs(true); return; }
      }
    }).catch(() => {});
  }
  const outs = await api("/api/outputs");
  state.outputs = Object.fromEntries(outs.map((o) => [o.dir, o]));
  const pct = (v) => v == null ? "-" : Math.round(v * 100) + "%";
  const writer = (o) => o.mode === "custom" ? "your own script, as written" : o.mode === "enhanced" ? "your script, enhanced" : o.backend === "claude_code" ? "written by Claude subscription" : "written by Anthropic API";
  $("#outputs").innerHTML = outs.map((o) => `<div class="out">
      ${o.video_url ? `<video src="${o.video_url}" controls preload="metadata"${o.thumbnail_url ? ` poster="${o.thumbnail_url}"` : ""}></video>` : `<div class="novideo">no video</div>`}
      <div class="outbody">
        <h3>${esc(o.title)} <span class="tag ${o.title.length <= 100 ? "ok" : "warn"}" title="YouTube title limit is 100 characters; Shorts show about 70">${o.title.length}/100</span></h3>
        <div class="muted">${o.mode === "custom" ? "custom" : `${esc(o.blueprint.format)} × ${esc(o.blueprint.topic)} · ${esc(o.blueprint.hook_style)} hook`} · ${o.duration ? o.duration.toFixed(1) + "s" : ""} · ${writer(o)}${o.music ? ` · ♪ ${esc(o.music.title)}` : ""}</div>
        ${o.qa ? `<div class="qa">hook ${o.qa.hook_strength?.toFixed(1)}/3 · clarity ${o.qa.clarity?.toFixed(1)}/2 · on-format ${pct(o.qa.matches_format)} · payoff ${pct(o.qa.has_payoff)} · policy risk ${pct(o.qa.policy_risk)}${o.qa.faithful != null ? ` · faithful ${pct(o.qa.faithful)}` : ""}</div>` : ""}
        <details><summary>Script</summary><p>${esc(o.script_text)}</p></details>
        ${o.original_text ? `<details><summary>Your original</summary><p class="muted">${esc(o.original_text)}</p></details>` : ""}
        <details><summary>Description &amp; hashtags</summary><p class="pre">${esc(o.description)}</p><p>${esc(tagLine(o))}</p></details>
        <div class="row tight">
          <button data-copy="title" data-dir="${o.dir}" title="goes in YouTube's title box (100 char limit)">Copy title</button>
          <button data-copy="desc" data-dir="${o.dir}" title="goes in YouTube's description box">Copy description + hashtags</button>
          <button data-telegram="${o.dir}">Send to Telegram</button>
          ${o.video_url ? `<a class="btn" href="${o.video_url}" download="${esc(o.title).replace(/[^\w ]+/g, "").trim() || "short"}.mp4">Download video</a>` : ""}
          ${o.thumbnail_url ? `<a class="btn" href="${o.thumbnail_url}" download="${esc(o.title).replace(/[^\w ]+/g, "").trim() || "short"}_thumbnail.jpg" title="${esc(o.thumbnail_text || "")}">Download thumbnail</a>` : ""}
          <button data-thumb="${o.dir}" title="${o.thumbnail_url ? "rebuild the thumbnail" : "build a thumbnail for this video"}${o.youtube_id ? " and set it on YouTube" : ""}">${o.thumbnail_url ? "Rebuild" : "Make"} thumbnail${o.youtube_id ? " + set on YouTube" : ""}</button>
        </div>
        <div class="muted small">${esc(o.folder)}</div>
        ${!o.youtube_id && o.upload_state === "queued" ? `<span class="tag ${o.upload_priority >= (state.minPriority ?? 30) ? "ok" : "warn"}" title="${esc(Object.entries(o.priority_parts || {}).map(([k, v]) => `${k} ${Math.round(v * 100)}`).join(", "))}">in upload queue · priority ${o.upload_priority}</span>` : ""}
        ${!o.youtube_id && o.upload_state === "expired" ? `<span class="tag warn">not uploaded automatically (waited too long)</span>` : ""}
        ${!o.youtube_id && o.upload_state === "failed" ? `<span class="tag warn" title="${esc(o.upload_error || "")}">automatic upload failed</span>` : ""}
        ${o.youtube_id && o.privacy === "deleted" ? `<span class="tag warn">removed from YouTube (${esc(o.youtube_id)})</span>` : ""}
        ${o.youtube_id && o.publish_at && o.privacy === "private" ? `<span class="tag ok" title="YouTube publishes it by itself at this time">goes public ${esc(new Date(o.publish_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }))}</span>
             <button data-publish="${o.dir}" data-privacy="private" title="cancel the planned publish; it stays private">Keep private</button>` : ""}
        ${o.youtube_id && o.privacy !== "deleted" ? `<a class="tag ${o.privacy === "private" ? "warn" : "ok"}" href="https://youtube.com/shorts/${o.youtube_id}" target="_blank">on YouTube${o.privacy ? ` (${o.privacy})` : ""}${o.channel_stats ? `: ${fmt(o.channel_stats.views)} views · ${o.channel_stats.views_per_hour}/h · ${fmt(o.channel_stats.likes)} likes` : `: ${o.youtube_id}`}</a>
             ${o.privacy !== "public" ? `<button data-publish="${o.dir}" data-privacy="public">${o.publish_at ? "Publish now" : "Make public"}</button>` : ""}
             ${o.privacy !== "unlisted" && o.privacy !== "public" ? `<button data-publish="${o.dir}" data-privacy="unlisted">Unlisted</button>` : ""}
             ${o.privacy === "public" ? `<button data-publish="${o.dir}" data-privacy="private">Make private</button>` : ""}`
          : `<button data-upload="${o.dir}">Upload to YouTube</button>`}
      </div></div>`).join("") || `<p class="muted">Nothing produced yet.</p>`;
  $$("button[data-upload]").forEach((b) => b.addEventListener("click", () => startJob("upload", { path: `output/${b.dataset.upload}` })));
  $$("button[data-telegram]").forEach((b) => b.addEventListener("click", () => startJob("telegram", { action: "send", path: `output/${b.dataset.telegram}` })));
  $$("button[data-publish]").forEach((b) => b.addEventListener("click", () => startJob("publish", { path: `output/${b.dataset.publish}`, privacy: b.dataset.privacy })));
  $$("button[data-thumb]").forEach((b) => b.addEventListener("click", () => startJob("thumbnail", { path: `output/${b.dataset.thumb}` })));
  $$("button[data-copy]").forEach((b) => b.addEventListener("click", () => {
    const o = state.outputs[b.dataset.dir]; const kind = b.dataset.copy;
    if (kind === "title") copyText(o.title.slice(0, 100), "Title");
    else copyText(descPack(o), "Description and hashtags");
  }));
}

// ---------------------------------------------------------------- captions
const CAP_FIELDS = ["font", "size", "words_per_caption", "primary", "highlight", "outline_color", "outline", "shadow", "highlight_mode", "position", "uppercase"];
function capRead() {
  const st = { style: $("#cap-preset").value };
  for (const f of CAP_FIELDS) {
    const el = $(`[name="captions.${f}"]`); if (!el) continue;
    if (el.type === "checkbox") st[f] = el.checked;
    else if (el.type === "number") st[f] = Number(el.value);
    else st[f] = el.value;
  }
  return st;
}
function capFill(values) {
  for (const f of CAP_FIELDS) {
    const el = $(`[name="captions.${f}"]`); if (!el || values[f] == null) continue;
    if (el.type === "checkbox") el.checked = !!values[f]; else el.value = values[f];
  }
}
let capTimer = null;
async function capPreview() {
  clearTimeout(capTimer);
  capTimer = setTimeout(async () => {
    try {
      const res = await fetch("/api/captions/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ style: capRead() }) });
      if (!res.ok) return;
      const blob = await res.blob(); $("#cap-preview").src = URL.createObjectURL(blob);
    } catch (_) {}
  }, 350);
}
async function loadCaptions() {
  const p = await api("/api/captions/presets"); state.capPresets = p.presets;
  const sel = $("#cap-preset");
  sel.innerHTML = Object.entries(p.presets).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  const cur = (state.settings && state.settings.config.production.captions) || {};
  sel.value = cur.style && p.presets[cur.style] ? cur.style : "bold_impact";
  capFill(p.current);
  capPreview();
}
$("#cap-preset").addEventListener("change", () => { capFill(state.capPresets[$("#cap-preset").value]); capPreview(); });
$("#cap-reset").addEventListener("click", () => { capFill(state.capPresets[$("#cap-preset").value]); capPreview(); });
CAP_FIELDS.forEach((f) => { const el = $(`[name="captions.${f}"]`); if (el) el.addEventListener("input", capPreview); });

// ---------------------------------------------------------------- schedule
async function loadSchedule() {
  const p = await api("/api/schedule");
  const c = p.config;
  $("#schedsummary").textContent = c.enabled
    ? `on: ${c.produces_per_day} Shorts/day, ${c.refresh_per_day} trend refresh/day, ${String(c.start_hour).padStart(2, "0")}:00-${String(c.end_hour).padStart(2, "0")}:00, ${p.done_today}/${c.produces_per_day} done today`
    : "off. Turn it on under Settings.";
  $("#schednext").innerHTML = p.next.length ? "Next: " + p.next.map((n) => `<span class="tag">${n.time} ${esc(n.kind)}</span>`).join(" ") : (c.enabled ? "No more slots today." : "");
  $("#schedeligible").innerHTML = p.eligible && p.eligible.length
    ? (p.source === "profile" && p.profile ? `Producing in the style of <b>${esc(p.profile)}</b> (Settings → Schedule → Source). Trend blueprints for reference: ` : `Producing from run ${esc(p.run || "")}: `)
      + p.eligible.map((b) => `<span class="tag ${b.source === "channel" ? "ok" : ""}" title="${b.source === "channel" ? `your channel's winner: ${b.channel_videos} videos, x${b.channel_factor} your median` : `opportunity ${b.opportunity}, ${b.count} trending Shorts${b.channel_videos ? `, your channel: ${b.channel_videos} videos (${b.channel_basis}), factor x${b.channel_factor}` : ""}`}">${b.source === "channel" ? "★" : "#" + b.index} ${esc(b.format)} × ${esc(b.topic)} · ${Math.round(b.weight * 100)}%${b.channel_videos ? ` <span class="${b.channel_factor >= 1 ? "ok" : "warn"}">(you: x${b.channel_factor})</span>` : ""}</span>`).join(" ")
    : "No eligible blueprints yet (a trend refresh will run first).";
  const q = p.queue;
  $("#queuepanel").innerHTML = !q ? "" : !q.auto ? "Automatic upload is off (Settings → Automatic uploads)."
    : `Upload queue: <b>${q.queued}</b> waiting${q.below_min ? ` (${q.below_min} below priority ${q.min_priority}, waiting for you)` : ""} · uploaded ${q.uploaded_24h}/${q.limit} in 24 h`
      + (q.paused_until ? ` · <span class="warn">paused until ${esc(q.paused_until)} (${esc(q.paused_reason || "")})</span>` : "")
      + (q.next ? ` · next: <span class="tag ok">${q.next.priority} ${esc(q.next.title.slice(0, 50))}</span>` : (q.waiting_reason ? ` · ${esc(q.waiting_reason)}` : ""))
      + (p.publish && p.publish.enabled ? ` · goes public at good hours (next free ${esc(p.publish.next_publish)})` : "")
      + ((p.retries || []).length ? ` · retries pending: ${p.retries.map((r) => `${esc(r.due.slice(11, 16))} (${esc(r.reason)})`).join(", ")}` : "");
  loadChannel();
  const detail = (h) => h.params ? (h.params.blueprint || h.params.blueprint_key || (h.params.title ? `${h.params.priority ?? ""} ${h.params.title}` : "-")) : (h.retry_at ? `retry at ${h.retry_at}` : "-");
  $("#schedhistory tbody").innerHTML = p.history.map((h) => `<tr><td>${esc(h.slot)}</td><td>${esc(h.kind)}</td><td>${esc(h.started)}</td>
      <td class="${h.status === "error" ? "err" : ""}">${esc(h.status)}${h.retry_at ? ` → retry ${esc(h.retry_at)}` : ""}</td><td>${esc(String(detail(h)))}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Nothing scheduled has run yet.</td></tr>`;
}

// ---------------------------------------------------------------- channel feedback
async function loadChannel() {
  const c = await api("/api/channel");
  const el = $("#channelpanel");
  if (!c.configured) { el.innerHTML = `<span class="warn">Channel feedback is OFF, so production cannot learn from your views.</span> Add your Google OAuth client_secrets.json (Settings → YouTube upload) or a YouTube API key plus your channel handle, then Sync.`; return; }
  const r = c.report;
  if (!r) { el.innerHTML = `Channel feedback configured, not synced yet.`; return; }
  const perf = r.performance;
  const rows = Object.entries(perf.blueprints).sort((a, b) => b[1].factor - a[1].factor)
    .map(([k, v]) => `<span class="tag ${v.factor >= 1 ? "ok" : "warn"}" title="${v.videos} videos, median ${v.median_views} views">${esc(k.replace("|", " × "))} · ${v.median_views_per_hour}/h · x${v.factor}${v.provisional ? " (provisional)" : ""}</span>`).join(" ");
  const winners = (r.winners || []).map((w) => `<span class="tag ok">★ ${esc(w.key.replace("|", " × "))} x${w.opportunity}</span>`).join(" ");
  el.innerHTML = `Your channel <b>${esc(r.channel.title)}</b>: ${r.uploads} Shorts, ${perf.uploads_labelled ?? 0} labelled by format × topic, median ${perf.channel_median_vph ?? "-"} views/h over ${perf.videos} scored (synced ${esc(r.fetched_at)}). `
    + (winners ? `<br>Channel winners (always in the production pool): ${winners}<br>` : "")
    + (rows || "No labelled videos old enough to score yet - run Sync.");
}
$("#channelsync").addEventListener("click", () => startJob("channel", { action: "sync" }));

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

// ---------------------------------------------------------------- style profiles
async function loadProfiles() {
  const ps = await api("/api/profiles"); state.profiles = ps;
  const opts = ps.map((p) => `<option value="${esc(p.handle)}">${esc(p.channel || p.handle)} · ${(p.top_formats || []).slice(0, 2).map((f) => f.name).join(", ")}</option>`).join("");
  const sel = $("#pf-select"); const keep = sel.value;
  sel.innerHTML = opts || `<option value="">no profiles yet</option>`;
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  const sched = $(`[name="schedule.profile"]`);
  if (sched) { const k = sched.value; sched.innerHTML = `<option value="">(none)</option>` + ps.map((p) => `<option value="${esc(p.handle)}">${esc(p.channel || p.handle)}</option>`).join(""); if (k) sched.value = k; }
  showProfileSummary();
}
async function showProfileSummary() {
  const h = $("#pf-select").value; const el = $("#pf-summary");
  if (!h) { el.textContent = ""; return; }
  try {
    const p = await api(`/api/profiles/${encodeURIComponent(h)}`);
    const sg = p.style_guide;
    el.innerHTML = `<b>${esc(p.channel)}</b>: ${p.videos} Shorts, ~${p.typical_seconds}s · formats ${(p.top_formats || []).map((f) => `${esc(f.name)} ${Math.round(f.share * 100)}%`).join(", ")} · topics ${(p.top_topics || []).slice(0, 4).map((t) => `${esc(t.name)} ${Math.round(t.share * 100)}%`).join(", ")}`
      + (sg ? `<details><summary>Style guide</summary><p><b>Voice:</b> ${esc(sg.voice_and_tone)}</p><p><b>Hooks:</b> ${esc((sg.hook_patterns || []).join(" · "))}</p><p><b>Structure:</b> ${esc(sg.structure)}</p><p><b>Pacing:</b> ${esc(sg.pacing_and_sentences)}</p><p><b>Endings:</b> ${esc(sg.ending_and_cta)}</p><p><b>Do:</b> ${esc((sg.dos || []).join(" · "))}</p><p><b>Never:</b> ${esc((sg.donts || []).join(" · "))}</p></details>` : " · no style guide yet (written on the next produce if Claude is available)");
    const vids = p.exemplars || [];
    const rows = vids.map((v) => `<tr><td><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title || v.id)}</a></td>
        <td>${Number(v.views || 0).toLocaleString()}</td><td>${v.duration ? v.duration + "s" : "-"}</td>
        <td>${esc((v.format || "?") + " × " + (v.topic || "?"))}</td><td>${esc(v.hook_style || "")}</td>
        <td><button class="pf-remake" data-id="${esc(v.id)}" data-title="${esc(v.title || "")}">Produce like this</button></td></tr>`).join("");
    const head = `Their Shorts (${vids.length}${vids.length < (p.videos || 0) ? ` of ${p.videos}; analyse again to list all` : ""}, most viewed first)`;
    el.innerHTML += `<div class="tablewrap" style="margin-top:8px"><table><thead><tr><th>${head}</th><th>Views</th><th>Length</th><th>Format × topic</th><th>Hook</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted">No videos stored; analyse the channel again.</td></tr>`}</tbody></table></div>`;
    el.querySelectorAll(".pf-remake").forEach((b) => b.addEventListener("click", () => {
      toast(`Producing a remake of "${b.dataset.title.slice(0, 50)}" in the style of ${h}`);
      startJob("produce", { profile: h, exemplar: b.dataset.id, angle: $("#pf-angle").value, music: $("#pf-music").value, upload: $("#pf-upload").checked });
    }));
  } catch (_) { el.textContent = ""; }
}
$("#pf-select").addEventListener("change", showProfileSummary);
$("#pf-add").addEventListener("click", () => {
  const t = $("#pf-target").value.trim(); if (!t) return toast("Paste a channel URL or @handle", true);
  startJob("profile", { action: "add", target: t, videos: Number($("#pf-videos").value || 24) });
});
$("#pf-produce").addEventListener("click", () => {
  const h = $("#pf-select").value; if (!h) return toast("Analyse a channel first", true);
  toast(`Producing in the style of ${h}`);
  startJob("produce", { profile: h, angle: $("#pf-angle").value, music: $("#pf-music").value, upload: $("#pf-upload").checked });
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
    enhance: !$("#cs-aswritten").checked, upload: $("#cs-upload").checked });
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
  const ytKey = s.keys.find((k) => k.name === "YOUTUBE_API_KEY"), ytCh = s.paths.find((p) => p.name === "YOUTUBE_CHANNEL");
  $("#channelstatus").textContent = ytKey && ytKey.set ? (ytCh && ytCh.value ? `Configured for ${ytCh.value}.` : "API key saved; add your channel handle above and save.") : "No YouTube API key yet.";
  loadCaptions();
  const ai = c.production.ai_images || {};
  set("production.ai_images.mode", ai.enabled === false ? "never" : (ai.mode || "fallback"));
  set("production.ai_images.provider", ai.provider || "pollinations");
  set("production.ai_images.style", ai.style || "");
  set("production.ai_images.model", ai.model || "");
  const sc = c.schedule || {};
  $(`[name="schedule.enabled"]`).checked = !!sc.enabled;
  set("schedule.produces_per_day", sc.produces_per_day ?? 20); set("schedule.refresh_per_day", sc.refresh_per_day ?? 1);
  set("schedule.start_hour", sc.start_hour ?? 6); set("schedule.end_hour", sc.end_hour ?? 24); set("schedule.blueprints_to_rotate", sc.blueprints_to_rotate ?? 5);
  set("schedule.selection", sc.selection || "weighted"); set("schedule.min_share_pct", Math.round((sc.min_share ?? 0.25) * 100));
  set("schedule.source", sc.source || "trends"); loadProfiles().then(() => set("schedule.profile", sc.profile || ""));
  $(`[name="schedule.skip_stretch"]`).checked = sc.skip_stretch !== false;
  const th = c.production.thumbnail || {};
  $(`[name="production.thumbnail.enabled"]`).checked = th.enabled !== false;
  $(`[name="production.thumbnail.band"]`).checked = th.band !== false;
  set("production.thumbnail.handle", th.handle || "");
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
  const ol = c.production.ollama || {};
  $(`[name="production.ollama.fallback"]`).checked = ol.fallback !== false; set("production.ollama.model", ol.model || "qwen2.5:3b");
  const up = c.upload || {};
  $(`[name="upload.auto"]`).checked = up.auto !== false;
  set("upload.daily_limit", up.daily_limit ?? 20); set("upload.min_priority", up.min_priority ?? 30);
  set("upload.max_age_hours", up.max_age_hours ?? 36); set("upload.min_gap_minutes", up.min_gap_minutes ?? 20);
  $(`[name="upload.schedule_publish"]`).checked = up.schedule_publish !== false;
  set("upload.review_hours", up.review_hours ?? 2); set("upload.max_per_hour", up.max_per_hour ?? 2);
  api("/api/publish-times").then((p) => { $("#publishhours").textContent = `Best hours for this channel (your time, learned from ${p.videos} videos): ${p.best_local_hours.map((h) => String(h).padStart(2, "0") + ":00").join(", ")}. Next free publish time: ${p.next_publish}.`; }).catch(() => {});
  const tgc = (c.notifications || {}).telegram || {};
  $(`[name="notifications.telegram.alerts"]`).checked = tgc.alerts !== false;
  set("notifications.telegram.daily_report_hour", tgc.daily_report_hour === null || tgc.daily_report_hour === false ? "" : (tgc.daily_report_hour ?? 22));
  $(`[name="schedule.retry_failed"]`).checked = (c.schedule || {}).retry_failed !== false;
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
      ollama: { ...(state.settings.config.production.ollama || {}), fallback: $(`[name="production.ollama.fallback"]`).checked,
        model: g("production.ollama.model") || "qwen2.5:3b" },
      music: { default: g("production.music.default"), volume_db: num("production.music.volume_db"),
        fade_seconds: num("production.music.fade_seconds"), duck: $(`[name="production.music.duck"]`).checked },
      captions: capRead(),
      thumbnail: { enabled: $(`[name="production.thumbnail.enabled"]`).checked, band: $(`[name="production.thumbnail.band"]`).checked,
        handle: g("production.thumbnail.handle") },
      ai_images: { enabled: g("production.ai_images.mode") !== "never", mode: g("production.ai_images.mode"),
        provider: g("production.ai_images.provider"), style: g("production.ai_images.style"), model: g("production.ai_images.model") },
      intro: { ...(state.settings.config.production.intro || {}), enabled: $(`[name="production.intro.enabled"]`).checked,
        text: g("production.intro.text") || "{title}", seconds: num("production.intro.seconds") || 1.5 },
      outro: { ...(state.settings.config.production.outro || {}), enabled: $(`[name="production.outro.enabled"]`).checked,
        text: g("production.outro.text") || "Follow for more", handle: g("production.outro.handle"), seconds: num("production.outro.seconds") || 2 } },
    upload: { privacy: g("upload.privacy"), auto: $(`[name="upload.auto"]`).checked,
      daily_limit: num("upload.daily_limit") || 20, min_priority: Math.min(100, Math.max(0, num("upload.min_priority"))),
      max_age_hours: num("upload.max_age_hours") || 36, min_gap_minutes: Math.max(0, num("upload.min_gap_minutes")),
      schedule_publish: $(`[name="upload.schedule_publish"]`).checked, review_hours: Math.max(0, num("upload.review_hours")),
      max_per_hour: num("upload.max_per_hour") || 2 },
    notifications: { telegram: { alerts: $(`[name="notifications.telegram.alerts"]`).checked,
      daily_report_hour: g("notifications.telegram.daily_report_hour") === "" ? null : num("notifications.telegram.daily_report_hour") } },
    schedule: { enabled: $(`[name="schedule.enabled"]`).checked, produces_per_day: num("schedule.produces_per_day") || 20,
      refresh_per_day: num("schedule.refresh_per_day"), start_hour: num("schedule.start_hour"), end_hour: num("schedule.end_hour") || 24,
      blueprints_to_rotate: num("schedule.blueprints_to_rotate") || 5, selection: g("schedule.selection"),
      min_share: Math.min(1, Math.max(0, num("schedule.min_share_pct") / 100)), skip_stretch: $(`[name="schedule.skip_stretch"]`).checked,
      source: g("schedule.source"), profile: g("schedule.profile"), retry_failed: $(`[name="schedule.retry_failed"]`).checked },
  };
  try { await api("/api/settings", { method: "POST", body: JSON.stringify({ config }) }); $("#cfgsaved").textContent = "saved"; toast("Settings saved"); loadStatus(); }
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
loadStatus(); loadRuns(); loadOutputs(); loadJobPicker(); loadMusic(); loadProfiles();
setInterval(loadStatus, 10000);

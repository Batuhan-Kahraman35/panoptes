"use strict";
// Kayıtlar sekmesi: günün kayıtlı aralıkları, tarayıcıda oynatma, dışa aktarma.
// Saatler DVR'ın yerel saatidir; gün içindeki saniye olarak tutulur (0..86400).

const DAY = 86400;
const SPEEDS = [1, 2, 4];
const RESUME_AFTER_MS = 15000;  // daha uzun duraklatmada sunucu akışı kapatır; kaldığı yerden yeniden açılır

const rec = { spans: [], startSec: null, speed: 1, pausedAt: 0, jobsTimer: null };
const video = $("#rec-video");

const pad = (n) => String(n).padStart(2, "0");
const hms = (sec) => { sec = Math.floor(((sec % DAY) + DAY) % DAY); return `${pad(sec / 3600 | 0)}:${pad((sec % 3600) / 60 | 0)}:${pad(sec % 60)}`; };
const toSec = (text) => { const [h, m, s = 0] = text.split(":").map(Number); return h * 3600 + m * 60 + s; };
const isoAt = (sec) => `${$("#rec-date").value}T${hms(sec)}`;
const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };

function currentBranch() {
  return state.branches.find((b) => b.id === $("#rec-branch").value);
}

function openRecordings() {
  const sel = $("#rec-branch");
  if (!sel.options.length) {
    sel.innerHTML = state.branches.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("");
    $("#rec-date").value = today();
    $("#rec-speed").innerHTML = SPEEDS.map((s) => `<button class="chip${s === 1 ? " on" : ""}" data-speed="${s}">${s}x</button>`).join("");
    fillChannels();
  }
  loadSpans();
  loadJobs();
}

function fillChannels() {
  const b = currentBranch();
  const off = new Set(b?.disabled || []);
  $("#rec-channel").innerHTML = (b?.channels || []).map((c) =>
    `<option value="${c.no}">${esc(c.name)}${off.has(c.no) ? " (pasif)" : ""}</option>`).join("");
}

// ------------------------------------------------------------------ zaman çizelgesi

async function loadSpans() {
  stopPlayback("Zaman çizelgesinde yeşil bir alana tıklayın.");
  rec.spans = [];
  renderSpans();
  const b = currentBranch(), ch = $("#rec-channel").value;
  if (!b || !ch) return;
  $("#rec-status").textContent = "Kayıtlar aranıyor…";
  try {
    const data = await api("GET", `/api/recordings/${b.id}/${ch}?date=${$("#rec-date").value}`);
    const base = new Date(`${data.date}T00:00:00`).getTime();
    rec.spans = data.spans.map(([s, e]) => [(new Date(s) - base) / 1000, (new Date(e) - base) / 1000]);
    const total = rec.spans.reduce((t, [s, e]) => t + e - s, 0);
    $("#rec-status").textContent = rec.spans.length ? `${(total / 3600).toFixed(1)} saat kayıt` : "Bu gün kayıt yok";
  } catch (err) {
    $("#rec-status").textContent = err.message;
  }
  renderSpans();
}

function renderSpans() {
  $("#rec-spans").innerHTML = rec.spans.map(([s, e]) =>
    `<div style="left:${s / DAY * 100}%;width:${Math.max(0.15, (e - s) / DAY * 100)}%"></div>`).join("");
  renderRange();
}

function renderRange() {
  const r = $("#rec-range"), s = $("#exp-start").value, e = $("#exp-end").value;
  r.hidden = !(s && e && toSec(e) > toSec(s));
  if (!r.hidden) Object.assign(r.style, { left: `${toSec(s) / DAY * 100}%`, width: `${(toSec(e) - toSec(s)) / DAY * 100}%` });
}

function secFromEvent(e) {
  const box = $("#rec-timeline").getBoundingClientRect();
  return Math.max(0, Math.min(DAY - 1, Math.round((e.clientX - box.left) / box.width * DAY)));
}

$("#rec-timeline").addEventListener("mousemove", (e) => {
  const h = $("#rec-hover"), sec = secFromEvent(e);
  h.hidden = false;
  h.textContent = hms(sec);
  h.style.left = `${Math.min(96, Math.max(4, sec / DAY * 100))}%`;
});
$("#rec-timeline").addEventListener("mouseleave", () => { $("#rec-hover").hidden = true; });
$("#rec-timeline").addEventListener("click", (e) => {
  let sec = secFromEvent(e);
  if (!rec.spans.some(([s, en]) => sec >= s && sec < en)) {
    // boşluğa tıklandıysa sonraki kaydın başına git
    const next = rec.spans.find(([s]) => s > sec);
    if (!next) return toast("Bu saatten sonra kayıt yok");
    sec = Math.ceil(next[0]);
  }
  play(sec);
});

// ------------------------------------------------------------------ oynatma

function clockSec() {
  return rec.startSec + video.currentTime * rec.speed;
}

function play(sec, speed = rec.speed) {
  const b = currentBranch(), ch = $("#rec-channel").value;
  rec.startSec = sec;
  rec.speed = speed;
  document.querySelectorAll("#rec-speed .chip").forEach((c) => c.classList.toggle("on", Number(c.dataset.speed) === speed));
  showMsg(`${hms(sec)} yükleniyor…`);
  video.src = `/api/playback/${b.id}/${ch}?start=${isoAt(sec)}&speed=${speed}`;
  video.play().catch(() => {});
  $("#rec-play").disabled = false;
  $("#rec-play").textContent = "⏸ Duraklat";
  $("#rec-head").hidden = false;
  tick();
}

function stopPlayback(message) {
  video.removeAttribute("src");
  video.load();
  rec.startSec = null;
  $("#rec-head").hidden = true;
  $("#rec-play").disabled = true;
  $("#rec-clock").textContent = "--:--:--";
  showMsg(message);
}

function showMsg(text) {
  $("#rec-msg").textContent = text || "";
  $("#rec-msg").hidden = !text;
}

function tick() {
  if (rec.startSec === null) return;
  const sec = clockSec();
  $("#rec-clock").textContent = hms(sec);
  $("#rec-head").style.left = `${sec / DAY * 100}%`;
}

setInterval(tick, 500);
video.addEventListener("playing", () => showMsg(""));
video.addEventListener("waiting", () => { if (rec.startSec !== null) showMsg("Yükleniyor…"); });
video.addEventListener("ended", () => showMsg("Kayıt burada bitiyor."));
video.addEventListener("error", () => {
  if (rec.startSec === null || !video.getAttribute("src")) return;
  showMsg("Kayıt açılamadı. DVR'a ulaşılamıyor, bu saatte kayıt yok ya da aynı anda çok fazla kayıt izleniyor.");
});

$("#rec-play").addEventListener("click", () => {
  if (rec.startSec === null) return;
  if (!video.paused) {
    video.pause();
    rec.pausedAt = Date.now();
    $("#rec-play").textContent = "▶ Devam";
    return;
  }
  if (Date.now() - rec.pausedAt > RESUME_AFTER_MS) return play(Math.floor(clockSec()));
  video.play().catch(() => {});
  $("#rec-play").textContent = "⏸ Duraklat";
});

$("#rec-speed").addEventListener("click", (e) => {
  const speed = Number(e.target.dataset?.speed);
  if (!speed || speed === rec.speed) return;
  if (rec.startSec === null) {
    rec.speed = speed;
    document.querySelectorAll("#rec-speed .chip").forEach((c) => c.classList.toggle("on", c === e.target));
    return;
  }
  play(Math.floor(clockSec()), speed);
});

$("#rec-goto-btn").addEventListener("click", () => {
  const v = $("#rec-goto").value;
  if (v) play(toSec(v));
});

$("#rec-branch").addEventListener("change", () => { fillChannels(); loadSpans(); });
$("#rec-channel").addEventListener("change", loadSpans);
$("#rec-date").addEventListener("change", loadSpans);

// ------------------------------------------------------------------ dışa aktarma

$("#exp-mark-start").addEventListener("click", () => {
  if (rec.startSec === null) return toast("Önce bir kayıt oynatın");
  $("#exp-start").value = hms(clockSec());
  renderRange();
});
$("#exp-mark-end").addEventListener("click", () => {
  if (rec.startSec === null) return toast("Önce bir kayıt oynatın");
  $("#exp-end").value = hms(clockSec());
  renderRange();
});
$("#exp-start").addEventListener("input", renderRange);
$("#exp-end").addEventListener("input", renderRange);

$("#exp-go").addEventListener("click", async () => {
  const s = $("#exp-start").value, e = $("#exp-end").value;
  if (!s || !e) return toast("Başlangıç ve bitiş saatini seçin");
  try {
    await api("POST", "/api/exports", {
      branch: $("#rec-branch").value, channel: Number($("#rec-channel").value),
      start: isoAt(toSec(s)), end: isoAt(toSec(e)), format: $("#exp-format").value,
    });
    toast("Dışa aktarma başladı");
    loadJobs();
  } catch (err) {
    toast(err.message);
  }
});

const STATUS = { "sırada": "", "çalışıyor": "warn", "hazır": "ok", "hata": "bad" };

async function loadJobs() {
  clearTimeout(rec.jobsTimer);
  let jobs = [];
  try {
    jobs = (await api("GET", "/api/exports")).jobs;
  } catch {
    return;
  }
  $("#exp-jobs").innerHTML = jobs.map((j) => `
    <div class="card">
      <div class="card-head">
        <h3>${esc(j.branch)} · ${esc(j.channel)}</h3>
        <span class="muted">${esc(j.start)} – ${esc(j.end.slice(11))} · ${j.format === "h264" ? "H.264" : "Orijinal H.265"}</span>
        <span class="badge ${STATUS[j.status]}">${esc(j.status)}${j.status === "çalışıyor" ? ` %${j.progress}` : ""}</span>
        <span class="spacer"></span>
        ${j.status === "hazır" ? `<a class="btn" href="/api/exports/${j.id}/file" download>İndir (${(j.size / 1048576).toFixed(1)} MB)</a>` : ""}
        <button class="small danger" data-del="${j.id}">${j.status === "çalışıyor" || j.status === "sırada" ? "İptal" : "Sil"}</button>
      </div>
      ${j.status === "çalışıyor" ? `<div class="progress"><div style="width:${j.progress}%"></div></div>` : ""}
      ${j.error ? `<p class="muted" style="margin:8px 0 0">${esc(j.error)}</p>` : ""}
      ${j.sha256 ? `<p style="margin:8px 0 0"><span class="muted">SHA-256:</span> <code class="hash">${j.sha256}</code></p>` : ""}
    </div>`).join("");
  if (jobs.some((j) => j.status === "sırada" || j.status === "çalışıyor") && !$("#tab-recordings").hidden) {
    rec.jobsTimer = setTimeout(loadJobs, 2000);
  }
}

$("#exp-jobs").addEventListener("click", (e) => {
  const id = e.target.dataset?.del;
  if (!id) return;
  confirmClick(e.target, async () => {
    try {
      await api("DELETE", `/api/exports/${id}`);
      loadJobs();
    } catch (err) {
      toast(err.message);
    }
  });
});

// Başka sekmeye geçilince akış kapansın (DVR ve cihaz boşuna çalışmasın).
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.tab !== "recordings" && rec.startSec !== null) stopPlayback("Zaman çizelgesinde yeşil bir alana tıklayın.");
}));

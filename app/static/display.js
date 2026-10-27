"use strict";

// Kiosk ekranı: aktif listeleri grid olarak gösterir, rotate_seconds aralıkla döndürür.
// Bir sonraki liste geçişten birkaç saniye önce arka planda açılır, böylece akışlar hazır olur.

const PRELOAD_MS = 4000;
const POLL_MS = 10000;
const FULL_RELOAD_MS = 6 * 3600 * 1000; // uzun süre açık kalan tarayıcıda bellek birikmesine karşı

let cfg = null;
let idx = 0;
let current = null;
let timers = [];

const msg = document.getElementById("msg");
const hud = document.getElementById("hud");

function gridDims(grid, n) {
  if (grid === "auto") {
    const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
    return [cols, Math.max(1, Math.ceil(n / cols))];
  }
  return grid.split("x").map(Number);
}

// Tarayıcının çözebildiği video codec'leri; go2rtc bunlardan biriyle fMP4 gönderir.
const CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033", "avc1.42E01E", "hvc1.1.6.L153.B0"]
  .filter((c) => window.MediaSource && MediaSource.isTypeSupported(`video/mp4; codecs="${c}"`));

// go2rtc WebSocket + MSE oynatıcı: ses yok, kontrol yok, kopunca yeniden bağlanır.
class Player {
  constructor(tile, stream) {
    this.tile = tile;
    this.stream = stream;
    this.video = document.createElement("video");
    this.video.muted = true;
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.status = document.createElement("div");
    this.status.className = "status";
    tile.append(this.video, this.status);
    this.video.addEventListener("error", () => {
      const e = this.video.error;
      console.warn(`[oynatici] ${this.stream} video hatası: ${e?.code} ${e?.message}`);
      this.setStatus(`Oynatma hatası: ${e?.message || e?.code}`);
    });
    this.lastTime = -1;
    this.stalled = 0;
    this.watchdog = setInterval(() => this.check(), 5000);
    this.connect();
  }

  setStatus(text) {
    this.status.textContent = text;
    this.status.hidden = !text;
  }

  connect() {
    if (this.closed) return;
    this.setStatus("Bağlanıyor…");
    const ms = new MediaSource();
    this.video.src = URL.createObjectURL(ms);
    ms.addEventListener("sourceopen", () => {
      URL.revokeObjectURL(this.video.src);
      const ws = (this.ws = new WebSocket(`ws://${location.hostname}:${cfg.go2rtc_port}/api/ws?src=${encodeURIComponent(this.stream)}`));
      ws.binaryType = "arraybuffer";
      let sb = null;
      const queue = [];
      ws.onopen = () => ws.send(JSON.stringify({ type: "mse", value: CODECS.join(",") }));
      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          const msg = JSON.parse(ev.data);
          console.info(`[oynatici] ${this.stream} ${msg.type}: ${msg.value}`);
          if (msg.type === "mse") {
            sb = ms.addSourceBuffer(msg.value);
            sb.mode = "segments";
            sb.addEventListener("updateend", () => {
              if (queue.length) sb.appendBuffer(queue.shift());
              else this.trim(sb);
            });
          } else if (msg.type === "error") {
            this.fail(/codecs|not matched/i.test(msg.value)
              ? "Codec desteklenmiyor (alt akış H.264 olmalı)"
              : "Kameraya ulaşılamıyor — yeniden denenecek");
          }
          return;
        }
        if (!sb) return;
        if (sb.updating || queue.length) {
          if (queue.length < 60) queue.push(ev.data);
        } else {
          sb.appendBuffer(ev.data);
        }
        this.seekLive();
      };
      ws.onclose = () => {
        if (!this.closed && !this.failed) this.retry();
      };
    }, { once: true });
    this.video.play().catch(() => {});
  }

  seekLive() {
    const b = this.video.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    if (end - this.video.currentTime > 2) this.video.currentTime = end - 0.3;
    if (this.video.paused) this.video.play().catch(() => {});
  }

  trim(sb) {
    const b = sb.buffered;
    if (b.length && this.video.currentTime - b.start(0) > 30) sb.remove(b.start(0), this.video.currentTime - 10);
  }

  check() {
    if (this.failed) return;
    const t = this.video.currentTime;
    if (t > this.lastTime) {
      this.lastTime = t;
      this.stalled = 0;
      this.setStatus("");
    } else if (++this.stalled >= 3) {
      const b = this.video.buffered;
      console.warn(`[oynatici] ${this.stream} ilerlemiyor: t=${t} buffered=${b.length ? b.start(0) + "-" + b.end(b.length - 1) : "yok"} readyState=${this.video.readyState} paused=${this.video.paused}`);
      this.setStatus("Görüntü yok, yeniden bağlanıyor…");
      this.retry();
    }
  }

  retry() {
    this.ws?.close();
    this.stalled = 0;
    this.lastTime = -1;
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.connect(), 3000);
  }

  // Codec hatası gibi kalıcı görünen durumlarda da dakikada bir yeniden dener (DVR ayarı düzeltilmiş olabilir).
  fail(text) {
    this.failed = true;
    this.ws?.close();
    this.setStatus(text);
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.failed = false;
      this.connect();
    }, 60000);
  }

  destroy() {
    this.closed = true;
    clearInterval(this.watchdog);
    clearTimeout(this.retryTimer);
    this.ws?.close();
    this.video.removeAttribute("src");
    this.video.load();
  }
}

function build(list) {
  const [cols, rows] = gridDims(list.grid, list.tiles.length);
  const el = document.createElement("div");
  el.className = "screen pending";
  el.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  el.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
  el.players = [];
  for (let i = 0; i < cols * rows; i++) {
    const t = list.tiles[i];
    const tile = document.createElement("div");
    tile.className = t ? "tile" : "tile empty";
    if (t) {
      el.players.push(new Player(tile, t.stream));
      const label = document.createElement("div");
      label.className = "label";
      label.textContent = t.label;
      tile.append(label);
    }
    el.append(tile);
  }
  document.body.append(el);
  return el;
}

function destroyScreen(el) {
  el.players.forEach((p) => p.destroy());
  el.remove();
}

function reveal(next) {
  next.classList.remove("pending");
  const old = current;
  current = next;
  if (old) setTimeout(() => destroyScreen(old), 500);
  updateHud();
}

function updateHud() {
  if (!cfg || !cfg.lists.length) return;
  const n = cfg.lists.length;
  const time = new Date().toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  hud.textContent = `${cfg.lists[idx].name}${n > 1 ? ` (${idx + 1}/${n})` : ""} · ${time}`;
  hud.hidden = false;
}

function cycle() {
  const ms = cfg.rotate_seconds * 1000;
  const preload = Math.min(PRELOAD_MS, ms / 2);
  timers.push(setTimeout(() => {
    idx = (idx + 1) % cfg.lists.length;
    const next = build(cfg.lists[idx]);
    timers.push(setTimeout(() => { reveal(next); cycle(); }, preload));
  }, ms - preload));
}

function start() {
  timers.forEach(clearTimeout);
  timers = [];
  document.querySelectorAll(".screen").forEach(destroyScreen);
  current = null;
  idx = 0;
  if (!cfg.lists.length) {
    msg.textContent = "Gösterilecek aktif liste yok. Yönetim panelinden bir liste oluşturun.";
    msg.hidden = false;
    hud.hidden = true;
    return;
  }
  msg.hidden = true;
  reveal(build(cfg.lists[0]));
  if (cfg.lists.length > 1) cycle();
}

async function load() {
  try {
    const res = await fetch("/api/display", { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    cfg = await res.json();
    start();
  } catch (err) {
    msg.textContent = "Sunucuya bağlanılamadı, tekrar deneniyor…";
    msg.hidden = false;
    setTimeout(load, 5000);
  }
}

async function poll() {
  try {
    const res = await fetch("/api/version", { cache: "no-store" });
    const { hash } = await res.json();
    if (cfg && hash !== cfg.hash) await load();
  } catch {
    // sunucu geçici olarak kapalıysa mevcut görüntü kalmaya devam eder
  }
}

// Onaylanmamış alarmlar ekranın altında kırmızı bant olarak görünür.
async function pollAlarms() {
  const el = document.getElementById("alarms");
  try {
    const { alarms } = await (await fetch("/api/alarms/active", { cache: "no-store" })).json();
    el.hidden = !alarms.length;
    el.innerHTML = alarms.length ? `<b>⚠ ${alarms.length} ALARM</b>` + alarms.map((a) => a.replace(/[&<>]/g, "")).join("  ·  ") : "";
  } catch {
    // sunucuya ulaşılamazsa son durum kalır
  }
}

load();
pollAlarms();
setInterval(poll, POLL_MS);
setInterval(pollAlarms, 30000);
setInterval(updateHud, 15000);
setTimeout(() => location.reload(), FULL_RELOAD_MS);

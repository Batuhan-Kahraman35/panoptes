"""Kayıtlar: DVR'daki kayıtları arama, tarayıcıda oynatma ve MP4 olarak dışa aktarma.

Oynatma akışı (/Streaming/tracks/<kanal>01?starttime=...) Hik-Connect akış şifrelemesi açıkken de şifresiz geliyor
(DVR'lerde ve NVR'de doğrulandı). Kayıtlar H.265 olduğu için tarayıcıya ffmpeg ile H.264'e çevrilip parçalı MP4
olarak akıtılır. PLAY isteğine 'Scale: N' eklenince DVR kaydı tüm kareleriyle N kat hızlı gönderiyor (16x'e kadar
denendi); hızlı oynatma ve dışa aktarma bunu kullanır.

Kayıtlar sabit kare hızında değil (40 ve 80 ms aralıklar karışık; ayardaki fps'e güvenmek saati saatte dakikalarca
kaydırıyor). Bu yüzden kareler ffmpeg'e ham akış olarak değil, RTP zaman damgalarıyla MPEG-TS içinde verilir;
RTP zaman damgaları Scale'de de kaydın kendi zamanını gösteriyor.
"""
import datetime as dt
import hashlib
import json
import re
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

import hikstream
import isapi

FMT_SEARCH = "%Y-%m-%dT%H:%M:%SZ"
FMT_URI = "%Y%m%dT%H%M%SZ"  # Hikvision 'Z' yazar ama cihaz yerel saati olarak yorumlar
START = b"\x00\x00\x00\x01"
TS_INPUT = ["-f", "mpegts", "-probesize", "500000", "-analyzeduration", "1000000", "-i", "pipe:0"]
KEEPALIVE_SEC = 25
PLAYBACK_SPEEDS = (1, 2, 4)
MAX_PLAYBACKS = 3
EXPORT_MAX = dt.timedelta(hours=2)
EXPORT_KEEP_SEC = 24 * 3600
FMP4 = ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof"]


class KayitError(Exception):
    pass


class SpeedError(KayitError):
    """Cihaz istenen Scale değerini kabul etmedi (ör. NVR en fazla 8x)."""


def channel(b, no):
    c = next((c for c in b.get("channels", []) if c["no"] == no), None)
    if not c:
        raise KayitError("Kanal bulunamadı")
    return c


def display_aspect(c):
    """DVR'ın yarım genişlik modları (ör. 960x1080) 16:9 görüntüyü dar piksellerle kaydeder; oynatıcıya bildirilir."""
    try:
        w, h = map(int, c["main"]["res"].split("x"))
    except (KeyError, AttributeError, ValueError):
        return []
    return ["-aspect", "16:9"] if w < h else []


def channel_fps(c):
    try:
        return max(1, min(60, int(c["main"]["fps"])))
    except (KeyError, TypeError, ValueError):
        return 25


# ---------------------------------------------------------------- arama

def search(b, no, start, end):
    """[start, end) içindeki kayıtlı aralıklar: [(başlangıç, bitiş)], birleştirilmiş ve aralığa kırpılmış."""
    cli = isapi.Isapi(b["ip"], b["http_port"], b["username"], b["password"], timeout=20)
    sid, pos, spans = uuid.uuid4(), 0, []
    while pos < 2000:
        body = (f'<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription><searchID>{sid}</searchID>'
                f"<trackIDList><trackID>{no * 100 + 1}</trackID></trackIDList><timeSpanList><timeSpan>"
                f"<startTime>{start.strftime(FMT_SEARCH)}</startTime><endTime>{end.strftime(FMT_SEARCH)}</endTime>"
                f"</timeSpan></timeSpanList><maxResults>50</maxResults><searchResultPostion>{pos}</searchResultPostion>"
                "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
                "</CMSearchDescription>")
        try:
            raw = cli.request("/ISAPI/ContentMgmt/search", "POST", body.encode()).decode("utf-8", "ignore")
        except isapi.IsapiError as e:
            raise KayitError(f"DVR'da arama yapılamadı: {e}")
        found = re.findall(r"<startTime>([^<]+)</startTime>\s*<endTime>([^<]+)</endTime>", raw)
        for s, e in found:
            spans.append((dt.datetime.fromisoformat(s[:19]), dt.datetime.fromisoformat(e[:19])))
        if "MORE" not in raw or not found:
            break
        pos += len(found)
    merged = []
    for s, e in sorted(spans):
        s, e = max(s, start), min(e, end)
        if e <= s:
            continue
        if merged and s <= merged[-1][1] + dt.timedelta(seconds=2):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


# ---------------------------------------------------------------- RTSP oynatma akışı

class Depacketizer:
    """RTP yüklerinden H.264 / H.265 NAL birimlerini çıkarır."""

    def __init__(self, h265):
        self.h265 = h265
        self.fu = bytearray()

    def nals(self, p):
        out = []
        if self.h265:
            kind = (p[0] >> 1) & 0x3F
            if kind == 49 and len(p) > 3:  # FU
                fh = p[2]
                if fh & 0x80:
                    self.fu = bytearray([(p[0] & 0x81) | ((fh & 0x3F) << 1), p[1]])
                self.fu += p[3:]
                if fh & 0x40:
                    out.append(bytes(self.fu))
            elif kind == 48:  # AP
                i = 2
                while i + 2 <= len(p):
                    n = struct.unpack(">H", p[i:i + 2])[0]
                    out.append(p[i + 2:i + 2 + n])
                    i += 2 + n
            else:
                out.append(p)
        else:
            kind = p[0] & 0x1F
            if 1 <= kind <= 23:
                out.append(p)
            elif kind == 24:  # STAP-A
                i = 1
                while i + 2 <= len(p):
                    n = struct.unpack(">H", p[i:i + 2])[0]
                    out.append(p[i + 2:i + 2 + n])
                    i += 2 + n
            elif kind == 28 and len(p) > 2:  # FU-A
                if p[1] & 0x80:
                    self.fu = bytearray([(p[0] & 0xE0) | (p[1] & 0x1F)])
                self.fu += p[2:]
                if p[1] & 0x40:
                    out.append(bytes(self.fu))
        return [n for n in out if len(n) > 1]


def _crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


class TsWriter:
    """Tek görüntü akışlı en küçük MPEG-TS yazıcısı: her kare kendi PTS'iyle bir PES paketi."""
    PMT_PID, VIDEO_PID = 0x1000, 0x100

    def __init__(self, write, h265):
        self.write = write
        self.cc = {}
        self.frames = 0
        pat = bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                     0xE0 | (self.PMT_PID >> 8), self.PMT_PID & 0xFF])
        pmt = bytes([0x02, 0xB0, 18, 0x00, 0x01, 0xC1, 0x00, 0x00,
                     0xE0 | (self.VIDEO_PID >> 8), self.VIDEO_PID & 0xFF, 0xF0, 0x00,
                     0x24 if h265 else 0x1B, 0xE0 | (self.VIDEO_PID >> 8), self.VIDEO_PID & 0xFF, 0xF0, 0x00])
        self.psi = (self._packets(0, b"\x00" + pat + struct.pack(">I", _crc32_mpeg(pat)))
                    + self._packets(self.PMT_PID, b"\x00" + pmt + struct.pack(">I", _crc32_mpeg(pmt))))

    def _packets(self, pid, payload, pcr=None):
        out, first = bytearray(), True
        while True:
            cc = self.cc.get(pid, 0)
            self.cc[pid] = (cc + 1) & 0x0F
            content = b""
            if first and pcr is not None:
                content = b"\x10" + struct.pack(">IH", (pcr >> 1) & 0xFFFFFFFF, ((pcr & 1) << 15) | 0x7E00)
            room = 184 - (1 + len(content) if content else 0)
            chunk, payload = payload[:room], payload[room:]
            need = 184 - len(chunk)  # uyarlama alanının kaplayacağı bayt (dolgu dahil)
            if need == 0:
                af = b""
            elif need == 1 and not content:
                af = b"\x00"
            else:
                content = content or b"\x00"
                af = bytes([need - 1]) + content + b"\xff" * (need - 1 - len(content))
            out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, (0x30 if af else 0x10) | cc])
            out += af + chunk
            first = False
            if not payload:
                return bytes(out)

    def frame(self, data, pts):
        if self.frames % 40 == 0:
            self.write(self.psi)
        self.frames += 1
        pts &= (1 << 33) - 1
        header = bytes([0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05,
                        0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1,
                        (pts >> 7) & 0xFF, ((pts << 1) & 0xFE) | 1])
        self.write(self._packets(self.VIDEO_PID, header + data, pcr=pts))


class Stream:
    """DVR'dan [start, end] kaydını RTSP ile çeker; scale > 1 ise o kadar hızlı."""

    def __init__(self, b, no, start, end, scale=1):
        self.url = (f"rtsp://{b['ip']}:{b['rtsp_port']}/Streaming/tracks/{no * 100 + 1}"
                    f"?starttime={start.strftime(FMT_URI)}&endtime={end.strftime(FMT_URI)}")
        try:
            self.rtsp = hikstream.Rtsp(b["ip"], b["rtsp_port"], b["username"], b["password"])
        except OSError as e:
            raise KayitError(f"DVR'a bağlanılamadı: {e}")
        try:
            headers, sdp = self.rtsp.request("DESCRIBE", self.url, ["Accept: application/sdp"])
            video = sdp.split("m=video", 1)[1].split("\nm=", 1)[0]
            self.h265 = bool(re.search(r"a=rtpmap:\d+ (H265|HEVC)", video, re.I))
            base = headers.get("content-base", self.url.split("?")[0] + "/").rstrip("/") + "/"
            control = re.search(r"a=control:(\S+)", video).group(1)
            track = control if control.startswith("rtsp://") else base + control
            headers, _ = self.rtsp.request("SETUP", track, ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
            self.rtsp.session = headers.get("session", "").split(";")[0]
        except (ConnectionError, IndexError, AttributeError, OSError) as e:
            self.close()
            raise KayitError("Bu saatte kayıt yok" if "RTSP 4" in str(e) else f"Kayıt açılamadı: {e}")
        try:
            self.rtsp.request("PLAY", self.url, ["Range: npt=0.000-"] + ([f"Scale: {scale:.1f}"] if scale != 1 else []))
        except (ConnectionError, OSError) as e:
            self.close()
            if scale != 1 and "RTSP 4" in str(e):
                raise SpeedError(f"Bu cihaz {scale}x hızı desteklemiyor")
            raise KayitError("Bu saatte kayıt yok" if "RTSP 4" in str(e) else f"Kayıt açılamadı: {e}")
        self.rtsp.sock.settimeout(20)
        self.dep = Depacketizer(self.h265)

    def pump(self, write, stop, speed=1, max_seconds=None, on_progress=None):
        """Kareleri RTP zaman damgalarıyla MPEG-TS olarak write'a verir. speed > 1 ise zaman damgaları o kadar
        sıkıştırılır (izleme süresi kısalır). Akış bitince, stop kurulunca ya da max_seconds kadar kayıt geçince
        döner; geçen kayıt süresini (saniye) döner."""
        ts_out = TsWriter(write, self.h265)
        au, au_ts, first, elapsed = [], None, None, 0.0
        last_keepalive = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last_keepalive > KEEPALIVE_SEC:
                self.keepalive()
                last_keepalive = time.monotonic()
            try:
                chn, pkt = self.rtsp.read_frame()
            except (OSError, ConnectionError):
                break  # DVR akışı bitirdi ya da bağlantı koptu
            if chn != 0 or len(pkt) < 12:
                continue
            p = hikstream.rtp_payload(pkt)
            if not p:
                continue
            ts = struct.unpack(">I", pkt[4:8])[0]
            if ts != au_ts and au:
                # önceki kare tamam: aynı RTP zaman damgasını taşıyan NAL'lar bir kareyi oluşturur
                if first is None:
                    first = au_ts
                rel = (au_ts - first) & 0xFFFFFFFF  # 90 kHz; 32 bit taşması 13 saatte bir
                elapsed = rel / 90000
                if max_seconds is not None and elapsed >= max_seconds:
                    return elapsed
                ts_out.frame(b"".join(au), rel // speed)
                if on_progress:
                    on_progress(elapsed)
                au = []
            au_ts = ts
            au += [START + nal for nal in self.dep.nals(p)]
        return elapsed

    def keepalive(self):
        """Oturumu canlı tutar; yanıt beklenmez (read_frame araya giren RTSP yanıtlarını atlar).
        NVR kimlik doğrulamasız isteğe akışı keserek karşılık veriyor, bu yüzden Digest başlığı eklenir."""
        r = self.rtsp
        r.cseq += 1
        lines = [f"GET_PARAMETER {self.url} RTSP/1.0", f"CSeq: {r.cseq}", f"Session: {r.session}"]
        if r.challenge:
            r.nc += 1
            lines.append("Authorization: " + isapi.auth_header(r.challenge, r.username, r.password,
                                                               "GET_PARAMETER", self.url, r.nc))
        r.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def close(self):
        try:
            self.rtsp.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- tarayıcıda oynatma

_playback_slots = threading.BoundedSemaphore(MAX_PLAYBACKS)


def playback(b, no, start, speed, out):
    """Kaydı start'tan itibaren H.264 parçalı MP4 olarak out'a (dosya benzeri) akıtır. İstemci kopunca biter."""
    if speed not in PLAYBACK_SPEEDS:
        raise KayitError("Geçersiz hız")
    fps = channel_fps(channel(b, no))
    if not _playback_slots.acquire(blocking=False):
        raise KayitError(f"Aynı anda en fazla {MAX_PLAYBACKS} kayıt izlenebilir")
    try:
        stream = Stream(b, no, start, start + dt.timedelta(hours=6), scale=speed)
    except KayitError:
        _playback_slots.release()
        raise
    # Zaman damgaları hızla sıkıştırılmış gelir; fps filtresi hızlı oynatmada fazla kareleri atar.
    proc = subprocess.Popen(
        ["nice", "-n", "10", "ffmpeg", "-v", "error", *TS_INPUT, "-an", "-vf", f"fps={fps},scale=1280:720",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", str(fps * 2),
         "-pix_fmt", "yuv420p", *FMP4, "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stop = threading.Event()

    def feed():
        try:
            stream.pump(proc.stdin.write, stop, speed=speed)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            stream.close()
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        while chunk := proc.stdout.read1(65536):
            out.write(chunk)
            out.flush()
    except (BrokenPipeError, ConnectionError, OSError):
        pass  # tarayıcı başka ana geçti ya da sayfayı kapattı
    finally:
        stop.set()
        stream.close()
        proc.kill()
        proc.wait()
        _playback_slots.release()


# ---------------------------------------------------------------- dışa aktarma

def _safe(text):
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    return re.sub(r"[^A-Za-z0-9]+", "_", text.translate(tr)).strip("_")


class Exports:
    """Dışa aktarma işleri: sırayla, tek tek çalışır. Dosyalar ve iş kayıtları klasörde 24 saat durur."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.jobs = {}
        self.queue = []
        self.wake = threading.Event()
        self.folder.mkdir(parents=True, exist_ok=True)
        for f in self.folder.glob("*.json"):
            try:
                job = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if job["status"] in ("sırada", "çalışıyor"):
                job.update(status="hata", error="Cihaz yeniden başladığı için yarıda kaldı")
            self.jobs[job["id"]] = job
        threading.Thread(target=self._worker, daemon=True).start()

    def _save(self, job):
        (self.folder / f"{job['id']}.json").write_text(json.dumps(job, ensure_ascii=False), "utf-8")

    def _cleanup(self):
        now = time.time()
        for job in list(self.jobs.values()):
            if job["status"] in ("hazır", "hata") and now - job["created"] > EXPORT_KEEP_SEC:
                self._delete(job["id"])

    def list(self):
        with self.lock:
            self._cleanup()
            return sorted(self.jobs.values(), key=lambda j: -j["created"])

    def file(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if not job or job["status"] != "hazır":
            raise KayitError("Dosya hazır değil")
        return self.folder / job["file"], job["file"]

    def delete(self, job_id):
        with self.lock:
            self._delete(job_id)

    def _delete(self, job_id):
        job = self.jobs.pop(job_id, None)
        if not job:
            return
        job["cancel"] = True  # çalışıyorsa _run bir sonraki karede durur
        for name in (job.get("file"), f"{job_id}.json", f"{job_id}.part.mp4", f"{job_id}.log"):
            if name:
                (self.folder / name).unlink(missing_ok=True)

    def start(self, b, no, start, end, fmt):
        c = channel(b, no)
        if fmt not in ("h264", "orijinal"):
            raise KayitError("Geçersiz biçim")
        if end <= start:
            raise KayitError("Bitiş başlangıçtan sonra olmalı")
        if end - start > EXPORT_MAX:
            raise KayitError("En fazla 2 saatlik kayıt dışa aktarılabilir")
        if not search(b, no, start, end):
            raise KayitError("Bu aralıkta kayıt yok")
        job_id = uuid.uuid4().hex[:12]
        name = f"{_safe(b['name'])}_{_safe(c['name'])}_{start:%Y-%m-%d_%H%M%S}-{end:%H%M%S}"
        job = {"id": job_id, "created": time.time(), "branch": b["name"], "channel": c["name"],
               "start": start.isoformat(sep=" "), "end": end.isoformat(sep=" "), "format": fmt,
               "status": "sırada", "progress": 0, "file": f"{name}{'_H265' if fmt == 'orijinal' else ''}.mp4",
               "size": 0, "sha256": "", "error": ""}
        with self.lock:
            self._cleanup()
            self.jobs[job_id] = job
            self.queue.append((job_id, b, no, start, end, display_aspect(c)))
            self._save(job)
        self.wake.set()
        return job

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                item = self.queue.pop(0) if self.queue else None
                if not self.queue:
                    self.wake.clear()
            if item and item[0] in self.jobs:
                self._run(*item)

    def _run(self, job_id, b, no, start, end, aspect):
        job = self.jobs[job_id]
        part = self.folder / f"{job_id}.part.mp4"
        log = self.folder / f"{job_id}.log"
        job["status"] = "çalışıyor"
        self._save(job)
        total = (end - start).total_seconds()
        stop = threading.Event()
        proc = stream = None
        try:
            # Orijinal: görüntü verisi değiştirilmeden MP4'e konur (DVR'ın gönderebildiği en hızlı biçimde).
            # H.264: bu cihazda çevirme gerçek zamanın ~4 katı; DVR'ı ondan çok zorlamaya gerek yok.
            original = job["format"] == "orijinal"
            for scale in (16, 8, 4, 2, 1) if original else (8, 4, 2, 1):
                try:
                    stream = Stream(b, no, start, end, scale=scale)
                    break
                except SpeedError:
                    continue
            codec = ["-c:v", "copy", "-tag:v", "hvc1" if stream.h265 else "avc1"] if original else \
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
            proc = subprocess.Popen(
                ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y", *TS_INPUT,
                 "-an", *codec, *aspect, "-fps_mode", "passthrough", "-movflags", "+faststart", str(part)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(log, "wb"))

            def progress(sec):
                job["progress"] = min(99, int(sec * 100 / total))
                if job.get("cancel"):
                    stop.set()

            got = stream.pump(proc.stdin.write, stop, max_seconds=total, on_progress=progress)
            stream.close()
            proc.stdin.close()
            code = proc.wait()
            err = log.read_text("utf-8", "ignore") if log.exists() else ""
            if code != 0 or job.get("cancel"):
                raise KayitError("İptal edildi" if job.get("cancel") else f"ffmpeg hatası: {err.strip()[-200:]}")
            if got < total * 0.5:
                raise KayitError(f"DVR kaydın sadece %{int(got * 100 / total)} kadarını gönderdi")
            sha = hashlib.sha256()
            with open(part, "rb") as f:
                while chunk := f.read(1 << 20):
                    sha.update(chunk)
            part.replace(self.folder / job["file"])
            job.update(status="hazır", progress=100, size=(self.folder / job["file"]).stat().st_size,
                       sha256=sha.hexdigest())
        except (KayitError, OSError) as e:
            job.update(status="hata", error=str(e))
            part.unlink(missing_ok=True)
        except Exception as e:  # beklenmeyen hata işi "çalışıyor"da bırakmasın
            job.update(status="hata", error=f"Beklenmeyen hata: {e}")
            part.unlink(missing_ok=True)
        finally:
            stop.set()
            if stream:
                stream.close()
            if proc and proc.poll() is None:
                proc.kill()
            log.unlink(missing_ok=True)
            if job_id in self.jobs:
                self._save(job)

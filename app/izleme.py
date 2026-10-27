"""Şube DVR'lerini düzenli kontrol eder ve alarm üretir.

Kontroller: DVR erişilemiyor, disk hatası, kayıt yok (son kayıt DVR saatine göre eski), görüntü yok
(anlık görüntü neredeyse tamamen koyu: Hikvision'ın "NO VIDEO" ekranı; NVR'de kanal çevrimdışı).
Bir sorun art arda CONFIRM_RUNS kontrolde görülünce alarm olur; düzelince kapanır. Yeni ve kapanan
alarmlar e-postayla bildirilir. Durum DATA/alarms.json'da tutulur.
"""
import datetime as dt
import io
import json
import os
import re
import smtplib
import threading
import time
import uuid
from email.message import EmailMessage

import isapi

try:
    from PIL import Image
except ImportError:  # Pillow yoksa görüntü kontrolü atlanır
    Image = None

CONFIRM_RUNS = 2
DARK_LEVEL = 35        # bu değerin altındaki pikseller "koyu"
DARK_RATIO = 0.85      # görüntünün bu kadarı koyuysa görüntü yok sayılır
HISTORY_MAX = 200

DEFAULTS = {
    "interval_min": 5,
    "record_gap_min": 20,
    "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
              "username": "", "password": "", "sender": "", "to": []},
}


def log(*args):
    print(time.strftime("%H:%M:%S"), "izleme:", *args, flush=True)


def _dvr_now(cli):
    """DVR'ın kendi saati (kayıt zamanları bu saate göre)."""
    try:
        raw = cli.request("/ISAPI/System/time").decode("utf-8", "ignore")
        m = re.search(r"<localTime>(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", raw)
        if m:
            return dt.datetime.fromisoformat(m.group(1))
    except isapi.IsapiError:
        pass
    return dt.datetime.now()


def _last_record_end(cli, no, now):
    fmt = "%Y-%m-%dT%H:%M:%SZ"  # Hikvision 'Z' ile yazar ama cihaz yerel saati olarak yorumlar
    body = (f'<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription><searchID>{uuid.uuid4()}</searchID>'
            f"<trackIDList><trackID>{no * 100 + 1}</trackID></trackIDList><timeSpanList><timeSpan>"
            f"<startTime>{(now - dt.timedelta(hours=2)).strftime(fmt)}</startTime><endTime>{now.strftime(fmt)}</endTime>"
            "</timeSpan></timeSpanList><maxResults>50</maxResults><searchResultPostion>0</searchResultPostion>"
            "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
            "</CMSearchDescription>")
    raw = cli.request("/ISAPI/ContentMgmt/search", "POST", body.encode()).decode("utf-8", "ignore")
    ends = [dt.datetime.fromisoformat(e[:19]) for e in re.findall(r"<endTime>([^<]+)<", raw)]
    return max(ends) if ends else None


def _is_dark(jpeg):
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    img.thumbnail((160, 120))
    hist = img.histogram()
    return sum(hist[:DARK_LEVEL]) / max(1, sum(hist)) >= DARK_RATIO


def check_branch(b, cfg):
    """Şubedeki sorunları döner: {anahtar: metin}."""
    bid, name = b["id"], b["name"]
    cli = isapi.Isapi(b["ip"], b["http_port"], b["username"], b["password"], timeout=10)
    try:
        cli.device_info()
    except isapi.AuthError as e:
        return {f"{bid}:offline": f"{name}: DVR'a giriş yapılamıyor ({e})"}
    except isapi.IsapiError:
        return {f"{bid}:offline": f"{name}: DVR'a ulaşılamıyor (kapalı veya internet yok)"}

    problems = {}
    chan_status = {}
    try:
        ws = json.loads(cli.request("/ISAPI/System/workingstatus?format=json")).get("WorkingStatus", {})
        bad = [h for h in ws.get("HDStatus", []) if h.get("status") not in (0, None)]
        if bad:
            problems[f"{bid}:disk"] = f"{name}: disk hatası (disk {', '.join(str(h.get('hdNo')) for h in bad)})"
        chan_status = {c.get("chanNo"): c for c in ws.get("ChanStatus", [])}
    except (isapi.IsapiError, ValueError):
        pass

    now = _dvr_now(cli)
    gap = dt.timedelta(minutes=cfg["record_gap_min"])
    no_record, no_video, checked_video = [], [], 0
    off = set(b.get("disabled", []))
    channels = [c for c in b.get("channels", []) if c["no"] not in off]  # pasif kameralar kontrol edilmez
    for c in channels:
        no, cname = c["no"], c["name"]
        try:
            end = _last_record_end(cli, no, now)
            if end is None or now - end > gap:
                no_record.append((no, cname, end))
        except isapi.IsapiError:
            pass
        st = chan_status.get(no)
        if st is not None and st.get("online") == 0:
            no_video.append((no, cname, "çevrimdışı"))
            checked_video += 1
            continue
        if Image is None:
            continue
        try:
            if _is_dark(cli.snapshot(no)):
                no_video.append((no, cname, "görüntü yok"))
            checked_video += 1
        except isapi.NotSupported:
            pass
        except (isapi.IsapiError, OSError):
            no_video.append((no, cname, "görüntü alınamadı"))
            checked_video += 1

    if channels and len(no_record) == len(channels):
        problems[f"{bid}:record"] = f"{name}: hiçbir kamera kayıt yapmıyor"
    else:
        for no, cname, end in no_record:
            last = f"son kayıt {end:%d.%m %H:%M}" if end else "son 2 saatte kayıt yok"
            problems[f"{bid}:{no}:record"] = f"{name} · {cname}: kayıt yapmıyor ({last})"
    if checked_video and len(no_video) == len(channels):
        problems[f"{bid}:video"] = f"{name}: tüm kameralarda görüntü yok"
    else:
        for no, cname, why in no_video:
            problems[f"{bid}:{no}:video"] = f"{name} · {cname}: {why}"
    return problems


class Monitor:
    def __init__(self, store, data_dir):
        self.store = store
        self.path = os.path.join(data_dir, "alarms.json")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.running = False
        self.last_run = None
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("active", {})
        self.state.setdefault("pending", {})
        self.state.setdefault("history", [])

    def settings(self):
        with self.store.lock:
            s = self.store.data["settings"].setdefault("alarms", json.loads(json.dumps(DEFAULTS)))
            s.setdefault("email", {})
            for k, v in DEFAULTS.items():
                s.setdefault(k, v)
            for k, v in DEFAULTS["email"].items():
                s["email"].setdefault(k, v)
            return json.loads(json.dumps(s))

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def public(self):
        with self.lock:
            active = [{"key": k, **v} for k, v in sorted(self.state["active"].items(), key=lambda kv: kv[1]["since"])]
            return {"active": active, "history": self.state["history"][:100],
                    "last_run": self.last_run, "running": self.running}

    def ack(self, key):
        with self.lock:
            if key in self.state["active"]:
                self.state["active"][key]["acked"] = True
                self._save()

    def drop(self, prefix, note):
        """Pasif yapılan kameranın alarmlarını 'düzeldi' bildirimi göndermeden kapatır."""
        with self.lock:
            gone = [k for k in self.state["active"] if k.startswith(prefix)]
            for k in gone:
                del self.state["active"][k]
            self.state["pending"] = {k: v for k, v in self.state["pending"].items() if not k.startswith(prefix)}
            self.state["history"].insert(0, {"time": time.strftime("%Y-%m-%d %H:%M"), "event": "pasif", "text": note})
            self._save()

    def run_once(self):
        cfg = self.settings()
        with self.store.lock:
            branches = json.loads(json.dumps(self.store.data["branches"]))
        self.running = True
        found = {}
        for b in branches:
            try:
                found.update(check_branch(b, cfg))
            except Exception as e:  # tek şubenin hatası diğerlerini durdurmasın
                log(f"{b['name']} kontrol hatası: {e!r}")
        stamp = time.strftime("%Y-%m-%d %H:%M")
        offline = {k.split(":")[0] for k in found if k.endswith(":offline")}
        if branches and len(offline) == len(branches):
            # Hiçbir DVR'a ulaşılamıyor: sorun büyük ihtimalle mini PC'nin kendi ağında (örn. açılışta
            # bağlantı henüz gelmemiş). Alarm üretme ve mevcut alarmları kapatma.
            self.running = False
            self.last_run = stamp
            log("hiçbir DVR'a ulaşılamadı; yerel ağ sorunu varsayılıp kontrol atlandı")
            return
        new, resolved = [], []
        with self.lock:
            ids = {b["id"] for b in branches}
            pending = {k: self.state["pending"].get(k, 0) + 1 for k in found}
            for key, text in found.items():
                if key in self.state["active"]:
                    self.state["active"][key].update(text=text, missing=0)
                elif pending[key] >= CONFIRM_RUNS:
                    self.state["active"][key] = {"text": text, "since": stamp, "acked": False, "missing": 0}
                    new.append(text)
            for key in list(self.state["active"]):
                if key in found:
                    continue
                bid = key.split(":")[0]
                if bid in offline and not key.endswith(":offline"):
                    continue  # erişilemeyen şubenin disk/kayıt/görüntü durumu bilinmiyor: alarm açık kalır
                item = self.state["active"][key]
                item["missing"] = item.get("missing", 0) + 1
                if bid not in ids or item["missing"] >= CONFIRM_RUNS:
                    del self.state["active"][key]
                    if bid in ids:
                        resolved.append(item["text"])
            self.state["pending"] = {k: v for k, v in pending.items() if k not in self.state["active"]}
            for text in new:
                self.state["history"].insert(0, {"time": stamp, "event": "alarm", "text": text})
            for text in resolved:
                self.state["history"].insert(0, {"time": stamp, "event": "düzeldi", "text": text})
            del self.state["history"][HISTORY_MAX:]
            self._save()
            self.last_run = stamp
        self.running = False
        log(f"kontrol bitti: {len(found)} sorun, {len(new)} yeni alarm, {len(resolved)} düzelen")
        if new or resolved:
            try:
                self.send_email(cfg, new, resolved)
            except Exception as e:
                log(f"e-posta gönderilemedi: {e!r}")

    def send_email(self, cfg, new, resolved, test=False):
        e = cfg["email"]
        if not test and not e.get("enabled"):
            return
        if not (e.get("host") and e.get("sender") and e.get("to")):
            raise ValueError("E-posta ayarları eksik (sunucu, gönderen, alıcı)")
        msg = EmailMessage()
        if test:
            msg["Subject"] = "DVR Ekran: test e-postası"
            body = "Bu bir test e-postasıdır. Alarm bildirimleri bu adrese gelecek."
        else:
            msg["Subject"] = f"DVR alarm: {len(new)} yeni, {len(resolved)} düzelen"
            body = ""
            if new:
                body += "YENİ ALARMLAR\n" + "\n".join(f"  • {t}" for t in new) + "\n\n"
            if resolved:
                body += "DÜZELENLER\n" + "\n".join(f"  • {t}" for t in resolved) + "\n\n"
            with self.lock:
                body += f"Şu an açık alarm sayısı: {len(self.state['active'])}\n"
        msg["From"] = e["sender"]
        msg["To"] = ", ".join(e["to"])
        msg.set_content(body + "\n— DVR Ekran (tv-kamera)")
        port = int(e.get("port") or 587)
        if e.get("security") == "ssl":
            server = smtplib.SMTP_SSL(e["host"], port, timeout=20)
        else:
            server = smtplib.SMTP(e["host"], port, timeout=20)
            if e.get("security") == "starttls":
                server.starttls()
        with server:
            if e.get("username"):
                server.login(e["username"], e["password"])
            server.send_message(msg)
        log(f"e-posta gönderildi: {msg['Subject']}")

    def loop(self):
        time.sleep(90)  # açılışta ağ ve DVR bağlantıları otursun
        while True:
            try:
                self.run_once()
            except Exception as e:
                log(f"izleme hatası: {e!r}")
            self.wake.wait(self.settings()["interval_min"] * 60)
            self.wake.clear()

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

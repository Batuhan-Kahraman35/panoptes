"""DVR Ekran yönetim sunucusu.

- Yönetim paneli (/): şube ekle/test et, ekran listelerini düzenle, ayarlar.
- Kiosk ekranı (/display): aktif listeleri grid halinde gösterir, belirli aralıkla döndürür.
- go2rtc: şubelerdeki kanalları RTSP akışı olarak go2rtc'ye tanıtır (yaml + API).

Sadece Python standart kütüphanesini kullanır.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import isapi
import izleme

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("DVR_DATA", ROOT.parent / "data"))
CONFIG_FILE = DATA / "config.json"
GO2RTC_YAML = DATA / "go2rtc.yaml"
GO2RTC_API = os.environ.get("GO2RTC_API", "http://127.0.0.1:1984")
PORT = int(os.environ.get("PORT", "8765"))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "443"))
TLS_DIR = DATA / "tls"
LOGO_FILE = DATA / "logo.img"
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MAX_PX = 600
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

GRIDS = {"auto", "1x1", "2x2", "3x3", "4x4", "5x5"}
STREAM_RE = re.compile(r"^b[0-9a-f]{6}_\d+(_main)?$")
PUBLIC_PATHS = {"/display", "/api/display", "/api/version", "/api/alarms/active", "/static/display.js",
                "/login", "/ca.crt", "/logo"}
SESSION_COOKIE = "oturum"
SESSION_DAYS = 30
LOGIN_MAX_FAILS = 5        # bu kadar hatalı denemeden sonra
LOGIN_LOCK_SECONDS = 600   # IP bu süre boyunca giriş yapamaz
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}

DEFAULT_SETTINGS = {
    "rotate_seconds": 30, "go2rtc_port": 1984, "go2rtc_listen": "127.0.0.1",
    # Çalışma saatleri (zamanlama.py uygular); days: 0=Pzt .. 6=Paz
    "schedule": {"enabled": False, "days": [0, 1, 2, 3, 4, 5], "on": "08:30", "off": "19:30"},
    # version 0 = logo yok; opacity yüzde, size ekran yüksekliğinin yüzdesi
    "logo": {"version": 0, "type": "", "opacity": 50, "size": 10},
}


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


class ApiError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- veri deposu

class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        data.setdefault("branches", [])
        data.setdefault("lists", [])
        data["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        self.data = data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def branch(self, bid):
        b = next((b for b in self.data["branches"] if b["id"] == bid), None)
        if b is None:
            raise ApiError("Şube bulunamadı", HTTPStatus.NOT_FOUND)
        return b

    def lst(self, lid):
        item = next((x for x in self.data["lists"] if x["id"] == lid), None)
        if item is None:
            raise ApiError("Liste bulunamadı", HTTPStatus.NOT_FOUND)
        return item


store = Store(CONFIG_FILE)
monitor = izleme.Monitor(store, DATA)
isapi_clients = {}


def new_id(prefix, existing):
    while True:
        i = prefix + secrets.token_hex(3)
        if i not in existing:
            return i


def public_branch(b):
    out = {k: v for k, v in b.items() if k not in ("password", "stream_key")}
    out["has_password"] = bool(b.get("password"))
    out["has_stream_key"] = bool(b.get("stream_key"))
    return out


def client_for(b):
    key = (b["ip"], b["http_port"], b["username"], b["password"])
    cli = isapi_clients.get(key)
    if cli is None:
        cli = isapi_clients[key] = isapi.Isapi(*key)
    return cli


def validate_branch(body, existing=None):
    def port(name, default):
        try:
            p = int(body.get(name) or (existing or {}).get(name) or default)
        except (TypeError, ValueError):
            raise ApiError(f"{name} geçerli bir port olmalı")
        if not 1 <= p <= 65535:
            raise ApiError(f"{name} 1-65535 arasında olmalı")
        return p

    name = str(body.get("name", "")).strip()
    ip = str(body.get("ip", "")).strip()
    username = str(body.get("username", "")).strip()
    password = body.get("password") or (existing or {}).get("password", "")
    if not name:
        raise ApiError("Şube adı gerekli")
    if not ip or re.search(r"[\s/:@]", ip):
        raise ApiError("Geçerli bir IP adresi veya alan adı girin")
    if not username:
        raise ApiError("Kullanıcı adı gerekli")
    if not password:
        raise ApiError("Şifre gerekli")
    # Hik-Connect akış şifrelemesi açıksa doğrulama kodu; boş gönderilirse mevcut kod korunur.
    stream_key = str(body.get("stream_key") or (existing or {}).get("stream_key", "")).strip()
    return {"name": name, "ip": ip, "username": username, "password": password, "stream_key": stream_key,
            "http_port": port("http_port", 80), "rtsp_port": port("rtsp_port", 554)}


def validate_list(body):
    name = str(body.get("name", "")).strip()
    if not name:
        raise ApiError("Liste adı gerekli")
    grid = body.get("grid", "auto")
    if grid not in GRIDS:
        raise ApiError("Geçersiz grid")
    quality = body.get("quality", "sub")
    if quality not in ("sub", "main"):
        raise ApiError("Geçersiz kalite")
    items = []
    for it in body.get("items", []):
        if it.get("empty"):
            items.append({"empty": True})
            continue
        try:
            items.append({"branch": str(it["branch"]), "channel": int(it["channel"])})
        except (KeyError, TypeError, ValueError):
            raise ApiError("Geçersiz kamera kaydı")
    return {"name": name, "grid": grid, "quality": quality, "active": bool(body.get("active", True)), "items": items}


# ---------------------------------------------------------------- go2rtc

def rtsp_url(b, no, stream):
    user = urllib.parse.quote(b["username"], safe="")
    pw = urllib.parse.quote(b["password"], safe="")
    return f"rtsp://{user}:{pw}@{b['ip']}:{b['rtsp_port']}/Streaming/Channels/{no * 100 + stream}"


def active_channels(b):
    """Pasif yapılmamış kanallar (b["disabled"]: pasif kanal numaraları; testte kanal listesi yenilense de korunur)."""
    off = set(b.get("disabled", []))
    return [c for c in b.get("channels", []) if c["no"] not in off]


def stream_source(b, no, stream):
    # Akış şifrelemesi açık şubeler hikstream.py üzerinden çözülerek gelir.
    if b.get("stream_key"):
        return f"exec:{sys.executable} {ROOT / 'hikstream.py'} {b['id']} {no} {stream}"
    return rtsp_url(b, no, stream)


def desired_streams():
    out = {}
    for b in store.data["branches"]:
        for c in active_channels(b):
            out[f"{b['id']}_{c['no']}"] = stream_source(b, c["no"], 2 if c.get("sub") else 1)
            out[f"{b['id']}_{c['no']}_main"] = stream_source(b, c["no"], 1)
    return out


def write_go2rtc_yaml(streams, settings):
    lines = [
        "# DVR Ekran sunucusu tarafından otomatik yazılır; elle düzenlemeyin.",
        "api:",
        f'  listen: "{settings["go2rtc_listen"]}:{settings["go2rtc_port"]}"',
        # Ekran sayfası (8765) go2rtc'ye (1984) WebSocket ile bağlanır; farklı port = farklı origin.
        '  origin: "*"',
        "rtsp:",
        '  listen: ""',
        "streams:",
    ]
    lines += [f"  {name}: {json.dumps(src)}" for name, src in sorted(streams.items())]
    GO2RTC_YAML.parent.mkdir(parents=True, exist_ok=True)
    GO2RTC_YAML.write_text("\n".join(lines) + "\n", "utf-8")


def go2rtc_call(method, path):
    req = urllib.request.Request(GO2RTC_API + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


def sync_go2rtc():
    with store.lock:
        streams = desired_streams()
        settings = dict(store.data["settings"])
    write_go2rtc_yaml(streams, settings)
    try:
        current = json.loads(go2rtc_call("GET", "/api/streams") or b"{}") or {}
    except Exception as e:
        log("go2rtc'ye ulaşılamadı:", e)
        return
    q = urllib.parse.quote
    for name in current:
        if STREAM_RE.match(name) and name not in streams:
            go2rtc_call("DELETE", f"/api/streams?src={q(name)}")
    for name, src in streams.items():
        producers = (current.get(name) or {}).get("producers") or []
        if any(p.get("url") == src for p in producers):
            continue
        method = "PATCH" if name in current else "PUT"
        try:
            go2rtc_call(method, f"/api/streams?name={q(name)}&src={q(src, safe='')}")
        except Exception as e:
            log(f"go2rtc {method} {name} başarısız:", e)


sync_event = threading.Event()


def sync_worker():
    while True:
        sync_event.wait(60)
        sync_event.clear()
        time.sleep(0.5)
        try:
            sync_go2rtc()
        except Exception as e:
            log("go2rtc senkron hatası:", e)


def request_sync():
    sync_event.set()


# ---------------------------------------------------------------- ekran

def display_payload():
    with store.lock:
        s = store.data["settings"]
        branches = {b["id"]: b for b in store.data["branches"]}
        lists = []
        for lst in store.data["lists"]:
            if not lst.get("active", True):
                continue
            suffix = "_main" if lst.get("quality") == "main" else ""
            tiles = []
            for it in lst.get("items", []):
                if it.get("empty"):
                    tiles.append(None)
                    continue
                b = branches.get(it["branch"])
                ch = b and next((c for c in b.get("channels", []) if c["no"] == it["channel"]), None)
                if not ch:
                    continue
                if ch["no"] in b.get("disabled", []):
                    tiles.append(None)  # pasif kamera: düzen kaymasın diye boş kare
                    continue
                tiles.append({"stream": f"{b['id']}_{ch['no']}{suffix}", "label": f"{b['name']} · {ch['name']}"})
            if any(tiles):
                lists.append({"name": lst["name"], "grid": lst.get("grid", "auto"), "tiles": tiles})
        logo = s["logo"]
        payload = {"rotate_seconds": s["rotate_seconds"], "go2rtc_port": s["go2rtc_port"], "lists": lists,
                   "logo": {"url": f"/logo?v={logo['version']}", "opacity": logo["opacity"], "size": logo["size"]}
                   if logo["version"] else None}
    payload["hash"] = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
    return payload


# ---------------------------------------------------------------- API işlemleri

def api_state():
    with store.lock:
        return {"branches": [public_branch(b) for b in store.data["branches"]],
                "lists": store.data["lists"], "settings": store.data["settings"]}


def api_create_branch(body):
    data = validate_branch(body)
    with store.lock:
        bid = new_id("b", {b["id"] for b in store.data["branches"]})
        b = {"id": bid, **data, "channels": [], "last_test": None}
        store.data["branches"].append(b)
        store.save()
        return public_branch(b)


def api_update_branch(bid, body):
    with store.lock:
        b = store.branch(bid)
        b.update(validate_branch(body, b))
        store.save()
        request_sync()
        return public_branch(b)


def api_delete_branch(bid):
    with store.lock:
        store.branch(bid)
        store.data["branches"] = [b for b in store.data["branches"] if b["id"] != bid]
        for lst in store.data["lists"]:
            lst["items"] = [it for it in lst["items"] if it.get("branch") != bid]
        store.save()
    request_sync()
    return {"ok": True}


def api_test_branch(bid):
    with store.lock:
        b = dict(store.branch(bid))
    log(f"Test: {b['name']} ({b['ip']})")
    result = isapi.test_device(b["ip"], b["http_port"], b["rtsp_port"], b["username"], b["password"])
    with store.lock:
        target = next((x for x in store.data["branches"] if x["id"] == bid), None)
        if target is not None:
            target["last_test"] = {k: result[k] for k in ("ok", "time", "steps", "warnings", "device")}
            if result["channels"]:
                target["channels"] = result["channels"]
            store.save()
            result["branch"] = public_branch(target)
    request_sync()
    return result


def api_set_channel_active(bid, no, body):
    active = bool(body.get("active"))
    with store.lock:
        b = store.branch(bid)
        if not any(c["no"] == no for c in b.get("channels", [])):
            raise ApiError("Kanal bulunamadı", HTTPStatus.NOT_FOUND)
        off = set(b.get("disabled", []))
        if active:
            off.discard(no)
        else:
            off.add(no)
        b["disabled"] = sorted(off)
        store.save()
        name = next(c["name"] for c in b["channels"] if c["no"] == no)
    if not active:
        monitor.drop(f"{bid}:{no}:", f"{b['name']} · {name}: pasif yapıldı")
    request_sync()
    return public_branch(b)


def api_fix_substream(bid):
    """H.264 olmayan alt akışları DVR'de H.264'e çevirir, ardından şubeyi yeniden test eder."""
    with store.lock:
        b = dict(store.branch(bid))
    cli = client_for(b)
    results = []
    for c in b.get("channels", []):
        if not c.get("sub") or c["sub"]["codec"] == "H.264":
            continue
        try:
            detail = cli.set_sub_codec_h264(c["no"])
            results.append({"no": c["no"], "name": c["name"], "ok": True, "detail": detail})
        except isapi.IsapiError as e:
            results.append({"no": c["no"], "name": c["name"], "ok": False, "detail": str(e)})
            if isinstance(e, isapi.AuthError):
                break
    log(f"Alt akış H.264: {b['name']}: " + ", ".join(f"{r['no']}={'ok' if r['ok'] else r['detail']}" for r in results))
    return {"results": results, "test": api_test_branch(bid)}


def api_snapshot(bid, no):
    with store.lock:
        b = dict(store.branch(bid))
    try:
        return client_for(b).snapshot(no)
    except isapi.IsapiError as e:
        raise ApiError(str(e), HTTPStatus.BAD_GATEWAY)


def api_create_list(body):
    with store.lock:
        lid = new_id("l", {x["id"] for x in store.data["lists"]})
        name = str(body.get("name") or f"Liste {len(store.data['lists']) + 1}")
        lst = {"id": lid, **validate_list({"name": name, "grid": "4x4"})}
        store.data["lists"].append(lst)
        store.save()
        return lst


def api_update_list(lid, body):
    with store.lock:
        lst = store.lst(lid)
        lst.update(validate_list(body))
        store.save()
        return lst


def api_delete_list(lid):
    with store.lock:
        store.lst(lid)
        store.data["lists"] = [x for x in store.data["lists"] if x["id"] != lid]
        store.save()
    return {"ok": True}


def api_move_list(lid, body):
    with store.lock:
        lists = store.data["lists"]
        i = lists.index(store.lst(lid))
        j = i + (1 if body.get("direction") == "down" else -1)
        if 0 <= j < len(lists):
            lists[i], lists[j] = lists[j], lists[i]
            store.save()
    return {"ok": True}


def validate_schedule(sch):
    times = {}
    for k in ("on", "off"):
        v = str(sch.get(k, ""))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ApiError("Saatler SS:DD biçiminde olmalı (örn. 08:30)")
        times[k] = v
    if times["on"] >= times["off"]:
        raise ApiError("Açılış saati kapanış saatinden önce olmalı")
    try:
        days = sorted({int(d) for d in sch.get("days", [])})
    except (TypeError, ValueError):
        raise ApiError("Geçersiz gün listesi")
    if any(not 0 <= d <= 6 for d in days):
        raise ApiError("Geçersiz gün")
    return {"enabled": bool(sch.get("enabled")), "days": days, **times}


def api_settings(body):
    with store.lock:
        s = store.data["settings"]
        try:
            rotate = int(body.get("rotate_seconds", s["rotate_seconds"]))
        except (TypeError, ValueError):
            raise ApiError("Dönüş süresi sayı olmalı")
        if not 5 <= rotate <= 3600:
            raise ApiError("Dönüş süresi 5-3600 saniye arasında olmalı")
        if "schedule" in body:
            s["schedule"] = validate_schedule(body["schedule"])
        if "logo" in body:
            try:
                opacity, size = int(body["logo"]["opacity"]), int(body["logo"]["size"])
            except (TypeError, ValueError, KeyError):
                raise ApiError("Logo ayarları geçersiz")
            if not (10 <= opacity <= 100 and 3 <= size <= 30):
                raise ApiError("Saydamlık %10-100, boyut %3-30 arasında olmalı")
            s["logo"] = {**s["logo"], "opacity": opacity, "size": size}
        s["rotate_seconds"] = rotate
        store.save()
        return s


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def api_upload_logo(data):
    if len(data) > LOGO_MAX_BYTES:
        raise ApiError("Logo en fazla 2 MB olabilir")
    ctype = image_type(data)
    if not ctype:
        raise ApiError("PNG, JPEG veya WebP yükleyin (şeffaf logo için PNG)")
    try:
        from PIL import Image
    except ImportError:
        Image = None  # Pillow yoksa dosya olduğu gibi saklanır
    if Image:
        try:
            im = Image.open(io.BytesIO(data))
            im.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX))
            out = io.BytesIO()
            im.convert("RGBA").save(out, "PNG", optimize=True)
        except Exception:
            raise ApiError("Görsel okunamadı")
        data, ctype = out.getvalue(), "image/png"
    with store.lock:
        LOGO_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOGO_FILE.write_bytes(data)
        s = store.data["settings"]
        s["logo"] = {**s["logo"], "type": ctype, "version": int(time.time())}
        store.save()
        return s


def api_delete_logo():
    with store.lock:
        LOGO_FILE.unlink(missing_ok=True)
        s = store.data["settings"]
        s["logo"] = {**s["logo"], "type": "", "version": 0}
        store.save()
        return s


def api_alarms():
    out = monitor.public()
    s = monitor.settings()
    s["email"]["has_password"] = bool(s["email"].pop("password", ""))
    out["settings"] = s
    return out


def api_alarms_active():
    return {"alarms": [a["text"] for a in monitor.public()["active"] if not a.get("acked")]}


def api_alarm_settings(body):
    try:
        interval = int(body.get("interval_min", 5))
        gap = int(body.get("record_gap_min", 20))
    except (TypeError, ValueError):
        raise ApiError("Süreler sayı olmalı")
    if not 2 <= interval <= 120 or not 5 <= gap <= 720:
        raise ApiError("Kontrol aralığı 2-120 dk, kayıt boşluğu 5-720 dk olmalı")
    e = body.get("email") or {}
    to = [x.strip() for x in re.split(r"[,;\s]+", str(e.get("to", ""))) if x.strip()] \
        if not isinstance(e.get("to"), list) else [str(x).strip() for x in e["to"] if str(x).strip()]
    if any("@" not in x for x in to):
        raise ApiError("Alıcı e-posta adreslerinden biri geçersiz")
    security = e.get("security", "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise ApiError("Geçersiz bağlantı güvenliği")
    try:
        port = int(e.get("port") or 587)
    except (TypeError, ValueError):
        raise ApiError("SMTP portu sayı olmalı")
    old = monitor.settings()["email"]
    email = {"enabled": bool(e.get("enabled")), "host": str(e.get("host", "")).strip(), "port": port,
             "security": security, "username": str(e.get("username", "")).strip(),
             "password": e.get("password") or old.get("password", ""),
             "sender": str(e.get("sender", "")).strip(), "to": to}
    with store.lock:
        store.data["settings"]["alarms"] = {"interval_min": interval, "record_gap_min": gap, "email": email}
        store.save()
    return api_alarms()


def api_test_email():
    try:
        monitor.send_email(monitor.settings(), [], [], test=True)
    except Exception as e:
        raise ApiError(f"E-posta gönderilemedi: {e}", HTTPStatus.BAD_GATEWAY)
    return {"ok": True}


def api_check_now():
    monitor.wake.set()
    return {"ok": True}


def api_ack(body):
    monitor.ack(str(body.get("key", "")))
    return api_alarms()


ROUTES = [
    ("GET", r"/api/alarms", lambda m, b: api_alarms()),
    ("GET", r"/api/alarms/active", lambda m, b: api_alarms_active()),
    ("PUT", r"/api/alarms/settings", lambda m, b: api_alarm_settings(b)),
    ("POST", r"/api/alarms/test-email", lambda m, b: api_test_email()),
    ("POST", r"/api/alarms/check", lambda m, b: api_check_now()),
    ("POST", r"/api/alarms/ack", lambda m, b: api_ack(b)),
    ("GET", r"/api/state", lambda m, b: api_state()),
    ("GET", r"/api/display", lambda m, b: display_payload()),
    ("GET", r"/api/version", lambda m, b: {"hash": display_payload()["hash"]}),
    ("POST", r"/api/branches", lambda m, b: api_create_branch(b)),
    ("PUT", r"/api/branches/(b\w+)", lambda m, b: api_update_branch(m[1], b)),
    ("DELETE", r"/api/branches/(b\w+)", lambda m, b: api_delete_branch(m[1])),
    ("POST", r"/api/branches/(b\w+)/test", lambda m, b: api_test_branch(m[1])),
    ("POST", r"/api/branches/(b\w+)/fix-substream", lambda m, b: api_fix_substream(m[1])),
    ("PUT", r"/api/branches/(b\w+)/channels/(\d+)", lambda m, b: api_set_channel_active(m[1], int(m[2]), b)),
    ("GET", r"/api/branches/(b\w+)/snapshot/(\d+)", lambda m, b: api_snapshot(m[1], int(m[2]))),
    ("POST", r"/api/lists", lambda m, b: api_create_list(b)),
    ("PUT", r"/api/lists/(l\w+)", lambda m, b: api_update_list(m[1], b)),
    ("DELETE", r"/api/lists/(l\w+)", lambda m, b: api_delete_list(m[1])),
    ("POST", r"/api/lists/(l\w+)/move", lambda m, b: api_move_list(m[1], b)),
    ("PUT", r"/api/settings", lambda m, b: api_settings(b)),
    ("DELETE", r"/api/logo", lambda m, b: api_delete_logo()),
]


# ---------------------------------------------------------------- oturum

_session_key = None
_login_fails = {}  # ip -> hatalı deneme zamanları
_login_lock = threading.Lock()


def session_key():
    global _session_key
    if _session_key is None:
        f = DATA / "session.key"
        if not f.exists():
            DATA.mkdir(parents=True, exist_ok=True)
            f.write_bytes(secrets.token_bytes(32))
            f.chmod(0o600)
        _session_key = f.read_bytes()
    return _session_key


def make_session(expires):
    # Şifre imzaya dahil: şifre değişince eski oturumlar kendiliğinden geçersiz olur.
    msg = f"{ADMIN_USER}:{expires}:{ADMIN_PASSWORD}".encode()
    return f"{expires}.{hmac.new(session_key(), msg, hashlib.sha256).hexdigest()}"


def session_valid(token):
    expires = token.partition(".")[0]
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(token, make_session(int(expires)))


def check_login(ip, user, password):
    """(başarılı mı, hata kodu). Hatalı denemeler IP başına sınırlanır."""
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails.get(ip, []) if t > now - LOGIN_LOCK_SECONDS]
        _login_fails[ip] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            return False, "kilitli"
    ok = (secrets.compare_digest(user.encode(), ADMIN_USER.encode())
          and secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()))
    if ok:
        with _login_lock:
            _login_fails.pop(ip, None)
        return True, ""
    time.sleep(1)
    with _login_lock:
        _login_fails.setdefault(ip, []).append(now)
    return False, "hatali"


def safe_next(target):
    """Girişten sonra sadece bu sitedeki bir sayfaya dönülür (açık yönlendirme olmasın)."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# ---------------------------------------------------------------- HTTP

class Server(ThreadingHTTPServer):
    daemon_threads = True
    tls = False
    redirect_https = False

    def handle_error(self, request, client_address):
        # Sertifikayı tanımayan tarayıcıların kopardığı bağlantılar günlüğü doldurmasın.
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "DVREkran/1.0"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _authorized(self, path):
        if not ADMIN_PASSWORD or path in PUBLIC_PATHS:
            return True
        if self._local():
            return True  # cihazın kendisi (kiosk, SSH ile bakım): oraya erişen zaten içeride
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(SESSION_COOKIE)
        if morsel and session_valid(morsel.value):
            return True
        h = self.headers.get("Authorization", "")  # betikler için (curl -u)
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        return (secrets.compare_digest(user.encode(), ADMIN_USER.encode())
                and secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()))

    def _redirect(self, location, status=HTTPStatus.SEE_OTHER, cookie=None):
        extra = {"Location": location}
        if cookie:
            extra["Set-Cookie"] = cookie
        self._send(status, b"", "text/plain; charset=utf-8", extra)

    def _cookie(self, value, max_age):
        secure = "; Secure" if self.server.tls else ""
        return f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    def _login(self):
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        get = lambda k: (form.get(k) or [""])[0]
        nxt = safe_next(get("next"))
        ok, err = check_login(self.client_address[0], get("username"), get("password"))
        if not ok:
            log("Hatalı giriş:", self.client_address[0])
            return self._redirect("/login?" + urllib.parse.urlencode({"hata": err, "next": nxt}))
        max_age = SESSION_DAYS * 86400
        self._redirect(nxt, cookie=self._cookie(make_session(int(time.time()) + max_age), max_age))

    def _https_redirect(self):
        """Ağdan düz HTTP ile gelenleri HTTPS'e yönlendirir (kiosk localhost'tan geldiği için etkilenmez)."""
        if not self.server.redirect_https or self._local() or self.path == "/ca.crt":
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            return False
        port = "" if HTTPS_PORT == 443 else f":{HTTPS_PORT}"
        self._redirect(f"https://{host}{port}{self.path}", HTTPStatus.MOVED_PERMANENTLY)
        return True

    def _send(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, name):
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Bulunamadı"})
        self._send(HTTPStatus.OK, f.read_bytes(), CONTENT_TYPES.get(f.suffix, "application/octet-stream"))

    def _handle(self, method):
        path = urllib.parse.urlsplit(self.path).path
        if self._https_redirect():
            return
        if method == "GET" and path == "/login":
            return self._static("login.html")
        if method == "POST" and path == "/login":
            return self._login()
        if method == "GET" and path == "/logout":
            return self._redirect("/login", cookie=self._cookie("", 0))
        if method == "GET" and path == "/ca.crt":
            f = TLS_DIR / "ca.crt"
            if not f.is_file():
                return self._json(HTTPStatus.NOT_FOUND, {"error": "Sertifika yok"})
            return self._send(HTTPStatus.OK, f.read_bytes(), "application/x-x509-ca-cert",
                              {"Content-Disposition": 'attachment; filename="panoptes-ca.crt"'})
        if method == "GET" and path == "/logo":
            with store.lock:
                ctype = store.data["settings"]["logo"]["type"]
            if not ctype or not LOGO_FILE.is_file():
                return self._json(HTTPStatus.NOT_FOUND, {"error": "Logo yok"})
            return self._send(HTTPStatus.OK, LOGO_FILE.read_bytes(), ctype)
        if not self._authorized(path):
            if path.startswith("/api/"):
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Oturum açın"})
            return self._redirect("/login?" + urllib.parse.urlencode({"next": path}))
        if method == "POST" and path == "/api/logo":
            length = int(self.headers.get("Content-Length") or 0)
            if length > LOGO_MAX_BYTES:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Logo en fazla 2 MB olabilir"})
            try:
                return self._json(HTTPStatus.OK, api_upload_logo(self.rfile.read(length)))
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
        if method == "GET" and path == "/":
            return self._static("admin.html")
        if method == "GET" and path == "/display":
            return self._static("display.html")
        if method == "GET" and path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        for m, pattern, fn in ROUTES:
            match = re.fullmatch(pattern, path)
            if m == method and match:
                break
        else:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Bulunamadı"})
        try:
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length) or b"{}")
            result = fn(match, body)
        except ApiError as e:
            return self._json(e.status, {"error": str(e)})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Geçersiz JSON"})
        except Exception as e:
            log("Hata:", method, path, repr(e))
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Sunucu hatası: {e}"})
        if isinstance(result, bytes):
            return self._send(HTTPStatus.OK, result, "image/jpeg")
        self._json(HTTPStatus.OK, result)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    with store.lock:
        write_go2rtc_yaml(desired_streams(), store.data["settings"])
    threading.Thread(target=sync_worker, daemon=True).start()
    request_sync()
    monitor.start()
    # Windows'ta SO_REUSEADDR başka programın portunu sessizce paylaşmaya izin veriyor.
    Server.allow_reuse_address = os.name != "nt"
    server = Server(("0.0.0.0", PORT), Handler)
    crt, key = TLS_DIR / "server.crt", TLS_DIR / "server.key"
    if crt.is_file() and key.is_file():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(crt, key)
        https = Server(("0.0.0.0", HTTPS_PORT), Handler)
        # El sıkışma ilk okumada, istek iş parçacığında yapılır; yavaş bir istemci accept'i kilitlemez.
        https.socket = ctx.wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)
        https.tls = True
        server.redirect_https = True
        threading.Thread(target=https.serve_forever, daemon=True).start()
        log(f"HTTPS açık, port {HTTPS_PORT}")
    log(f"Yönetim paneli: http://localhost:{PORT}/   Ekran: http://localhost:{PORT}/display")
    if not ADMIN_PASSWORD:
        log("UYARI: ADMIN_PASSWORD tanımlı değil, panel şifresiz.")
    server.serve_forever()

if __name__ == "__main__":
    main()

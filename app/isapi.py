"""Hikvision ISAPI istemcisi: cihaz bilgisi, kanal listesi, anlık görüntü ve port testleri.

Kimlik doğrulama elle yapılır (Digest/Basic). urllib'in hazır Digest handler'ı hatalı
şifrede isteği 5 kez tekrarlar; Hikvision birkaç hatalı denemede hesabı kilitlediği için
burada hatalı şifre en fazla bir başarısız deneme sayılır.
"""
import base64
import hashlib
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

TIMEOUT = 8


class IsapiError(Exception):
    pass


class AuthError(IsapiError):
    pass


class NotSupported(IsapiError):
    pass


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def parse_challenge(headers):
    """WWW-Authenticate başlıklarından Digest'i (yoksa Basic'i) seçer."""
    basic = None
    for h in headers:
        scheme, _, rest = h.strip().partition(" ")
        if scheme.lower() == "digest":
            params = {k.lower(): (v1 if v1 else v2) for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest)}
            params["scheme"] = "digest"
            return params
        if scheme.lower() == "basic":
            basic = {"scheme": "basic"}
    return basic


def auth_header(challenge, username, password, method, uri, nc):
    """Digest/Basic Authorization başlığı (ISAPI ve RTSP ortak kullanır)."""
    if challenge["scheme"] == "basic":
        return "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode()
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    qop = "auth" if "auth" in [q.strip() for q in challenge.get("qop", "").split(",")] else ""
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if qop:
        ncs, cnonce = f"{nc:08x}", secrets.token_hex(8)
        parts += [f'response="{_md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:{qop}:{ha2}")}"', "algorithm=MD5",
                  f"qop={qop}", f"nc={ncs}", f'cnonce="{cnonce}"']
    else:
        parts += [f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"', "algorithm=MD5"]
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def _auth_message(body):
    txt = body.decode("utf-8", "ignore")
    if re.search(r"<lockStatus>\s*lock\s*<", txt):
        m = re.search(r"<unlockTime>(\d+)", txt)
        wait = f"{int(m.group(1)) // 60 + 1} dk sonra" if m else "bir süre sonra"
        return f"Hesap kilitlendi (çok fazla hatalı deneme). {wait} tekrar deneyin."
    m = re.search(r"<retryLoginTime>(\d+)", txt)
    if m:
        return f"Kullanıcı adı veya şifre hatalı (kalan deneme hakkı: {m.group(1)})"
    return "Kullanıcı adı veya şifre hatalı"


def _status_detail(body):
    """Hikvision ResponseStatus gövdesinden kısa açıklama (örn. ' · badParameters')."""
    txt = body.decode("utf-8", "ignore")
    parts = [m.group(1) for tag in ("statusString", "subStatusCode")
             if (m := re.search(rf"<{tag}>([^<]+)</{tag}>", txt))]
    return " · " + " / ".join(parts) if parts else ""


def _xml(data):
    root = ET.fromstring(data)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el, path, default=""):
    if el is None:
        return default
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else default


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _codec(raw):
    r = raw.upper().replace("_", ".")
    if "265" in r or "HEVC" in r:
        return "H.265"
    if "264" in r:
        return "H.264"
    return raw or "?"


class Isapi:
    def __init__(self, ip, port, username, password, timeout=TIMEOUT):
        self.base = f"http://{ip}:{port}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._challenge = None
        self._nc = 0

    def _auth_header(self, method, path):
        if self._challenge is None:
            return None
        self._nc += 1
        return auth_header(self._challenge, self.username, self.password, method, path, self._nc)

    def request(self, path, method="GET", body=None):
        fresh = False
        for _ in range(2):
            req = urllib.request.Request(self.base + path, data=body, method=method)
            if body is not None:
                req.add_header("Content-Type", "application/xml; charset=UTF-8")
            auth = self._auth_header(method, path)
            if auth:
                req.add_header("Authorization", auth)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                body_err = e.read()
                detail = _status_detail(body_err)
                if e.code in (403, 404):
                    raise NotSupported(f"{path} desteklenmiyor (HTTP {e.code}{detail})")
                if e.code != 401:
                    raise IsapiError(f"HTTP {e.code}: {e.reason}{detail}")
                if fresh:
                    raise AuthError(_auth_message(body_err))
                ch = parse_challenge(e.headers.get_all("WWW-Authenticate") or [])
                if ch is None:
                    raise AuthError("Cihaz desteklenmeyen bir kimlik doğrulama yöntemi istedi")
                self._challenge, self._nc, fresh = ch, 0, True
            except (urllib.error.URLError, OSError) as e:
                reason = getattr(e, "reason", e)
                raise IsapiError(f"Bağlantı hatası: {reason}")
        raise AuthError("Kimlik doğrulama başarısız")

    def get_xml(self, path):
        data = self.request(path)
        try:
            return _xml(data)
        except ET.ParseError:
            raise IsapiError(f"{path}: geçersiz XML yanıtı")

    def device_info(self):
        root = self.get_xml("/ISAPI/System/deviceInfo")
        return {
            "name": _text(root, "deviceName"),
            "model": _text(root, "model"),
            "serial": _text(root, "serialNumber"),
            "firmware": " ".join(filter(None, [_text(root, "firmwareVersion"), _text(root, "firmwareReleasedDate")])),
            "type": _text(root, "deviceType"),
        }

    def channels(self):
        root = self.get_xml("/ISAPI/Streaming/channels")
        by_no = {}
        for sc in root.iter("StreamingChannel"):
            no, stream = divmod(_int(_text(sc, "id")), 100)
            if no <= 0 or stream not in (1, 2):
                continue
            v = sc.find("Video")
            w, h = _int(_text(v, "videoResolutionWidth")), _int(_text(v, "videoResolutionHeight"))
            info = {
                "codec": _codec(_text(v, "videoCodecType")),
                "res": f"{w}x{h}" if w and h else "",
                "fps": round(_int(_text(v, "maxFrameRate")) / 100) or None,
            }
            c = by_no.setdefault(no, {"no": no, "name": "", "main": None, "sub": None, "online": None})
            c["main" if stream == 1 else "sub"] = info
            if stream == 1:
                c["name"] = _text(sc, "channelName")

        # Kamera isimleri (OSD) bu uçlarda daha doğru; desteklenmeyen cihazda atlanır.
        for path, tag in (("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
                          ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel")):
            try:
                names = self.get_xml(path)
            except AuthError:
                raise
            except IsapiError:
                continue
            for el in names.iter(tag):
                no, name = _int(_text(el, "id")), _text(el, "name")
                if no in by_no and name:
                    by_no[no]["name"] = name

        try:
            status = self.get_xml("/ISAPI/ContentMgmt/InputProxy/channels/status")
            for el in status.iter("InputProxyChannelStatus"):
                no = _int(_text(el, "id"))
                if no in by_no:
                    by_no[no]["online"] = _text(el, "online").lower() == "true"
        except AuthError:
            raise
        except IsapiError:
            pass

        for c in by_no.values():
            c["name"] = c["name"] or f"Kamera {c['no']}"
        return [by_no[k] for k in sorted(by_no)]

    def set_sub_codec_h264(self, no):
        """Kanalın alt akış kodlamasını H.264 yapar; diğer ayarlara dokunmaz. Cihazın yanıtını döner."""
        sid = no * 100 + 2
        # Analog/DVR kanalları ilk yolda; NVR'deki IP kameralar bazen sadece proxy yolunu kabul eder.
        for base in ("/ISAPI/Streaming/channels", "/ISAPI/ContentMgmt/StreamingProxy/channels"):
            path = f"{base}/{sid}"
            try:
                raw = self.request(path).decode("utf-8")
            except NotSupported:
                continue
            new, n = re.subn(r"<videoCodecType>[^<]*</videoCodecType>",
                             "<videoCodecType>H.264</videoCodecType>", raw, count=1)
            if not n:
                raise IsapiError("Cihaz yanıtında kodlama ayarı bulunamadı")
            try:
                resp = self.request(path, "PUT", new.encode("utf-8"))
            except NotSupported:
                continue
            status = _status_detail(resp).lstrip(" ·")
            return status or "OK"
        raise NotSupported("Cihaz bu kanalın alt akış ayarını uzaktan değiştirmeye izin vermiyor")

    def snapshot(self, no):
        data = self.request(f"/ISAPI/Streaming/channels/{no * 100 + 1}/picture")
        if not data.startswith(b"\xff\xd8"):
            raise IsapiError("Cihaz görüntü yerine farklı bir yanıt döndü")
        return data


def tcp_check(host, port, timeout=5):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"açık ({int((time.monotonic() - start) * 1000)} ms)"
    except socket.gaierror:
        return False, "adres çözümlenemedi"
    except ConnectionRefusedError:
        return False, "bağlantı reddedildi (port kapalı)"
    except (socket.timeout, TimeoutError):
        return False, "zaman aşımı (port kapalı veya modemde yönlendirilmemiş)"
    except OSError as e:
        return False, str(e)


def rtsp_check(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            if s.recv(512).startswith(b"RTSP/"):
                return True, "RTSP yanıt verdi"
            return False, "port açık ama RTSP yanıtı gelmedi"
    except socket.gaierror:
        return False, "adres çözümlenemedi"
    except ConnectionRefusedError:
        return False, "bağlantı reddedildi (port kapalı)"
    except (socket.timeout, TimeoutError):
        return False, "zaman aşımı (port kapalı veya modemde yönlendirilmemiş)"
    except OSError as e:
        return False, str(e)


def test_device(ip, http_port, rtsp_port, username, password):
    res = {"ok": False, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": [],
           "warnings": [], "device": None, "channels": []}

    def step(name, ok, detail=""):
        res["steps"].append({"name": name, "ok": ok, "detail": detail})

    def check_rtsp():
        ok, detail = rtsp_check(ip, rtsp_port)
        step(f"RTSP portu ({rtsp_port})", ok, detail)
        if not ok:
            res["warnings"].append(
                f"Görüntü RTSP ({rtsp_port}) üzerinden alınır. Şube modeminde bu port DVR'ye yönlendirilmeli.")

    ok, detail = tcp_check(ip, http_port)
    step(f"HTTP portu ({http_port})", ok, detail)
    if not ok:
        res["warnings"].append(
            f"Cihaz ayarları HTTP ({http_port}) üzerinden okunur. Şube modeminde bu port DVR'ye yönlendirilmeli "
            "(iVMS'in kullandığı 8000 portu bu iş için yeterli değil).")
        check_rtsp()
        return res

    cli = Isapi(ip, http_port, username, password)
    try:
        info = cli.device_info()
        res["device"] = info
        step("Giriş (kullanıcı adı / şifre)", True, " · ".join(filter(None, [info["model"], info["name"]])))
    except IsapiError as e:
        step("Giriş (kullanıcı adı / şifre)", False, str(e))
        return res

    try:
        chans = cli.channels()
        res["channels"] = chans
        step("Kanal listesi", bool(chans), f"{len(chans)} kanal bulundu" if chans else "kanal bulunamadı")
    except IsapiError as e:
        step("Kanal listesi", False, str(e))

    check_rtsp()

    for c in res["channels"]:
        if c["online"] is False:
            continue
        sub = c["sub"]
        if sub is None:
            res["warnings"].append(f"{c['name']}: alt akış (substream) yok, ekranda ana akış kullanılacak.")
        elif sub["codec"] != "H.264":
            res["warnings"].append(
                f"{c['name']}: alt akış {sub['codec']}. Mini PC akıcı oynatamaz; DVR'de alt akışı H.264 yapın.")

    res["ok"] = all(s["ok"] for s in res["steps"])
    return res

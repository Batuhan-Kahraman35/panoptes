"""Hik-Connect akış şifrelemesi açık bir DVR kanalını çözüp go2rtc'ye verir.

go2rtc bunu 'exec:' kaynağı olarak çalıştırır; görüntü stdout'a Annex-B H.264 olarak yazılır.
Kullanım: hikstream.py <şube_id> <kanal_no> <akış: 1=ana, 2=alt>

Şifreleme (DS-7108HGHI-M1 / DS-7116HGHI-M1 DVR'lerde doğrulandı): SPS ve I-kare NAL'ları AES-128-ECB ile şifreli,
anahtar = doğrulama kodu (ASCII) sıfırla 16 bayta tamamlanmış. Şifreli NAL düzeni:
[açık başlık 1 bayt][AES(özgün NAL'ın tam 16'lık blokları, en fazla 4096 bayt; başlık dahil)][kalan açık baytlar].
DVR'ler P-karelerini şifrelemiyor; NVR arkasındaki IP kameralar (DS-7108NI-Q1) bazı P-karelerini de aynı
düzenle şifreliyor ve 17 bayttan kısa NAL'larda başlığı şifrelemeden ikiliyor. Şifreli olup olmadığı,
çözülen ilk baytın NAL başlığıyla tutmasından anlaşılır.
"""
import base64
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.parse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import isapi

DATA = os.environ.get("DVR_DATA", "/var/lib/dvr-ekran")
START = b"\x00\x00\x00\x01"
KEEPALIVE_SEC = 25
MAX_ENCRYPTED = 4096  # DVR, NAL'ın en fazla ilk 4096 baytını (256 blok) şifreliyor


def log(*args):
    print("hikstream:", *args, file=sys.stderr, flush=True)


class Rtsp:
    def __init__(self, host, port, username, password):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.buf = b""
        self.cseq = 0
        self.username, self.password = username, password
        self.challenge = None
        self.nc = 0
        self.session = None

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DVR bağlantıyı kapattı")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _read_response(self):
        while b"\r\n\r\n" not in self.buf:
            # Yanıttan önce gelen RTP çerçevelerini atla
            if self.buf[:1] == b"$" and len(self.buf) >= 4:
                self._read(4 + struct.unpack(">H", self.buf[2:4])[0])
                continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DVR bağlantıyı kapattı")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("utf-8", "ignore").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        auth = []
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
            if k.strip().lower() == "www-authenticate":
                auth.append(v.strip())
        body = self._read(int(headers.get("content-length", 0))).decode("utf-8", "ignore")
        return status, headers, body, auth

    def request(self, method, url, extra=None, retry=True):
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: dvr-ekran"]
        if self.session:
            lines.append(f"Session: {self.session}")
        if self.challenge:
            self.nc += 1
            lines.append("Authorization: " + isapi.auth_header(self.challenge, self.username, self.password, method, url, self.nc))
        lines += extra or []
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status, headers, body, auth = self._read_response()
        if status == 401 and retry:
            self.challenge = isapi.parse_challenge(auth)
            if self.challenge is None:
                raise ConnectionError("Desteklenmeyen RTSP kimlik doğrulaması")
            return self.request(method, url, extra, retry=False)
        if status != 200:
            raise ConnectionError(f"{method} başarısız: RTSP {status}")
        return headers, body

    def read_frame(self):
        """Sıradaki interleaved çerçeveyi döner: (kanal, veri). Araya giren RTSP yanıtlarını atlar."""
        while True:
            first = self._read(1)
            if first == b"$":
                ch, length = struct.unpack(">BH", self._read(3))
                return ch, self._read(length)
            # Keepalive yanıtı gibi metin: başlık sonuna kadar oku
            self.buf = first + self.buf
            self._read_response()


def rtp_payload(pkt):
    if len(pkt) < 12:
        return None
    cc = pkt[0] & 0x0F
    offset = 12 + cc * 4
    if pkt[0] & 0x10:  # uzantı başlığı
        offset += 4 + struct.unpack(">H", pkt[offset + 2:offset + 4])[0] * 4
    payload = pkt[offset:]
    if pkt[0] & 0x20 and payload:  # dolgu
        payload = payload[:-payload[-1]]
    return payload


def slice_header_ok(data):
    """Dilim başlığının ilk alanları makul mü: first_mb_in_slice=0, geçerli slice_type, pps_id=0."""
    bits = int.from_bytes(data[:8], "big")
    pos = 64

    def ue():
        nonlocal pos
        zeros = 0
        while pos > 0 and not (bits >> (pos - 1)) & 1:
            zeros += 1
            pos -= 1
        pos -= 1
        if zeros > 16 or pos - zeros < 0:
            return None
        val = (1 << zeros) - 1 + ((bits >> (pos - zeros)) & ((1 << zeros) - 1))
        pos -= zeros
        return val

    first_mb, slice_type, pps_id = ue(), ue(), ue()
    return first_mb == 0 and slice_type is not None and slice_type <= 9 and pps_id == 0


class Decryptor:
    def __init__(self, code):
        key = code.encode("ascii").ljust(16, b"\x00")[:16]
        self.cipher = Cipher(algorithms.AES(key), modes.ECB())
        self.ip_camera = False  # kısa NAL'da başlık ikilemesi görüldü mü (P-kareleri de şifreli olabilir)

    def nal(self, nal):
        kind = nal[0] & 0x1F
        if len(nal) < 17:
            # Şifreleyen IP kameralar kısa NAL'larda da başlığı ikiler (örn. PPS "68 68 ee 3c 80")
            if len(nal) > 1 and nal[1] == nal[0]:
                self.ip_camera = True
                return nal[1:]
            return nal
        if kind == 1 and not self.ip_camera:
            return nal  # DVR'ler P-karelerini şifrelemiyor; tesadüfi eşleşme riskine girme
        body = nal[1:]
        n = min(len(body) // 16 * 16, MAX_ENCRYPTED)
        d = self.cipher.decryptor()
        plain = d.update(body[:n]) + d.finalize()
        if plain[0] != nal[0]:
            return nal  # şifreli değil
        if kind in (1, 5) and not slice_header_ok(plain[1:]):
            return nal  # başlık tesadüfen tuttu ama dilim başlığı geçersiz: açık NAL
        return plain + body[n:]


def main():
    bid, ch, stream = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    cfg = json.load(open(os.path.join(DATA, "config.json"), encoding="utf-8"))
    b = next(x for x in cfg["branches"] if x["id"] == bid)
    dec = Decryptor(b["stream_key"]) if b.get("stream_key") else None
    url = f"rtsp://{b['ip']}:{b['rtsp_port']}/Streaming/Channels/{ch * 100 + stream}"
    out = sys.stdout.buffer

    rtsp = Rtsp(b["ip"], b["rtsp_port"], b["username"], b["password"])
    headers, sdp = rtsp.request("DESCRIBE", url, ["Accept: application/sdp"])
    base = headers.get("content-base", url + "/").rstrip("/") + "/"
    video = sdp.split("m=video", 1)[1].split("\nm=", 1)[0]
    control = re.search(r"a=control:(\S+)", video).group(1)
    track = control if control.startswith("rtsp://") else base + control
    headers, _ = rtsp.request("SETUP", track, ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
    rtsp.session = headers.get("session", "").split(";")[0]
    rtsp.request("PLAY", url, ["Range: npt=0.000-"])
    rtsp.sock.settimeout(20)

    # SDP'deki açık SPS/PPS: go2rtc codec'i ilk I-kareyi beklemeden tanısın
    m = re.search(r"sprop-parameter-sets=([^;\s]+)", video)
    if m:
        for ps in m.group(1).split(","):
            out.write(START + base64.b64decode(ps))

    fu = bytearray()
    last_keepalive = time.monotonic()
    while True:
        if time.monotonic() - last_keepalive > KEEPALIVE_SEC:
            rtsp.cseq += 1
            rtsp.sock.sendall(f"GET_PARAMETER {url} RTSP/1.0\r\nCSeq: {rtsp.cseq}\r\nSession: {rtsp.session}\r\n\r\n".encode())
            last_keepalive = time.monotonic()
        channel, pkt = rtsp.read_frame()
        if channel != 0:
            continue
        p = rtp_payload(pkt)
        if not p:
            continue
        kind = p[0] & 0x1F
        nals = []
        if 1 <= kind <= 23:
            nals.append(p)
        elif kind == 24:  # STAP-A
            i = 1
            while i + 2 <= len(p):
                size = struct.unpack(">H", p[i:i + 2])[0]
                nals.append(p[i + 2:i + 2 + size])
                i += 2 + size
        elif kind == 28 and len(p) > 2:  # FU-A
            if p[1] & 0x80:
                fu = bytearray([(p[0] & 0xE0) | (p[1] & 0x1F)])
            fu += p[2:]
            if p[1] & 0x40:
                nals.append(bytes(fu))
        for nal in nals:
            out.write(START + (dec.nal(nal) if dec else nal))
        if nals:
            out.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        log(f"{sys.argv[1:]}: {e}")
        sys.exit(1)

"""Hik-Connect akış şifrelemesini çözme denemesi (mini PC'de sudo ile çalışır).

DVR, SPS'in şifresiz halini SDP'de (sprop-parameter-sets), şifreli halini akışın içinde gönderiyor.
Farklı anahtar türetme yöntemlerini deneyip şifreli SPS'i SDP'dekiyle karşılaştırır.
Kullanım: sudo python3 sifre_deneme.py "ŞUBE ADI BAŞLANGICI" [kanal]
"""
import base64
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CONFIG = "/var/lib/dvr-ekran/config.json"


def aes_ecb_decrypt(key, data):
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def nal_units(stream):
    """Annex-B akışını NAL birimlerine böler."""
    for part in re.split(rb"\x00\x00\x01", stream):
        part = part.rstrip(b"\x00")
        if part:
            yield part


def remove_epb(data):
    return data.replace(b"\x00\x00\x03", b"\x00\x00")


def key_candidates(code):
    c = code.encode("ascii")
    md5 = hashlib.md5(c)
    yield "kod + sıfır dolgu", c.ljust(16, b"\x00")[:16]
    yield "md5(kod)", md5.digest()
    yield "md5(kod) hex ilk 16", md5.hexdigest()[:16].encode()
    yield "md5(kod) HEX ilk 16", md5.hexdigest()[:16].upper().encode()
    yield "kod tekrar", (c * 16)[:16]


def main():
    prefix = sys.argv[1]
    ch = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    b = next(x for x in cfg["branches"] if x["name"].upper().startswith(prefix.upper()))
    code = b.get("stream_key")
    q = urllib.parse.quote
    url = f"rtsp://{q(b['username'], safe='')}:{q(b['password'], safe='')}@{b['ip']}:{b['rtsp_port']}/Streaming/Channels/{ch * 100 + 2}"

    # SDP'deki şifresiz SPS
    sdp = subprocess.run(["ffprobe", "-v", "debug", "-rtsp_transport", "tcp", "-i", url],
                         capture_output=True, text=True, timeout=30).stderr
    m = re.search(r"sprop-parameter-sets=([A-Za-z0-9+/=]+),", sdp)
    if not m:
        sys.exit("SDP'de SPS bulunamadı")
    sdp_sps = base64.b64decode(m.group(1))
    print("SDP SPS     :", sdp_sps.hex(" "))

    # Akıştan 4 saniyelik ham H.264 (şifreli içerik, sadece kopya)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", url, "-t", "4", "-an",
                          "-c:v", "copy", "-f", "h264", "-"], capture_output=True, timeout=40).stdout
    nals = list(nal_units(raw))
    types = {}
    for n in nals:
        types[n[0] & 0x1F] = types.get(n[0] & 0x1F, 0) + 1
    print("NAL tipleri :", types, f"({len(raw)} bayt)")
    enc_sps = next((n for n in nals if n[0] & 0x1F == 7 and n != sdp_sps), None)
    if enc_sps is None:
        sys.exit("Akışta SDP'den farklı bir SPS yok; şifreleme SPS'i kapsamıyor olabilir")
    print("Akış SPS    :", enc_sps.hex(" "))
    if not code:
        sys.exit(f"{b['name']} için akış şifresi girilmemiş; anahtar denemesi atlandı")

    found = False
    for label, key in key_candidates(code):
        for epb_label, enc in (("ham", enc_sps), ("EPB'siz", remove_epb(enc_sps))):
            if len(enc) < 17:
                continue
            dec = aes_ecb_decrypt(key, enc[1:17])
            # Akıştaki SPS, SDP'dekinden bir bayt uzun; ilk 10 baytın tutması anahtarı doğrular.
            ok = dec[:10] == sdp_sps[1:11]
            print(f"  {'✓' if ok else '·'} {label:<22} {epb_label:<8} → {dec.hex(' ')}")
            found |= ok
    print("SONUÇ:", "ANAHTAR BULUNDU" if found else "eşleşme yok")


if __name__ == "__main__":
    main()

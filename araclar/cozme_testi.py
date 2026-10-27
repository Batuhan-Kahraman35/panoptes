"""Bulunan yöntemle 4 sn'lik örneği tamamen çözer ve ffmpeg ile kare kare doğrular (mini PC'de sudo ile).
Kullanım: sudo python3 cozme_testi.py "ŞUBE ADI BAŞLANGICI" [kanal]
"""
import json
import re
import subprocess
import sys
import urllib.parse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def split_nals(stream):
    return [p.rstrip(b"\x00") for p in re.split(rb"\x00\x00\x01", stream) if p.rstrip(b"\x00")]


def decrypt_nal(nal, ecb):
    # Şifreli NAL: [açık başlık 1 bayt][AES(ilk 16 bayt, başlık dahil)][kalan açık]
    if len(nal) < 17:
        return nal
    return ecb.update(nal[1:17]) + nal[17:]


def frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                          "-show_entries", "stream=nb_read_frames,width,height,profile", "-of", "compact", path],
                         capture_output=True, text=True).stdout.strip()
    errs = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True).stderr
    return out, len(errs.splitlines())


def main():
    prefix, ch = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1
    cfg = json.load(open("/var/lib/dvr-ekran/config.json", encoding="utf-8"))
    b = next(x for x in cfg["branches"] if x["name"].upper().startswith(prefix.upper()))
    q = urllib.parse.quote
    url = f"rtsp://{q(b['username'], safe='')}:{q(b['password'], safe='')}@{b['ip']}:{b['rtsp_port']}/Streaming/Channels/{ch * 100 + 2}"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", url, "-t", "6", "-an",
                          "-c:v", "copy", "-f", "h264", "-"], capture_output=True, timeout=40).stdout
    key = b["stream_key"].encode("ascii").ljust(16, b"\x00")[:16]
    ecb = Cipher(algorithms.AES(key), modes.ECB()).decryptor()

    nals = split_nals(raw)
    # SDP'den gelen şifresiz SPS/PPS'i (ffmpeg başa ekliyor) tekrar çözmemek için: çözünce başlık baytı tutmuyorsa açık kabul et
    out = bytearray()
    stats = {"cozuldu": 0, "acik": 0, "kisa": 0}
    for nal in nals:
        if len(nal) < 17:
            stats["kisa"] += 1
            out += b"\x00\x00\x00\x01" + nal
            continue
        dec = decrypt_nal(nal, ecb)
        if dec[0] == nal[0]:
            stats["cozuldu"] += 1
            out += b"\x00\x00\x00\x01" + dec
        else:
            stats["acik"] += 1
            out += b"\x00\x00\x00\x01" + nal
    open("/tmp/sifreli.h264", "wb").write(raw)
    open("/tmp/cozulmus.h264", "wb").write(out)
    print(f"{b['name']} kanal {ch}: {len(nals)} NAL, {stats}")
    print("şifreli :", *frames("/tmp/sifreli.h264"))
    print("çözülmüş:", *frames("/tmp/cozulmus.h264"))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "/tmp/cozulmus.h264", "-frames:v", "1", "-update", "1", "/tmp/kare.jpg"])


if __name__ == "__main__":
    main()

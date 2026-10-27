"""İndirilen orijinal kayıt parçalarından istenen saat aralığını MP4 olarak çıkarır (mini PC'de).
indir.log satırları: dosya \t başlangıç(YYYY-MM-DD SS:DD:ss) \t bitiş(SS:DD:ss) \t boyut \t sha256
Kullanım: kayit_kes.py KLASÖR "2026-10-26 03:30:00" "2026-10-26 04:30:00" ÖNEK [--h264]
Çıktı: ÖNEK_KameraNN_H265.mp4 (görüntü verisi değişmeden) ve --h264 ile ÖNEK_KameraNN_H264.mp4 (uyumlu)
"""
import datetime as dt
import os
import re
import subprocess
import sys
from collections import defaultdict


def segments(folder):
    segs = defaultdict(list)
    for line in open(os.path.join(folder, "indir.log"), encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 5 or not parts[0].endswith(".ps"):
            continue
        path, start, end_t = parts[0], dt.datetime.fromisoformat(parts[1]), dt.time.fromisoformat(parts[2])
        end = dt.datetime.combine(start.date(), end_t)
        if end <= start:
            end += dt.timedelta(days=1)
        cam = int(re.search(r"_k(\d+)", os.path.basename(path)).group(1))
        segs[cam].append((start, end, path))
    return segs


def run(cmd):
    subprocess.run(["nice", "-n", "19", *cmd], check=True)


def main():
    folder, ts, te, prefix = sys.argv[1], dt.datetime.fromisoformat(sys.argv[2]), dt.datetime.fromisoformat(sys.argv[3]), sys.argv[4]
    h264 = "--h264" in sys.argv
    for cam, segs in sorted(segments(folder).items()):
        pieces = []
        for start, end, path in sorted(segs):
            a, b = max(start, ts), min(end, te)
            if a < b:
                pieces.append((path, (a - start).total_seconds(), (b - a).total_seconds()))
        if not pieces:
            print(f"Kamera {cam}: aralıkta kayıt yok")
            continue
        variants = [("H265", ["-c:v", "copy"])]
        if h264:
            variants.append(("H264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]))
        for label, vcodec in variants:
            tmp = []
            for i, (path, off, dur) in enumerate(pieces):
                t = os.path.join(folder, f".parca_{cam}_{label}_{i}.ts")
                run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{off:.3f}", "-i", path, "-t", f"{dur:.3f}",
                     "-map", "0:v:0", "-map", "0:a:0?", *vcodec, "-c:a", "aac", "-b:a", "48k", "-f", "mpegts", t])
                tmp.append(t)
            out = f"{prefix}_Kamera{cam:02d}_{label}.mp4"
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", "concat:" + "|".join(tmp), "-c", "copy",
                 *(["-tag:v", "hvc1"] if label == "H265" else []), "-movflags", "+faststart", out])
            for t in tmp:
                os.remove(t)
            print(f"{out} hazır ({os.path.getsize(out) / 1e6:.0f} MB)", flush=True)


if __name__ == "__main__":
    main()

"""Çalışma saatleri dışında mini PC'yi kapatır; kapanmadan önce BIOS uyandırma alarmını sonraki açılışa kurar.

systemd timer ile dakikada bir root olarak çalışır. Ayarlar panelden: settings.schedule
{"enabled": bool, "days": [0=Pzt..6=Paz], "on": "08:30", "off": "19:30"}
Kullanım: zamanlama.py [--dry-run]
"""
import datetime as dt
import json
import os
import subprocess
import sys

DATA = os.environ.get("DVR_DATA", "/var/lib/dvr-ekran")
WAKEALARM = "/sys/class/rtc/rtc0/wakealarm"
BOOT_GRACE_MIN = 30  # mesai dışında elle açılırsa hemen kapanmasın
WAKE_EARLY_MIN = 3   # açılış saatinde ekran hazır olsun


def parse_time(hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.time(h, m)


def is_open(now, s):
    return now.weekday() in s["days"] and parse_time(s["on"]) <= now.time() < parse_time(s["off"])


def next_open(now, s):
    for i in range(8):
        day = (now + dt.timedelta(days=i)).date()
        if day.weekday() in s["days"]:
            t = dt.datetime.combine(day, parse_time(s["on"]))
            if t > now:
                return t
    return None


def main():
    dry = "--dry-run" in sys.argv
    with open(os.path.join(DATA, "config.json"), encoding="utf-8") as f:
        s = json.load(f).get("settings", {}).get("schedule") or {}
    now = dt.datetime.now()
    if not s.get("enabled") or not s.get("days"):
        return print("zamanlama kapalı") if dry else None
    if is_open(now, s):
        return print("mesai içinde, açık kalacak") if dry else None
    with open("/proc/uptime") as f:
        uptime_min = float(f.read().split()[0]) / 60
    wake = next_open(now, s) - dt.timedelta(minutes=WAKE_EARLY_MIN)
    if dry:
        return print(f"mesai dışında; açılış alarmı {wake:%a %d.%m %H:%M}, "
                     f"{'kapanacak' if uptime_min >= BOOT_GRACE_MIN else f'açılışın ilk {BOOT_GRACE_MIN} dk içinde, bekliyor'}")
    if uptime_min < BOOT_GRACE_MIN:
        return
    with open(WAKEALARM, "w") as f:
        f.write("0")
    with open(WAKEALARM, "w") as f:
        f.write(str(int(wake.timestamp())))
    print(f"Mesai dışı: kapanıyor, açılış alarmı {wake:%Y-%m-%d %H:%M}", flush=True)
    subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()

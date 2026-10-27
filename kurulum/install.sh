#!/bin/sh
# Panoptes kurulumu — masaüstü olmadan kurulmuş Debian 12/13 üzerinde, proje klasöründen çalıştırılır:
#   sudo sh kurulum/install.sh [kiosk_kullanıcısı]
# Kiosk kullanıcısı tty1'de otomatik giriş yapar ve ekranı açar (varsayılan: sudo'yu çalıştıran kullanıcı).
set -e

KIOSK_USER="${1:-${SUDO_USER:?Kiosk kullanıcısını verin: sudo sh kurulum/install.sh kullanici}}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OPT=/opt/dvr-ekran
DATA=/var/lib/dvr-ekran

echo "[1/6] Paketler"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-cryptography python3-pil cage chromium curl ffmpeg grim fonts-dejavu-core >/dev/null

echo "[2/6] Servis kullanıcısı ve klasörler"
id dvr >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin dvr
install -d -o dvr -g dvr -m 700 "$DATA"
mkdir -p "$OPT"
rm -rf "$OPT/app"
cp -r "$SRC/app" "$OPT/app"
find "$OPT/app" -name __pycache__ -prune -exec rm -rf {} +
install -m 755 "$SRC/kurulum/kiosk.sh" "$OPT/kiosk.sh"
install -m 755 "$SRC/kurulum/panel-sifre.sh" "$OPT/panel-sifre.sh"
install -m 755 "$SRC/kurulum/sertifika.sh" "$OPT/sertifika.sh"

echo "[3/6] go2rtc"
if [ ! -x "$OPT/go2rtc" ]; then
  curl -fsSL -o "$OPT/go2rtc" https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
  chmod 755 "$OPT/go2rtc"
fi

echo "[4/6] systemd servisleri"
install -m 644 "$SRC/kurulum/dvr-ekran.service" "$SRC/kurulum/go2rtc.service" \
  "$SRC/kurulum/dvr-zamanlama.service" "$SRC/kurulum/dvr-zamanlama.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dvr-ekran.service go2rtc.service dvr-zamanlama.timer
sh "$OPT/sertifika.sh" >/dev/null

echo "[5/6] Kiosk: $KIOSK_USER için tty1 otomatik giriş"
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $KIOSK_USER --noclear %I \$TERM
EOF
HOME_DIR="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
if ! grep -q kiosk.sh "$HOME_DIR/.bash_profile" 2>/dev/null; then
  cat >> "$HOME_DIR/.bash_profile" <<'EOF'
[ -f ~/.bashrc ] && . ~/.bashrc
# tty1 otomatik girişinde kamera ekranını aç
if [ "$(tty)" = /dev/tty1 ]; then exec /opt/dvr-ekran/kiosk.sh; fi
EOF
  chown "$KIOSK_USER:" "$HOME_DIR/.bash_profile"
fi

echo "[6/6] Masaüstü oturumunu kapat (varsa)"
systemctl disable gdm3 >/dev/null 2>&1 || true
systemctl set-default multi-user.target >/dev/null

IP="$(hostname -I | cut -d' ' -f1)"
echo
echo "Kurulum tamam. Yeniden başlatınca ekran açılır."
echo "Yönetim paneli: https://$IP/  (önce bilgisayara http://$IP:8765/ca.crt sertifikasını kurun)"
echo "Panele şifre koymak için: sudo sh $OPT/panel-sifre.sh"

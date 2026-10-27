#!/bin/sh
# Bu bilgisayardaki app/ klasörünü mini PC'ye gönderir, servisleri ve kiosk ekranını yeniden başlatır.
# Kullanım (proje klasöründen): sh kurulum/guncelle.sh [hedef]
set -e
HEDEF="${1:-${DVR_HOST:?Hedef cihazı verin: sh kurulum/guncelle.sh kamera@IP (veya DVR_HOST ortam değişkeni)}}"
cd "$(dirname "$0")/.."

ssh -o BatchMode=yes "$HEDEF" 'rm -rf /tmp/dvr-app'
scp -q -o BatchMode=yes -r app "$HEDEF":/tmp/dvr-app
ssh -o BatchMode=yes "$HEDEF" '
  set -e
  sudo rm -rf /opt/dvr-ekran/app
  sudo cp -r /tmp/dvr-app /opt/dvr-ekran/app
  sudo find /opt/dvr-ekran/app -name __pycache__ -prune -exec rm -rf {} +
  sudo systemctl restart dvr-ekran
  sleep 2
  sudo systemctl restart go2rtc
  pkill -x cage || true
  echo GUNCELLENDI
'

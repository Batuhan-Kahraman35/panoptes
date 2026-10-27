#!/bin/sh
# tty1 otomatik girisinde calisir: panel hazir olunca Chromium kiosk ekranini acar.
until curl -fs http://127.0.0.1:8765/api/version >/dev/null; do sleep 2; done
exec cage -d -- chromium --kiosk --incognito --noerrdialogs --disable-infobars --no-first-run \
  --password-store=basic --autoplay-policy=no-user-gesture-required --disable-features=Translate,TranslateUI --lang=tr \
  --ozone-platform=wayland --disable-accelerated-video-decode \
  http://localhost:8765/display

#!/bin/sh
# Yönetim paneline kullanıcı adı ve şifre koyar (cihazda sudo ile çalıştırılır).
# Şifre ekrana yazılmaz ve sadece /etc/dvr-ekran.env içinde (yalnızca root okuyabilir) tutulur.
set -e

# Windows terminalinden gelen girdide satır sonuna \r eklenebiliyor
ask() { read -r v; printf %s "$v" | tr -d '\r'; }

printf "Panel kullanıcı adı [admin]: "
U=$(ask)
U=${U:-admin}

trap 'stty echo' EXIT
for try in 1 2 3; do
  stty -echo
  printf "Yeni şifre: "
  P1=$(ask)
  printf "\nTekrar: "
  P2=$(ask)
  stty echo
  echo
  if [ -z "$P1" ]; then
    echo "Şifre boş olamaz."
  elif [ "$P1" != "$P2" ]; then
    echo "Şifreler aynı değil, tekrar deneyin ($try/3)."
  elif [ ${#P1} -lt 8 ]; then
    echo "Şifre en az 8 karakter olmalı."
  else
    case "$P1$U" in
      *[\"\'\\\ ]*) echo "Kullanıcı adı ve şifrede tırnak, ters bölü ve boşluk kullanmayın." ;;
      *)
        umask 077
        printf 'ADMIN_USER=%s\nADMIN_PASSWORD=%s\n' "$U" "$P1" > /etc/dvr-ekran.env
        systemctl restart dvr-ekran
        echo "Kaydedildi. Panel artık kullanıcı adı ve şifre soruyor."
        exit 0 ;;
    esac
  fi
done
echo "Şifre ayarlanmadı."
exit 1

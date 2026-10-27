#!/bin/sh
# Yönetim paneline kullanıcı adı ve şifre koyar (cihazda sudo ile çalıştırılır).
# Şifre ekrana yazılmaz ve sadece /etc/dvr-ekran.env içinde (yalnızca root okuyabilir) tutulur.
set -e

printf "Panel kullanıcı adı [admin]: "
read -r U
U=${U:-admin}
stty -echo
printf "Yeni şifre: "
read -r P1
printf "\nTekrar: "
read -r P2
stty echo
echo

[ -n "$P1" ] || { echo "Şifre boş olamaz."; exit 1; }
[ "$P1" = "$P2" ] || { echo "Şifreler aynı değil."; exit 1; }
[ ${#P1} -ge 8 ] || { echo "Şifre en az 8 karakter olmalı."; exit 1; }
case "$P1$U" in
  *[\"\'\\\ ]*) echo "Kullanıcı adı ve şifrede tırnak, ters bölü ve boşluk kullanmayın."; exit 1 ;;
esac

umask 077
printf 'ADMIN_USER=%s\nADMIN_PASSWORD=%s\n' "$U" "$P1" > /etc/dvr-ekran.env
systemctl restart dvr-ekran
echo "Kaydedildi. Panel artık kullanıcı adı ve şifre soruyor."

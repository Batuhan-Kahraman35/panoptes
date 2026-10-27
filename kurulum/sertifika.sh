#!/bin/sh
# Panel için HTTPS sertifikası üretir (cihazda sudo ile çalıştırılır):
#   - yerel bir sertifika yetkilisi (CA): bir kez üretilir, panele girilen bilgisayarlara bir kez kurulur,
#   - cihazın IP'leri ve isimleri için o CA'nın imzaladığı sunucu sertifikası.
# IP değişince ya da Tailscale eklenince tekrar çalıştırmak yeterli; CA aynı kaldığı için tarayıcı tarafında bir şey gerekmez.
set -e
DIR=/var/lib/dvr-ekran/tls
install -d -o dvr -g dvr -m 750 "$DIR"
cd "$DIR"

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -keyout ca.key -out ca.crt \
    -subj "/CN=Panoptes Yerel CA ($(hostname))" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  echo "Yeni CA üretildi."
fi

SAN="DNS:localhost,DNS:$(hostname),DNS:$(hostname).local"
for ip in $(hostname -I); do SAN="$SAN,IP:$ip"; done
TSNAME=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
[ -n "$TSNAME" ] && SAN="$SAN,DNS:$TSNAME"

openssl req -newkey rsa:2048 -nodes -sha256 -keyout server.key -out server.csr -subj "/CN=$(hostname)" 2>/dev/null
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
  "$SAN" > server.ext
openssl x509 -req -sha256 -days 825 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -extfile server.ext 2>/dev/null
rm -f server.csr server.ext

chown root:root ca.key ca.srl && chmod 600 ca.key
chown dvr:dvr server.key server.crt ca.crt && chmod 600 server.key && chmod 644 server.crt ca.crt
systemctl restart dvr-ekran

echo "Sertifika hazır: $SAN"
echo "Bilgisayara kurulacak CA: http://$(hostname -I | cut -d' ' -f1):8765/ca.crt"

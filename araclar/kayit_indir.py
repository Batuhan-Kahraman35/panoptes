"""Hikvision DVR'dan kaydı olduğu gibi (orijinal) indirir. Mini PC'de sudo ile çalışır.
Kullanım: kayit_indir.py ŞUBE KANAL "2026-10-26 03:30:00" "2026-10-26 04:30:00" çıktı_öneki
Aralık birden fazla kayıt parçasına denk geliyorsa her parça ayrı dosyaya iner (önek_1.ps, önek_2.ps ...).
"""
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.request
import uuid

sys.path.insert(0, "/opt/dvr-ekran/app")
import isapi  # noqa: E402

FMT_SEARCH = "%Y-%m-%dT%H:%M:%SZ"
FMT_URI = "%Y%m%dT%H%M%SZ"  # Hikvision 'Z' yazar ama DVR yerel saati olarak yorumlar


def segments(cli, track, start, end):
    body = (f'<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription><searchID>{uuid.uuid4()}</searchID>'
            f"<trackIDList><trackID>{track}</trackID></trackIDList><timeSpanList><timeSpan>"
            f"<startTime>{start.strftime(FMT_SEARCH)}</startTime><endTime>{end.strftime(FMT_SEARCH)}</endTime>"
            "</timeSpan></timeSpanList><maxResults>50</maxResults><searchResultPostion>0</searchResultPostion>"
            "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
            "</CMSearchDescription>")
    raw = cli.request("/ISAPI/ContentMgmt/search", "POST", body.encode()).decode("utf-8", "ignore")
    return [html.unescape(u) for u in re.findall(r"<playbackURI>([^<]+)<", raw)]


def clip_uri(uri, start, end):
    """Parçanın başlangıç/bitişini istenen aralıkla kesiştirir (name/size korunur).
    Not: Test edilen DVR'lar (DS-7108HGHI-M1) bu aralığı yok sayıp parçanın tamamını gönderiyor; bu yüzden
    dosyanın gerçek sınırları olarak parçanın kendi başlangıç/bitişi döner."""
    s = dt.datetime.strptime(re.search(r"starttime=(\w+)", uri).group(1), FMT_URI)
    e = dt.datetime.strptime(re.search(r"endtime=(\w+)", uri).group(1), FMT_URI)
    cs, ce = max(s, start), min(e, end)
    uri = re.sub(r"starttime=\w+", f"starttime={cs.strftime(FMT_URI)}", uri)
    return re.sub(r"endtime=\w+", f"endtime={ce.strftime(FMT_URI)}", uri), s, e


def download(cli, uri, out):
    path = "/ISAPI/ContentMgmt/download"
    body = (f'<?xml version="1.0" encoding="UTF-8"?><downloadRequest version="1.0" '
            f'xmlns="http://www.isapi.org/ver20/XMLSchema"><playbackURI>{html.escape(uri)}</playbackURI>'
            "</downloadRequest>").encode()
    cli.device_info()  # Digest challenge'ı tazele
    req = urllib.request.Request(cli.base + path, data=body, method="GET")
    req.add_header("Content-Type", "application/xml; charset=UTF-8")
    req.add_header("Authorization", cli._auth_header("GET", path))
    sha, size = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def main():
    name, ch, start, end, prefix = sys.argv[1], int(sys.argv[2]), *sys.argv[3:6]
    start, end = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    cfg = json.load(open("/var/lib/dvr-ekran/config.json", encoding="utf-8"))
    b = next(x for x in cfg["branches"] if x["name"].upper().startswith(name.upper()))
    cli = isapi.Isapi(b["ip"], b["http_port"], b["username"], b["password"], timeout=60)
    uris = segments(cli, ch * 100 + 1, start, end)
    if not uris:
        sys.exit("Bu aralıkta kayıt bulunamadı")
    for i, uri in enumerate(uris, 1):
        part, s, e = clip_uri(uri, start, end)
        out = f"{prefix}_{i}.ps"
        size, digest = download(cli, part, out)
        print(f"{out}\t{s:%Y-%m-%d %H:%M:%S}\t{e:%H:%M:%S}\t{size}\t{digest}", flush=True)


if __name__ == "__main__":
    main()

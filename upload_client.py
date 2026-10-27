#!/usr/bin/env python3
"""Upload file besar ke app (resumable). Hanya butuh Python 3, tanpa library tambahan.

Contoh:
  python upload_client.py video_besar.mp4 --url https://xxxx.trycloudflare.com --token TOKENMU
  python upload_client.py *.mp4 --url http://IP-SERVER:8502 --token TOKENMU --hint video

Jika terputus / Ctrl+C, jalankan perintah yang sama lagi: upload lanjut dari byte terakhir.
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def call(method, url, token, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method, headers={"X-Token": token})
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body


def fmt(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


def upload(path, base, token, hint, chunk):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    q = urllib.parse.quote(name)
    code, st = call("GET", f"{base}/status?name={q}", token)
    if code == 401:
        sys.exit("Token salah.")
    if code != 200:
        sys.exit(f"Server menolak: {code} {st}")
    offset = st["offset"]
    if offset > size:
        call("POST", f"{base}/abort?name={q}", token)
        offset = 0
    if offset:
        print(f"  lanjut dari {fmt(offset)}")
    t0, start_off, fails = time.time(), offset, 0
    with open(path, "rb") as f:
        while offset < size:
            f.seek(offset)
            data = f.read(chunk)
            try:
                code, r = call("PUT", f"{base}/chunk?name={q}&offset={offset}", token, data, timeout=300)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                code, r = 0, {"error": str(e)}
            if code == 200:
                offset, fails = r["offset"], 0
            elif code == 409:                       # posisi server beda -> ikuti server
                offset = r["offset"]
            elif code in (401, 507):
                sys.exit(f"Dihentikan: {r.get('error', code)}")
            else:
                fails += 1
                if fails > 12:
                    sys.exit(f"Gagal terus: {r.get('error', code)}. Jalankan lagi untuk melanjutkan.")
                wait = min(2 ** fails, 30)
                print(f"\n  gangguan ({r.get('error', code)}), coba lagi {wait}s...")
                time.sleep(wait)
                try:
                    code, st = call("GET", f"{base}/status?name={q}", token)
                    if code == 200:
                        offset = st["offset"]
                except Exception:
                    pass
                continue
            speed = (offset - start_off) / max(time.time() - t0, 0.001)
            print(f"\r  {offset / size * 100:5.1f}%  {fmt(offset)} / {fmt(size)}  {fmt(speed)}/s   ", end="", flush=True)
    print()
    hint_q = urllib.parse.quote(hint)
    code, r = call("POST", f"{base}/finish?name={q}&hint={hint_q}&size={size}", token, timeout=600)
    if code != 200:
        sys.exit(f"Finalisasi gagal: {code} {r}")
    print(f"  selesai -> {r['kind']}/{r['saved_as']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--url", required=True, help="mis. http://IP:8502 atau https://xxxx.trycloudflare.com")
    ap.add_argument("--token", default=os.environ.get("UPLOAD_TOKEN"), required=not os.environ.get("UPLOAD_TOKEN"))
    ap.add_argument("--hint", default="auto", choices=["auto", "video", "audio"])
    ap.add_argument("--chunk-mb", type=int, default=8, help="ukuran potongan (default 8 MB)")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    paths = [p for pat in a.files for p in (glob.glob(pat) or [pat])]
    for p in paths:
        if not os.path.isfile(p):
            print(f"lewati (bukan file): {p}")
            continue
        print(f"{os.path.basename(p)}")
        upload(p, base, a.token, a.hint, a.chunk_mb * 1024 * 1024)


if __name__ == "__main__":
    main()

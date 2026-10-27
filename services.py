"""Backend untuk app Streamlit: library, impor, job queue, render, merge, live stream.

Membutuhkan di folder yang sama:
  render.py  (script render kamu, dipakai apa adanya)
  marge.py   (MARGE.PY kamu, di-rename huruf kecil)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

try:                      # dipakai untuk gain PCM agar volume bisa diubah saat live
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

BASE = Path("data").resolve()
DIRS = {
    "video": BASE / "videos",
    "audio": BASE / "audios",
    "output": BASE / "output",
    "thumb": BASE / "thumbnails",
    "other": BASE / "other",
}
TMP = BASE / "tmp"
LOG_FILE = BASE / "ffmpeg.log"
for _d in (*DIRS.values(), TMP):
    _d.mkdir(parents=True, exist_ok=True)

# Agar yt-dlp menemukan `deno` (dipasang lewat pip) di virtualenv Streamlit Cloud.
for _p in (str(Path(sys.executable).parent), str(Path.home() / ".deno" / "bin")):
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _p

for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".wmv", ".ts", ".3gp"}
AUDIO_EXTS = {".mp3", ".aac", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".wma", ".aiff"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PARTIAL_EXTS = {".part", ".ytdl", ".tmp", ".temp"}


def tool_status() -> dict:
    """Status komponen eksternal untuk panel diagnosa di sidebar."""
    try:
        import yt_dlp.version as v
        ytdlp = v.__version__
    except Exception:  # noqa: BLE001
        ytdlp = None
    return {"ffmpeg": shutil.which("ffmpeg") is not None,
            "deno": shutil.which("deno") is not None,
            "yt-dlp": ytdlp}


# ───────────────────────── library ─────────────────────────
def safe_name(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^\w\-]+", "_", stem.encode("ascii", "ignore").decode()).strip("_") or "file"
    return f"{stem[:80]}.{ext.lower()}" if ext else stem[:80]


def unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    stem, ext, n = target.stem, target.suffix, 1
    while target.exists():
        target = directory / f"{stem}_{n}{ext}"
        n += 1
    return target


def list_paths(kind: str) -> list[Path]:
    return sorted((p for p in DIRS[kind].iterdir() if p.is_file() and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())


def list_files(kind: str) -> list[dict]:
    rows = []
    for p in list_paths(kind):
        st = p.stat()
        rows.append({"name": p.name, "mb": st.st_size / 1024 / 1024,
                     "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m %H:%M")})
    return rows


def library_videos() -> list[Path]:
    """Semua file video yang bisa dipakai (folder videos + output)."""
    return [p for k in ("video", "output") for p in list_paths(k) if p.suffix.lower() in VIDEO_EXTS]


def label(p: Path) -> str:
    return f"{p.parent.name}/{p.name}"


def delete_files(paths, protected=()) -> int:
    protected = {Path(p).resolve() for p in protected if p}
    n = 0
    for p in paths:
        p = Path(p).resolve()
        if BASE not in p.parents or p in protected:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def move_files(paths, dest_kind: str, protected=()) -> int:
    protected = {Path(p).resolve() for p in protected if p}
    n = 0
    for p in paths:
        p = Path(p).resolve()
        if BASE not in p.parents or p in protected:
            continue
        try:
            shutil.move(str(p), str(unique_path(DIRS[dest_kind], p.name)))
            n += 1
        except OSError:
            pass
    return n


def clear_tmp() -> None:
    for p in TMP.iterdir():
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


def disk_info() -> dict:
    usage = shutil.disk_usage(BASE)
    used = sum(f.stat().st_size for f in BASE.rglob("*") if f.is_file())
    return {"free_gb": usage.free / 1024**3, "data_gb": used / 1024**3}


# ───────────────────────── klasifikasi file otomatis ─────────────────────────
def _probe_streams(path: Path) -> tuple[bool, bool]:
    """(punya video asli, punya audio). Cover-art MP3 tidak dihitung sebagai video."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:  # noqa: BLE001
        return False, False
    has_v = any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
                for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    return has_v, has_a


def detect_kind(path: Path, hint: str = "auto") -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:          # gambar (thumbnail) selalu ke folder thumbnail
        return "thumb"
    if hint in ("video", "audio"):
        return hint
    if ext in AUDIO_EXTS:
        return "audio"
    has_v, has_a = _probe_streams(path)
    if has_v:
        return "video"
    if has_a:
        return "audio"
    return "video" if ext in VIDEO_EXTS else "other"


def place_file(path: Path, hint: str = "auto") -> tuple[str, Path]:
    kind = detect_kind(path, hint)
    dest = unique_path(DIRS[kind], safe_name(path.name))
    shutil.move(str(path), str(dest))
    return kind, dest


def import_bytes(name: str, data, hint: str = "auto") -> tuple[str, Path]:
    stage = Path(tempfile.mkdtemp(prefix="up_", dir=TMP))
    try:
        tmp = stage / safe_name(name)
        tmp.write_bytes(data)
        return place_file(tmp, hint)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ───────────────────────── job queue ─────────────────────────
class JobRunner:
    """Antrean background dengan N worker."""

    def __init__(self, workers: int = 1):
        self.q: queue.Queue = queue.Queue()
        self.jobs: list[dict] = []
        self.lock = threading.Lock()
        for _ in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, title: str, fn, cleanup=None) -> dict:
        job = {"id": uuid.uuid4().hex[:6], "title": title, "status": "antre", "detail": "",
               "created": time.time(), "started": None, "ended": None, "fn": fn,
               "cleanup": cleanup}
        with self.lock:
            self.jobs.append(job)
            self.jobs = self.jobs[-200:]
        self.q.put(job)
        return job

    def _worker(self):
        while True:
            job = self.q.get()
            with self.lock:
                if job["status"] != "antre":
                    continue
                job["status"], job["started"] = "berjalan", time.time()
            try:
                ok, info = job["fn"](job)
            except Exception as e:  # noqa: BLE001
                ok, info = False, f"{type(e).__name__}: {e}"
            finally:
                if job.get("cleanup"):
                    job["cleanup"]()
            job["detail"] = str(info)[:600]
            job["ended"] = time.time()
            job["status"] = "selesai" if ok else "gagal"

    def snapshot(self) -> list[dict]:
        with self.lock:
            rows = []
            for j in reversed(self.jobs):
                end = j["ended"] or time.time()
                dur = f"{int(end - j['started'])}s" if j["started"] else "-"
                rows.append({"ID": j["id"], "Job": j["title"], "Status": j["status"],
                             "Durasi": dur, "Info": j["detail"]})
            return rows

    def counts(self) -> dict:
        with self.lock:
            c = {"antre": 0, "berjalan": 0, "selesai": 0, "gagal": 0}
            for j in self.jobs:
                if j["status"] in c:
                    c[j["status"]] += 1
            return c

    def cancel_queued(self) -> int:
        n = 0
        with self.lock:
            for j in self.jobs:
                if j["status"] == "antre":
                    j["status"] = "dibatalkan"
                    if j.get("cleanup"):
                        j["cleanup"]()
                    n += 1
        return n

    def retry_failed(self) -> int:
        with self.lock:
            failed = [j for j in self.jobs if j["status"] == "gagal"]
            for j in failed:
                j["status"] = "diulang"
        for j in failed:
            self.submit(j["title"], j["fn"])
        return len(failed)

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["status"] in ("antre", "berjalan")]

    def pending(self) -> int:
        c = self.counts()
        return c["antre"] + c["berjalan"]


# ───────────────────────── impor dari link ─────────────────────────
URL_RE = re.compile(r"https?://[^\s,;<>\"'\)\]]+", re.I)
SOURCE_LABEL = {"gdrive_file": "Google Drive · file", "gdrive_folder": "Google Drive · folder",
                "web": "Web / yt-dlp"}

# ───────────────────────── cookies yt-dlp ─────────────────────────
def parse_cookies(text: str) -> tuple[str, int, list[str]]:
    """Terima cookies JSON (Cookie-Editor / EditThisCookie) atau Netscape .txt.

    Return (teks_netscape, jumlah_cookie, daftar_domain). Raise ValueError jika tidak valid.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("kosong")

    # Format Netscape (cookies.txt) — dipakai apa adanya setelah dicek singkat.
    if not text.startswith(("[", "{")):
        rows, domains = [], []
        for line in text.splitlines():
            raw = line.strip()
            if not raw or (raw.startswith("#") and not raw.startswith("#HttpOnly_")):
                continue
            parts = raw.split("\t")
            if len(parts) != 7:
                raise ValueError("format bukan JSON dan bukan Netscape (harus 7 kolom dipisah TAB)")
            rows.append(raw)
            domains.append(parts[0].replace("#HttpOnly_", "").lstrip("."))
        if not rows:
            raise ValueError("tidak ada cookie")
        out = "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"
        return out, len(rows), sorted(set(domains))

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON rusak ({e.msg}, baris {e.lineno}). Pastikan menempel seluruh isi dari '[' sampai ']'")
    if isinstance(data, dict):
        data = data.get("cookies", [data])
    if not isinstance(data, list) or not data:
        raise ValueError("JSON harus berupa daftar cookie")

    lines, domains = [], []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c or not c.get("domain"):
            raise ValueError("tiap cookie harus punya name, value, dan domain")
        domain = str(c["domain"])
        host_only = bool(c.get("hostOnly", not domain.startswith(".")))
        flag = "FALSE" if host_only else "TRUE"
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        if c.get("httpOnly"):
            domain_out = "#HttpOnly_" + domain
        else:
            domain_out = domain
        exp = c.get("expirationDate", c.get("expires", 0)) or 0
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = 0
        value = str(c["value"]).replace("\t", " ").replace("\n", "").replace("\r", "")
        lines.append("\t".join([domain_out, flag, str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)),
                                str(c["name"]), value]))
        domains.append(domain.lstrip("."))
    out = "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
    return out, len(lines), sorted(set(domains))


class CookieBox:
    """Simpan cookies di memori; tulis ke file sementara hanya saat dipakai.

    Dipakai bersama oleh beberapa job (satu batch). Setelah semua job melepas
    (release), isi cookies dihapus dari memori.
    """

    def __init__(self, netscape: str):
        self._text = netscape
        self._refs = 0
        self._lock = threading.Lock()

    def make_releaser(self):
        with self._lock:
            self._refs += 1
        done = {"v": False}

        def release():
            with self._lock:
                if done["v"]:
                    return
                done["v"] = True
                self._refs -= 1
                if self._refs <= 0:
                    self._text = None
        return release

    def write_to(self, directory: Path) -> Path | None:
        with self._lock:
            text = self._text
        if not text:
            return None
        path = directory / f"ck_{uuid.uuid4().hex[:8]}.txt"
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path


def parse_urls(text: str) -> list[str]:
    """Ambil semua URL dari teks bebas (dipisah koma, spasi, atau baris baru), tanpa duplikat."""
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def classify_url(url: str) -> str:
    if "drive.google.com" in url or "drive.usercontent.google.com" in url:
        return "gdrive_folder" if "/folders/" in url else "gdrive_file"
    return "web"


def short_title(url: str) -> str:
    kind = classify_url(url)
    if kind == "gdrive_file":
        m = re.search(r"/d/([\w-]+)|[?&]id=([\w-]+)", url)
        fid = (m.group(1) or m.group(2)) if m else url[-8:]
        return f"Drive file {fid[:10]}…"
    if kind == "gdrive_folder":
        m = re.search(r"/folders/([\w-]+)", url)
        return f"Drive folder {(m.group(1) if m else url[-8:])[:10]}…"
    return re.sub(r"^https?://(www\.)?", "", url)[:48]


# ───────────────────────── Google Drive (tanpa batas ukuran dari app) ─────────────────────────
GD_BASE = "https://drive.usercontent.google.com"
GD_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/124.0 Safari/537.36")


class DriveError(Exception):
    """Error pasti (tidak berguna dicoba ulang): private, kuota, tidak ditemukan, disk penuh."""


def _gd_file_id(url: str) -> str | None:
    for pat in (r"/file/d/([\w-]{10,})", r"/d/([\w-]{10,})", r"[?&]id=([\w-]{10,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _cd_filename(headers) -> str | None:
    from urllib.parse import unquote
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    return m.group(1).strip() if m else None


def _gd_classify_html(page: str, status: int) -> None:
    """Halaman HTML dari Drive berarti ada masalah (atau halaman konfirmasi virus-scan)."""
    low = page.lower()
    if "quota exceeded" in low or "too many users" in low or "download quota" in low:
        raise DriveError("Kuota download Drive untuk file ini habis (biasanya pulih ±24 jam). "
                         "Solusi: buka file di Drive → 'Buat salinan' ke Drive-mu sendiri, lalu pakai link salinan itu.")
    if status == 404 or "not found" in low[:3000] or "can't be found" in low or "cannot be found" in low:
        raise DriveError("File tidak ditemukan. Cek link-nya (atau file sudah dihapus).")
    if "accounts.google.com" in low or "servicelogin" in low or "sign in" in low[:4000] \
            or status in (401, 403):
        raise DriveError("File tidak publik. Di Drive: Bagikan → Akses umum → 'Siapa saja yang memiliki link'.")


def _gd_confirm_request(sess, page: str):
    """Dari halaman peringatan virus-scan, bangun request lanjutan. None jika bukan halaman itu."""
    import html as _html
    from urllib.parse import urljoin
    m = re.search(r'<form[^>]*id="download-form"[^>]*>', page)
    if m:
        a = re.search(r'action="([^"]+)"', m.group(0))
        if a:
            params = {}
            for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", page):
                n, v = re.search(r'name="([^"]*)"', tag), re.search(r'value="([^"]*)"', tag)
                if n:
                    params[_html.unescape(n.group(1))] = _html.unescape(v.group(1)) if v else ""
            return urljoin(GD_BASE, _html.unescape(a.group(1))), params
    m = re.search(r'href="(/uc\?export=download[^"]+)"', page)
    if m:
        return urljoin("https://drive.google.com", _html.unescape(m.group(1))), {}
    return None


def _gd_download_id(job: dict, fid: str, stage: Path, disk_min_gb: float, label_txt: str = "") -> Path:
    """Unduh satu file Drive via stream, otomatis lanjut (Range) jika koneksi putus. Tanpa batas ukuran."""
    import requests

    sess = requests.Session()
    sess.headers["User-Agent"] = GD_UA
    to = (20, 90)

    def first():
        r = sess.get(f"{GD_BASE}/download", params={"id": fid, "export": "download", "confirm": "t"},
                     stream=True, timeout=to)
        for _ in range(3):
            if "text/html" not in r.headers.get("Content-Type", ""):
                return r
            page = r.text
            _gd_classify_html(page, r.status_code)
            nxt = _gd_confirm_request(sess, page)
            if not nxt:
                raise DriveError("Drive membalas halaman yang tidak dikenal (cek apakah link bisa dibuka tanpa login).")
            r = sess.get(nxt[0], params=nxt[1], stream=True, timeout=to)
        if "text/html" in r.headers.get("Content-Type", ""):
            _gd_classify_html(r.text, r.status_code)
            raise DriveError("Drive terus meminta konfirmasi; coba lagi nanti.")
        return r

    r = first()
    if r.status_code >= 400:
        raise DriveError(f"Drive menolak (HTTP {r.status_code}).")
    name = safe_name(_cd_filename(r.headers) or f"drive_{fid}")
    part = stage / (name + ".part")
    total = int(r.headers.get("Content-Length", "0") or 0)
    final_url = r.url
    have, t0, last_ui, last_disk = 0, time.time(), 0.0, 0
    attempts = 0
    while True:
        try:
            mode = "ab" if have else "wb"
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    have += len(chunk)
                    if have - last_disk >= 64 * 1024 * 1024:
                        last_disk = have
                        if shutil.disk_usage(BASE).free < disk_min_gb * 1024**3:
                            raise DriveError("Disk server hampir penuh; unduhan dihentikan. Hapus file dulu.")
                    now = time.time()
                    if now - last_ui > 0.7:
                        last_ui = now
                        sp = have / max(now - t0, 0.1)
                        pct = f" ({have / total * 100:.0f}%)" if total else ""
                        tot = f" / {total / 1024**3:.2f} GB" if total else ""
                        job["detail"] = (f"{label_txt}{have / 1024**3:.2f} GB{tot}{pct} · "
                                         f"{sp / 1024**2:.1f} MB/s")
            if total and have < total:
                raise ConnectionError("koneksi terputus sebelum selesai")
            break
        except DriveError:
            raise
        except Exception as e:  # noqa: BLE001  (putus jaringan, timeout, dll → lanjutkan dari byte terakhir)
            attempts += 1
            if attempts > 10:
                raise DriveError(f"Terputus terus-menerus setelah {have / 1024**2:.0f} MB: {e}")
            job["detail"] = f"{label_txt}terputus di {have / 1024**2:.0f} MB, melanjutkan ({attempts}/10)..."
            time.sleep(min(3 * attempts, 20))
            try:
                r = sess.get(final_url, headers={"Range": f"bytes={have}-"}, stream=True, timeout=to)
                if "text/html" in r.headers.get("Content-Type", ""):
                    r = first()           # link/cookie kedaluwarsa → mulai dari konfirmasi lagi
                    have = 0
                elif r.status_code == 200:    # server tak mendukung Range → ulang dari awal
                    have = 0
            except Exception:  # noqa: BLE001
                continue
    dest = stage / name
    part.rename(dest)
    return dest


def _friendly_drive_error(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


def _gdrive(job: dict, url: str, stage: Path, folder: bool, disk_min_gb: float = 0.3) -> tuple[bool, str]:
    try:
        if not folder:
            fid = _gd_file_id(url)
            if not fid:
                return False, "ID file tidak ditemukan di link."
            try:
                _gd_download_id(job, fid, stage, disk_min_gb)
                return True, ""
            except DriveError as e:
                return False, str(e)
            except Exception as e:  # noqa: BLE001
                err = _friendly_drive_error(e)
            # cadangan: gdown
            import gdown
            try:
                job["detail"] = "Metode utama gagal, mencoba gdown..."
                out = gdown.download(url=url, output=str(stage) + "/", quiet=True, fuzzy=True)
                return (True, "") if out else (False, err)
            except Exception as e2:  # noqa: BLE001
                return False, f"{err} | gdown: {_friendly_drive_error(e2)}"

        import gdown
        entries = None
        try:                                   # daftar isi folder saja, lalu unduh tiap file dengan downloader sendiri
            entries = gdown.download_folder(url=url, quiet=True, skip_download=True, remaining_ok=True)
        except TypeError:
            entries = None
        except Exception as e:  # noqa: BLE001
            return False, "Folder tidak bisa dibaca: " + _friendly_drive_error(e)
        if entries is None:
            files = gdown.download_folder(url=url, output=str(stage), quiet=True, remaining_ok=True)
            return (True, "") if files else (False, "Folder kosong atau tidak bisa diakses.")
        if not entries:
            return False, "Folder kosong atau tidak publik."
        fails = []
        for n, ent in enumerate(entries, 1):
            try:
                _gd_download_id(job, ent.id, stage, disk_min_gb, label_txt=f"[{n}/{len(entries)}] ")
            except DriveError as e:
                fails.append(f"{Path(ent.path).name}: {e}")
        if fails and len(fails) == len(entries):
            return False, fails[0]
        return True, (f"{len(fails)} file gagal" if fails else "")
    except DriveError as e:
        return False, str(e)


def _ytdlp(job: dict, url: str, stage: Path, hint: str, height: int, noplaylist: bool,
           audio_opts: dict | None = None, cookies: "CookieBox | None" = None) -> tuple[bool, str]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["detail"] = f"Mengunduh {d.get('downloaded_bytes', 0) / total * 100:.0f}%"
        elif d.get("status") == "finished":
            job["detail"] = "Memproses file..."

    opts = {
        "outtmpl": str(stage / "%(title).80s-%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [hook],
    }
    ao = {"codec": "mp3", "bitrate": "192", "embed": True, "thumb": True, **(audio_opts or {})}
    cookie_file = None
    if cookies is not None:
        cookie_file = cookies.write_to(stage)
        if cookie_file is not None:
            opts["cookiefile"] = str(cookie_file)

    def attempt(extra_audio: bool) -> None:
        o = dict(opts)
        if hint == "audio":
            o["format"] = "bestaudio/best"
            pps = [{"key": "FFmpegExtractAudio", "preferredcodec": ao["codec"],
                    "preferredquality": str(ao["bitrate"])}]
            if extra_audio and (ao["embed"] or ao["thumb"]):
                o["writethumbnail"] = True
                pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
                if ao["embed"]:
                    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
                    pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": bool(ao["thumb"])})
            o["postprocessors"] = pps
        else:
            h = height
            o["format"] = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
                           f"bv*[height<={h}]+ba/b[height<={h}]/b")
            o["merge_output_format"] = "mp4"
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.extract_info(url, download=True)

    try:
        try:
            attempt(extra_audio=True)
        except Exception:  # noqa: BLE001
            if hint != "audio":
                raise
            for f in stage.glob("*"):          # buang sisa percobaan gagal, ulang lebih sederhana
                if f != cookie_file:
                    f.unlink(missing_ok=True)
            job["detail"] = "Cover/thumbnail gagal, mengulang tanpa itu..."
            attempt(extra_audio=False)
        return True, ""
    except Exception as e:  # noqa: BLE001
        clean = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        return False, "yt-dlp: " + clean[:350]
    finally:
        if cookie_file is not None:
            cookie_file.unlink(missing_ok=True)


def run_download(job: dict, url: str, hint: str = "auto", height: int = 720,
                 noplaylist: bool = True, disk_min_gb: float = 0.3,
                 audio_opts: dict | None = None, cookies: "CookieBox | None" = None):
    """Unduh 1 link (Drive file/folder atau web/yt-dlp), lalu sortir otomatis ke folder yang tepat."""
    free = shutil.disk_usage(BASE).free
    if free < disk_min_gb * 1024**3:
        return False, f"Disk kurang ({free / 1024**3:.2f} GB). Hapus file dulu."
    stage = Path(tempfile.mkdtemp(prefix="dl_", dir=TMP))
    try:
        src = classify_url(url)
        job["detail"] = f"{SOURCE_LABEL[src]} · mengunduh..."
        if src == "web":
            ok, msg = _ytdlp(job, url, stage, hint, height, noplaylist, audio_opts, cookies)
        else:
            ok, msg = _gdrive(job, url, stage, folder=(src == "gdrive_folder"), disk_min_gb=disk_min_gb)
        if not ok:
            return False, msg
        files = [p for p in stage.rglob("*")
                 if p.is_file() and p.suffix.lower() not in PARTIAL_EXTS and p.stat().st_size > 0]
        if not files:
            return False, "Tidak ada file yang berhasil diunduh."
        job["detail"] = f"Menyortir {len(files)} file..."
        placed = []
        for f in files:
            kind, dest = place_file(f, hint)
            placed.append(f"{dest.name} → {kind}")
        return True, "; ".join(placed)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ───────────────────────── render (pakai render.py) ─────────────────────────
def run_render(job: dict, video: Path, audio: Path, mode: str, opts: dict, disk_min_gb: float):
    try:
        import render  # render.py milikmu — harus ada di folder yang sama dengan app.py
    except ImportError as e:
        return False, f"render.py tidak ditemukan/gagal diimpor di repo: {e}"
    import traceback

    video, audio = Path(video), Path(audio)
    for f, nm in ((video, "Video"), (audio, "Audio")):
        if not f.is_file():
            return False, f"{nm} tidak ada lagi di server: {f.name} (disk Streamlit Cloud bisa ter-reset)."
        if f.stat().st_size == 0:
            return False, f"{nm} berukuran 0 byte: {f.name}"

    min_bytes = int(disk_min_gb * 1024**3)
    free = shutil.disk_usage(BASE).free
    if free < min_bytes:
        return False, f"Disk kurang ({free / 1024**3:.2f} GB < {disk_min_gb} GB). Hapus file dulu."
    try:
        out = render.safe_out_name(DIRS["output"], audio.stem)
        job["detail"] = f"Render → {out.name}"
        # iphone_meta=False: metadata sumber dipertahankan apa adanya.
        if mode == "fast":
            vbr_k = int(opts["vbr_k"])
            ok, info = render.render_fast(
                video_path=video, audio_path=audio, out_path=out, tmp_dir=str(TMP),
                vbr=f"{vbr_k}k", fps=int(opts["fps"]), scale=int(opts["scale"]),
                maxrate=f"{int(vbr_k * 1.2)}k", bufsize=f"{vbr_k * 2}k",
                disk_min_bytes=min_bytes, iphone_meta=False)
        else:
            ok, info = render.render_copy(
                video_path=video, audio_path=audio, out_path=out, tmp_dir=str(TMP),
                disk_min_bytes=min_bytes, iphone_meta=False)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e} | {traceback.format_exc()[-500:]}"
    if not ok:
        out.unlink(missing_ok=True)
        return False, info
    return True, f"{out.name} | {info}"


# ───────────────────────── merge (pakai marge.py) ─────────────────────────
class MergeError(RuntimeError):
    pass


def _raise(message, exit_code=1):
    raise MergeError(message)


def run_merge(job: dict, opening: Path, ending: Path, out_name: str, mode: str, disk_min_gb: float):
    import marge  # marge.py milikmu (MARGE.PY di-rename)

    marge.error = _raise  # error() asli memanggil SystemExit; ganti agar bisa ditangkap
    free = shutil.disk_usage(BASE).free
    if free < disk_min_gb * 1024**3:
        return False, f"Disk kurang ({free / 1024**3:.2f} GB)."
    work = Path(tempfile.mkdtemp(prefix=".marge_vid_work_", dir=TMP))
    output = unique_path(DIRS["output"], safe_name(out_name))
    try:
        marge.ensure_tools()
        oi, ei = marge.probe(Path(opening)), marge.probe(Path(ending))
        if oi is None:
            return False, f"Opening tidak valid: {Path(opening).name}"
        if ei is None:
            return False, f"Ending tidak valid: {Path(ending).name}"
        compatible = marge.stream_signature(oi) == marge.stream_signature(ei)
        if mode == "copy" and not compatible:
            return False, "Mode copy dipilih tetapi format opening & ending berbeda."
        actual = "fast" if (mode == "fast" or (mode == "auto" and not compatible)) else "copy"
        job["detail"] = f"{actual.upper()}: {Path(opening).name} + {Path(ending).name}"
        if actual == "copy":
            marge.merge_copy(Path(opening), Path(ending), output, work)
        else:
            aligned = work / "opening_aligned.mp4"
            marge.normalize_fast(Path(opening), aligned, oi, ei, work)
            marge.merge_copy(aligned, Path(ending), output, work)
        ri = marge.probe(output)
        if ri is None or ri["duration"] <= 0:
            output.unlink(missing_ok=True)
            return False, "Output tidak terbaca (korup)."
        if marge.essential_signature(ri) != marge.essential_signature(ei):
            output.unlink(missing_ok=True)
            return False, "Parameter output tidak cocok dengan ending."
        mb = output.stat().st_size / 1024 / 1024
        return True, f"{output.name} | {int(ri['duration'])}s | {mb:.1f} MB | mode {actual}"
    except MergeError as e:
        output.unlink(missing_ok=True)
        return False, str(e)[-400:]
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ───────────────────────── live stream ─────────────────────────
# ───────────────────────── LIVE: analisis & persiapan media ─────────────────────────
LIVE_DIR = BASE / "live"
CACHE_DIR = LIVE_DIR / "cache"
for _d in (LIVE_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
NICE = ["nice", "-n", "10"] if shutil.which("nice") else []
RES = {480: (854, 480), 720: (1280, 720), 1080: (1920, 1080)}


def media_info(path) -> dict:
    """Ringkasan ffprobe: durasi, signature stream, info video/audio utama (cover-art tidak dihitung video)."""
    try:
        j = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            timeout=60, stderr=subprocess.DEVNULL))
    except Exception:  # noqa: BLE001
        return {"ok": False, "dur": 0.0, "sig": (), "v": None, "a": None}
    streams = j.get("streams", [])
    real_video = [x for x in streams if x.get("codec_type") == "video"
                  and not x.get("disposition", {}).get("attached_pic")]
    audio = [x for x in streams if x.get("codec_type") == "audio"]

    def fps_of(x):
        try:
            n, d = (x.get("avg_frame_rate") or x.get("r_frame_rate") or "0/1").split("/")
            return round(int(n) / int(d), 3) if int(d) else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    v = a = None
    if real_video:
        x = real_video[0]
        v = {"codec": x.get("codec_name"), "w": int(x.get("width") or 0), "h": int(x.get("height") or 0),
             "fps": fps_of(x), "pix": x.get("pix_fmt"), "profile": x.get("profile"),
             "bitrate": int(x.get("bit_rate") or 0)}
    if audio:
        x = audio[0]
        a = {"codec": x.get("codec_name"), "sr": int(x.get("sample_rate") or 0),
             "ch": int(x.get("channels") or 0)}
    try:
        dur = float(j.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    if not v and real_video == [] and not a:
        return {"ok": False, "dur": dur, "sig": (), "v": None, "a": None}
    fbr = int(j.get("format", {}).get("bit_rate") or 0)
    if v and not v["bitrate"]:
        v["bitrate"] = fbr
    sig = tuple((x.get("codec_type"), x.get("codec_name")) for x in streams)
    return {"ok": True, "dur": dur, "sig": sig, "v": v, "a": a, "nstreams": len(streams),
            "attached_pic": len(streams) != len(real_video) + len(audio)}


def keyframe_gap(path, dur: float, window: int = 120) -> float | None:
    """Jarak keyframe terpanjang (detik) pada ±window detik pertama; None jika tak bisa diukur."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
             "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0",
             "-read_intervals", f"%+{window}", str(path)],
            capture_output=True, text=True, timeout=90).stdout
        ts = sorted(float(x) for x in re.split(r"[,\s]+", out) if re.fullmatch(r"-?\d+(\.\d+)?", x))
    except Exception:  # noqa: BLE001
        return None
    if not ts:
        return None
    end = min(dur, window) if dur else ts[-1]
    pts = ts + [end]
    return max(b - a for a, b in zip(pts, pts[1:])) if len(pts) > 1 else None


def auto_vbr(h: int, fps: int) -> int:
    """Tangga bitrate otomatis (kbps) sesuai rekomendasi YouTube Live: stabil, cukup tajam, ringan di upload."""
    base = 1500 if h <= 480 else 3000 if h <= 720 else 5000 if h <= 1080 else 8000
    return int(base * 1.4) if fps > 30 else base


def br_cap(h: int) -> int:
    """Batas bitrate sumber (bps) agar boleh dikirim copy tanpa re-encode; lebih tinggi = rawan putus."""
    return 3_500_000 if h <= 480 else 6_000_000 if h <= 720 else 9_000_000 if h <= 1080 else 13_000_000


def video_copy_ok(paths, infos) -> tuple[bool, str]:
    """Apakah semua video bisa disambung & dikirim apa adanya (tanpa re-encode)?"""
    if not infos or any(not i["ok"] or not i["v"] for i in infos):
        return False, "ada file tanpa video"
    f = infos[0]
    for p, i in zip(paths, infos):
        v = i["v"]
        if v["codec"] != "h264" or v["pix"] != "yuv420p":
            return False, f"{Path(p).name}: bukan H.264 yuv420p"
        if (v["w"], v["h"]) != (f["v"]["w"], f["v"]["h"]) or abs(v["fps"] - f["v"]["fps"]) > 0.02:
            return False, "resolusi/fps antar video berbeda"
        if i["sig"] != f["sig"]:
            return False, "susunan stream antar video berbeda"
        if v["w"] % 2 or v["h"] % 2:
            return False, "resolusi ganjil"
        if v["bitrate"] and v["bitrate"] > br_cap(v["h"]):
            return False, f"{Path(p).name}: bitrate terlalu tinggi untuk live"
        gap = keyframe_gap(p, i["dur"])
        if gap is None or gap > 4.0:
            return False, f"{Path(p).name}: jarak keyframe >4 detik (YouTube butuh ≤4 dtk)"
    return True, ""


def audio_copy_ok(infos) -> bool:
    if not infos or any(not i["ok"] or not i["a"] for i in infos):
        return False
    f = infos[0]["a"]
    return all(i["a"]["codec"] == "aac" and i["a"]["sr"] == f["sr"] and i["a"]["ch"] == f["ch"]
               and not i["attached_pic"] and i["nstreams"] == 1 for i in infos)


def clear_live_cache() -> int:
    n = 0
    for f in CACHE_DIR.glob("*"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def _concat_text(files, cycles: int) -> str:
    lines = []
    for _ in range(cycles):
        for f in files:
            lines.append("file '" + str(f).replace("'", "'\\''") + "'")
    return "\n".join(lines) + "\n"


def _rotate_by_time(files, durs, offset: float):
    total = sum(durs)
    if total <= 0 or len(files) < 2:
        return list(files), list(durs)
    t, acc = offset % total, 0.0
    for i, d in enumerate(durs):
        if t < acc + d:
            return list(files[i:]) + list(files[:i]), list(durs[i:]) + list(durs[:i])
        acc += d
    return list(files), list(durs)


TS_PKT = 188
PCM_RATE, PCM_CH = 44100, 2
PCM_SEC = PCM_RATE * PCM_CH * 2            # byte per detik PCM s16le (176400)


def _align(n: int) -> int:
    """Bulatkan ke bawah ke kelipatan 1 frame PCM s16le stereo (4 byte)."""
    return max(0, int(n)) // 4 * 4


class _Session:
    """Satu sesi YouTube: video + dua PCM audio terpisah -> satu FFmpeg sink."""

    def __init__(self):
        self.stop = threading.Event()
        self.vq: queue.Queue = queue.Queue(maxsize=400)     # ±24 MB potongan TS video (penyangga jaringan)
        self.aq: queue.Queue = queue.Queue(maxsize=30)      # ±15 detik PCM audio RANDOM
        self.lq: queue.Queue = queue.Queue(maxsize=30)      # ±15 detik PCM audio LOOP
        self.procs: list = []
        self.error: str | None = None
        self.audio_done = False
        self.opening_done = threading.Event()
        self.program_done = threading.Event()

    def kill_all(self):
        self.stop.set()
        for p in list(self.procs):
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass


class StreamManager:
    """Live YouTube 24 jam tanpa putus.

    Arsitektur (satu koneksi RTMP yang TIDAK putus saat ganti video/lagu):
      feeder video  -> MPEG-TS (copy / cache / transkode langsung) -> pipe ┐
                                                                            ├-> ffmpeg sink -> RTMP YouTube
      feeder audio  -> PCM mentah (decode lagu satu per satu)      -> pipe ┘
    - Video berikutnya dipilih ACAK setiap pergantian (tanpa pengulangan berurutan), dan sudah disiapkan
      di antrean sebelum video sekarang habis -> tidak ada jeda / macet.
    - Audio berupa PCM tanpa timestamp, di-encode AAC sekali di sink -> sambungan antar lagu mulus,
      dan jika antrean audio kosong otomatis diisi hening (audio tidak pernah 'null').
    - Video yang belum seragam langsung diputar lewat transkode realtime, sambil diseragamkan di
      background (cache) -> live bisa mulai seketika, tidak menunggu semua file selesai dirender.
    """

    def __init__(self):
        self.proc = None
        self.prep_proc = None
        self.stop_event = threading.Event()
        self.started_at = None
        self.restarts = 0
        self.running = False
        self.finished = False
        self.stream_key = ""
        self.video: Path | None = None
        self.files: list[Path] = []
        self.max_seconds = 0
        self.state = "idle"
        self.detail = ""
        self.notes: list[str] = []
        self.progress: dict = {}
        self.now_video = ""
        self.now_audio = ""
        self.now_loop = ""
        self.health = "-"
        self._br_scale = 1.0
        self._slow_since = 0.0
        self._short_runs = 0
        self._adur: dict = {}
        self._vstage = 0
        self.switches = 0
        self.prep_info = ""
        self._out_time = 0.0
        self._prog_change = time.time()
        self._lock = threading.Lock()
        self._bag: dict = {}
        self._last: dict = {}
        self._an: dict = {}
        self._dur: dict = {}
        self._audio_validation_cache: dict[tuple, tuple[bool, str]] = {}
        self._target = (1280, 720, 30)
        self._prep_q: queue.Queue = queue.Queue()
        self._prep_queued: set = set()
        self._prep_stop = threading.Event()
        self._sess: _Session | None = None
        self.playlist_index = 0
        self.playlist_total = 0
        self.playlist_name = ""
        self._playlist_files: list[Path] = []
        self._secret_values: list[str] = []
        # Gain diterapkan per PCM pipe agar bisa diubah langsung saat live berlangsung.
        self.vol = {"l": 0.6, "r": 1.0}

    # ---------- kontrol ----------
    def set_volume(self, layer: str, value: float):
        """Layer 'l' (loop) atau 'r' (random); berlaku tanpa restart live."""
        if layer in self.vol:
            self.vol[layer] = max(0.0, min(2.0, float(value)))

    def start(self, cfg: dict, max_hours: float):
        if self.running:
            return
        self.playlist_index, self.playlist_total, self.playlist_name = 0, 0, ""
        self._playlist_files, self._secret_values = [], [str(cfg.get("stream_key") or "")]
        self.stop_event.clear()
        self.restarts, self.finished, self.notes, self.progress = 0, False, [], {}
        self.switches, self.now_video, self.now_audio, self.prep_info = 0, "", "", ""
        self.now_loop, self._vstage = "", 0
        self.health, self._br_scale, self._slow_since, self._short_runs = "-", 1.0, 0.0, 0
        self._bag, self._last, self._an, self._dur = {}, {}, {}, {}
        self._prep_queued = set()
        self._prep_q = queue.Queue()
        self.started_at = time.time()
        self.stream_key = cfg["stream_key"]
        self.files = [Path(p) for p in (*cfg["videos"], *cfg["audios"])]
        self._adur = {}
        self.video = self.files[0] if self.files else None
        self.max_seconds = int(max_hours * 3600)
        for _k, _ck, _d in (("l", "vol_loop", 0.6), ("r", "vol_rand", 1.0)):
            self.set_volume(_k, cfg.get(_ck, _d))
        self.running, self.state, self.detail = True, "menyiapkan", "Memeriksa file audio dan video..."
        LOG_FILE.write_bytes(b"")
        threading.Thread(target=self._run, args=(cfg,), daemon=True).start()

    def start_playlist(self, configs: list[dict]):
        """Play full-length program configs sequentially, each with its own RTMP destination."""
        if self.running:
            return
        configs = [dict(cfg) for cfg in configs]
        if len(configs) < 2:
            raise ValueError("Antrean multi-stream membutuhkan minimal dua stream.")
        for idx, cfg in enumerate(configs, start=1):
            if cfg.get("program_audio_mode", "full") not in ("full", "shuffle"):
                raise ValueError(
                    f"Stream {idx} harus memutar seluruh audio satu kali; mode durasi target tidak didukung."
                )
        self.stop_event.clear()
        self.restarts, self.finished, self.notes, self.progress = 0, False, [], {}
        self.switches, self.now_video, self.now_audio, self.prep_info = 0, "", "", ""
        self.now_loop, self._vstage = "", 0
        self.health, self._br_scale, self._slow_since, self._short_runs = "-", 1.0, 0.0, 0
        self._bag, self._last, self._an, self._dur = {}, {}, {}, {}
        self._prep_queued = set()
        self._prep_q = queue.Queue()
        self.playlist_index, self.playlist_total, self.playlist_name = 0, len(configs), ""
        self._playlist_files = list(dict.fromkeys(
            Path(p) for cfg in configs for p in (*cfg.get("videos", []), *cfg.get("audios", []))
        ))
        self._secret_values = list(dict.fromkeys(
            str(cfg.get("stream_key") or "") for cfg in configs if cfg.get("stream_key")
        ))
        self.files = list(self._playlist_files)
        self.max_seconds = 0
        self.started_at = time.time()
        self.running, self.state, self.detail = True, "menyiapkan", "Menyiapkan antrean multi-stream..."
        LOG_FILE.write_bytes(b"")
        threading.Thread(target=self._run_playlist, args=(configs,), daemon=True).start()

    def _run_playlist(self, configs: list[dict]):
        completed = 0
        try:
            for idx, cfg in enumerate(configs, start=1):
                if self.stop_event.is_set():
                    break
                self.playlist_index = idx
                self.playlist_name = str(cfg.get("playlist_name") or f"Stream {idx}")
                self.started_at = time.time()
                self.finished = False
                self.restarts = 0
                self.now_video, self.now_audio, self.prep_info = "", "", ""
                self.detail = f"Stream {idx}/{len(configs)}: {self.playlist_name}"
                self._log(f"antrean {idx}/{len(configs)} dimulai: {self.playlist_name}")
                self.running = True
                self._run(cfg)
                if self.finished:
                    completed += 1
                    if idx < len(configs):
                        self._log(f"stream {idx} selesai; berpindah ke stream {idx + 1}.")
                        self.running = True
                        self.state = "beralih"
                        self.detail = f"Stream {idx} selesai; menyiapkan stream {idx + 1}..."
                        continue
                    break
                # Gagal atau dihentikan manual: jangan diam-diam melewati stream.
                break
        except Exception as exc:  # noqa: BLE001
            self.state, self.detail = "error", str(exc)
            self._log(f"ANTREAN GAGAL: {exc}")
        finally:
            self.running = False
            self._kill(self.prep_proc)
            if completed == len(configs):
                self.finished, self.state, self.detail = True, "selesai", "Semua stream terjadwal selesai."
            elif self.stop_event.is_set() and self.state != "error":
                self.state, self.detail = "idle", "Antrean dihentikan."
            self._playlist_files = []

    def stop(self):
        self.stop_event.set()
        if self._sess:
            self._sess.kill_all()
        self._kill(self.prep_proc)
        self._kill(self.proc)

    @staticmethod
    def _kill(p):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()

    def _log(self, msg: str):
        with open(LOG_FILE, "ab") as f:
            f.write((f"[{datetime.now():%H:%M:%S}] {msg}\n").encode())

    # ---------- pemilihan acak ----------
    def _next(self, kind: str, items: list[Path], shuffle: bool, skip=()) -> Path:
        with self._lock:
            bag = self._bag.setdefault(kind, [])
            cand = [p for p in items if p not in skip] or list(items)
            bag[:] = [p for p in bag if p in cand]
            if not bag:
                bag.extend(cand)
                if shuffle:
                    random.shuffle(bag)
                    if len(bag) > 1 and bag[0] == self._last.get(kind):
                        bag.append(bag.pop(0))      # jangan sama dengan yang barusan diputar
            p = bag.pop(0)
            self._last[kind] = p
            return p

    def _pick_video(self, cfg: dict, all_videos: list[Path], bad: set) -> tuple[Path, str]:
        """Putar opening opsional sekali, lalu video acak berulang.

        Tahap opening hanya maju sekali per sesi live, termasuk setelah koneksi stream
        dibangun ulang. Pool opening kosong atau dinonaktifkan berarti langsung ke video acak.
        """
        if self._vstage == 0:
            self._vstage = 1
            pool = [Path(p) for p in (cfg.get("v_open") or []) if Path(p) not in bad]
            if pool:
                p = self._next("stage_opening", pool, True,
                               skip=(self._last.get("v"),))
                self._last["v"] = p
                return p, "opening"

        pool = [Path(p) for p in (cfg.get("v_rand") or []) if Path(p) not in bad]
        pool = pool or [p for p in all_videos if p not in bad] or list(all_videos)
        skip = (*bad, self._last.get("v"))
        return self._next("v", pool, bool(cfg.get("shuffle", True)), skip=skip), "random"

    # ---------- analisis & rencana video ----------
    def _vdur(self, p: Path) -> float:
        if p in self._dur:
            return self._dur[p]
        d = 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                 "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=60).stdout.strip()
            d = float(out.split()[0])
        except Exception:  # noqa: BLE001
            d = 0.0
        if d <= 0:
            d = media_info(p)["dur"]
        self._dur[p] = max(d, 0.5)
        return self._dur[p]

    def _adur_of(self, p: Path) -> float:
        if p not in self._adur:
            self._adur[p] = max(media_info(p)["dur"], 0.5)
        return self._adur[p]

    def _validate_audio_decode(self, path: Path, duration: float) -> tuple[bool, str]:
        """Fully decode a selected audio file before live so corrupt tracks are skipped, not cut mid-play."""
        try:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        except OSError as exc:
            return False, str(exc)
        cached = self._audio_validation_cache.get(key)
        if cached is not None:
            return cached

        cmd = [*NICE, "ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-nostdin",
               "-progress", "pipe:1", "-nostats", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=900, errors="replace")
        except subprocess.TimeoutExpired:
            checked = (False, "decode penuh melewati batas waktu 15 menit")
        except Exception as exc:  # noqa: BLE001
            checked = (False, f"{type(exc).__name__}: {exc}")
        else:
            decoded = None
            for line in result.stdout.splitlines():
                if line.startswith("out_time_us="):
                    try:
                        decoded = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        pass
            if result.returncode != 0:
                checked = (False, (result.stderr or f"FFmpeg keluar dengan kode {result.returncode}")[-300:])
            elif decoded is None or decoded <= 0:
                checked = (False, "FFmpeg tidak menghasilkan audio yang dapat diputar")
            else:
                tolerance = max(0.15, min(1.5, duration * 0.01))
                if duration > 0 and decoded < duration - tolerance:
                    checked = (False, f"audio hanya terdecode {decoded:.2f}s dari durasi {duration:.2f}s")
                else:
                    checked = (True, "")

        if len(self._audio_validation_cache) >= 3000:
            self._audio_validation_cache.clear()
        self._audio_validation_cache[key] = checked
        return checked

    def _spec(self, cfg: dict) -> tuple[int, int, int, int]:
        W, H, fps = self._target
        return W, H, fps, int(cfg.get("vbr_k") or 0) or auto_vbr(H, fps)

    def _cache_for(self, p: Path, cfg: dict) -> Path:
        W, H, fps, vbr = self._spec(cfg)
        return self._ck(p, f"v|{W}|{H}|{fps}|{vbr}", ".mp4")

    @staticmethod
    def _ck(path: Path, params: str, ext: str) -> Path:
        st = path.stat()
        h = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}|{params}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{h}{ext}"

    def _copy_ok(self, p: Path) -> bool:
        ok = self._an.get(p)
        if ok is None:
            info = media_info(p)
            ok = bool(info["ok"] and info["v"])
            if ok:
                ok, _ = video_copy_ok([p], [info])
            if ok:
                v = info["v"]
                W, H, fps = self._target
                ok = (v["w"], v["h"]) == (W, H) and abs(v["fps"] - fps) <= 0.02
            self._an[p] = ok
        return ok

    def _plan(self, p: Path, cfg: dict) -> tuple[str, Path]:
        mode = cfg.get("vmode", "auto")
        cache = self._cache_for(p, cfg)
        if cache.exists():
            return "copy", cache
        if mode == "copy":
            return "copy", p
        if mode != "normalize" and self._copy_ok(p):
            return "copy", p
        self._enqueue_prep(p)
        return "live", p

    def _venc(self, cfg: dict, scale: float = 1.0, preset: str = "superfast") -> list[str]:
        """Encoder seragam. Bitrate nyaris konstan (maxrate = bitrate, buffer kecil) -> aliran data ke
        YouTube rata. `scale` < 1 dipakai penurunan otomatis saat jaringan lambat."""
        W, H, fps, vbr = self._spec(cfg)
        vbr = max(500, int(vbr * scale))
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p")
        g = str(fps * 2)
        return ["-vf", vf, "-c:v", "libx264", "-preset", preset, "-profile:v", "high",
                "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{int(vbr * 1.5)}k",
                "-g", g, "-keyint_min", g, "-sc_threshold", "0", "-bf", "0"]

    # ---------- penyeragaman di background ----------
    def _enqueue_prep(self, p: Path):
        with self._lock:
            if p in self._prep_queued:
                return
            self._prep_queued.add(p)
        self._prep_q.put(p)

    def _prep_worker(self, cfg: dict):
        free_min = float(cfg.get("disk_min_gb", 0.5)) * 1024**3
        prep_stop = self._prep_stop
        while not self.stop_event.is_set() and not prep_stop.is_set() and self.running:
            try:
                p = self._prep_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                dst = self._cache_for(p, cfg)
                if dst.exists():
                    continue
                if shutil.disk_usage(BASE).free < free_min:
                    self.prep_info = "Cache dilewati: disk hampir penuh."
                    continue
                part = Path(str(dst) + ".part")
                part.unlink(missing_ok=True)
                self.prep_info = f"Menyeragamkan di background: {p.name}"
                dur = media_info(p)["dur"]
                cmd = ["nice", "-n", "15"] if shutil.which("nice") else []
                cmd += ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-progress", "pipe:1", "-nostats", "-i", str(p), "-map", "0:v:0", "-an",
                        *self._venc(cfg, preset="veryfast"), "-movflags", "+faststart", "-f", "mp4", str(part)]
                with open(LIVE_DIR / "prep_err.log", "wb") as ef:
                    self.prep_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, text=True,
                                                      stdin=subprocess.DEVNULL)
                    for line in self.prep_proc.stdout:
                        if line.startswith("out_time_us=") and dur > 0:
                            try:
                                t = int(line.split("=")[1]) / 1e6
                                self.prep_info = f"Menyeragamkan {p.name}: {min(99, t / dur * 100):.0f}%"
                            except ValueError:
                                pass
                    self.prep_proc.wait()
                if self.prep_proc.returncode == 0 and part.exists() and part.stat().st_size > 0:
                    part.rename(dst)
                    self._log(f"cache siap: {p.name}")
                else:
                    part.unlink(missing_ok=True)
                    self._log(f"cache gagal: {p.name}")
            except Exception as e:  # noqa: BLE001
                self._log(f"prep error: {e}")
            finally:
                self.prep_info = ""
        self.prep_info = ""

    # ---------- feeder ----------
    def _put(self, sess: _Session, q: queue.Queue, item) -> bool:
        while not sess.stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _video_feeder(self, sess: _Session, cfg: dict, videos: list[Path]):
        T, fails, bad = 2.0, 0, set()
        errlog = LIVE_DIR / "feed_v.log"
        program_mode = bool(cfg.get("program_mode"))
        ending_started = False
        try:
            while not sess.stop.is_set():
                if program_mode and sess.audio_done:
                    endings = [Path(p) for p in (cfg.get("v_end") or []) if Path(p) not in bad]
                    if not endings:
                        raise RuntimeError("Video end tidak tersedia atau gagal dibaca.")
                    if ending_started:
                        return
                    p = self._next("v_end", endings, True)
                    stage_name = "end"
                    ending_started = True
                else:
                    p, stage_name = self._pick_video(cfg, videos, bad)
                try:
                    mode, src = self._plan(p, cfg)
                    dur = self._vdur(src if mode == "copy" else p)
                except Exception as e:  # noqa: BLE001
                    bad.add(p)
                    self._log(f"video dilewati ({p.name}): {e}")
                    if program_mode and stage_name == "opening":
                        sess.opening_done.set()
                    fails += 1
                    if fails > 5:
                        raise RuntimeError("Video terus gagal dibaca.")
                    continue
                if not self._put(sess, sess.vq, ("mark", p.name, f"{stage_name} · {'copy' if mode == 'copy' else 'transkode langsung'}")):
                    return
                cmd = [*(NICE if mode == "live" else []), "ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-fflags", "+genpts", "-i", str(src), "-map", "0:v:0", "-an"]
                cmd += (["-c:v", "copy"] if mode == "copy"
                        else self._venc(cfg, self._br_scale))
                cmd += ["-output_ts_offset", f"{T:.3f}", "-muxdelay", "0", "-muxpreload", "0",
                        "-f", "mpegts", "pipe:1"]
                got = 0
                cut_for_end = False
                with open(errlog, "wb") as ef:
                    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
                    sess.procs.append(pr)
                    while not sess.stop.is_set():
                        if program_mode and sess.audio_done and stage_name not in ("opening", "end"):
                            cut_for_end = True
                            self._kill(pr)
                            break
                        buf = pr.stdout.read(TS_PKT * 512)
                        if not buf:
                            break
                        got += len(buf)
                        if not self._put(sess, sess.vq, buf):
                            break
                    pr.stdout.close()
                    pr.wait()
                if sess.procs and pr in sess.procs:
                    sess.procs.remove(pr)
                if sess.stop.is_set():
                    return
                if cut_for_end:
                    self._log(f"video utama dihentikan karena audio selesai: {p.name}")
                    continue
                if pr.returncode != 0 or got == 0:
                    bad.add(p)
                    fails += 1
                    tail = errlog.read_text(errors="replace")[-200:]
                    self._log(f"video gagal ({p.name}): {tail}")
                    if program_mode and stage_name == "opening":
                        sess.opening_done.set()
                    if fails > 5 or len(bad) >= len(videos):
                        raise RuntimeError(f"Video gagal diputar: {p.name}: {tail}")
                    continue
                fails = 0
                if program_mode:
                    marker = "program_done" if stage_name == "end" else "stage_end"
                    if not self._put(sess, sess.vq, (marker, stage_name)):
                        return
                T += dur
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder video: {e}"
            sess.kill_all()

    def _loop_audio_feeder(self, sess: _Session, audio: Path, q: queue.Queue):
        """Decode one loop file continuously; FFmpeg performs the file loop without transition gaps."""
        errlog = LIVE_DIR / "feed_a_loop.log"
        step, got, marked = PCM_SEC // 2, 0, False
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-re", "-stream_loop", "-1",
               "-i", str(audio), "-map", "0:a:0", "-vn", "-f", "s16le", "-ar", str(PCM_RATE),
               "-ac", str(PCM_CH), "pipe:1"]
        try:
            with open(errlog, "wb") as ef:
                pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
                sess.procs.append(pr)
                while not sess.stop.is_set():
                    buf = pr.stdout.read(step)
                    if not buf:
                        break
                    got += len(buf)
                    if not marked:
                        if not self._put(sess, q, ("mark", audio.name, "")):
                            break
                        marked = True
                    if not self._put(sess, q, buf):
                        break
                pr.stdout.close()
                pr.wait()
            if pr in sess.procs:
                sess.procs.remove(pr)
            if sess.stop.is_set():
                return
            if got == 0 or pr.returncode != 0:
                detail = errlog.read_text(errors="replace")[-200:]
                raise RuntimeError(f"Audio LOOP gagal diputar: {audio.name}. {detail}")
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder audio loop: {e}"
            sess.kill_all()

    def _random_audio_feeder(self, sess: _Session, audios: list[Path], q: queue.Queue):
        """Play complete audio files in random order, with no crossfade or segment trimming."""
        fails, bad = 0, set()
        errlog = LIVE_DIR / "feed_a_random.log"
        step = PCM_SEC // 2
        try:
            while not sess.stop.is_set():
                audio = self._next("a_r", audios, True, skip=bad)
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-i", str(audio),
                       "-map", "0:a:0", "-vn", "-f", "s16le", "-ar", str(PCM_RATE), "-ac", str(PCM_CH),
                       "pipe:1"]
                got, marked = 0, False
                with open(errlog, "wb") as ef:
                    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
                    sess.procs.append(pr)
                    while not sess.stop.is_set():
                        buf = pr.stdout.read(step)
                        if not buf:
                            break
                        got += len(buf)
                        if not marked:
                            if not self._put(sess, q, ("mark", audio.name, "")):
                                break
                            marked = True
                        if not self._put(sess, q, buf):
                            break
                    pr.stdout.close()
                    pr.wait()
                if pr in sess.procs:
                    sess.procs.remove(pr)
                if sess.stop.is_set():
                    return
                if got == 0 or pr.returncode != 0:
                    bad.add(audio)
                    fails += 1
                    detail = errlog.read_text(errors="replace")[-200:]
                    self._log(f"audio random gagal ({audio.name}): {detail}")
                    if fails > 5 or len(bad) >= len(audios):
                        raise RuntimeError(f"Audio RANDOM tidak bisa didecode: {audio.name}")
                    continue
                fails = 0
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder audio random: {e}"
            sess.kill_all()

    def _program_audio_feeder(self, sess: _Session, audios: list[Path], cfg: dict, q: queue.Queue):
        """Play a selected album once or randomly until its target, then signal audio completion."""
        errlog = LIVE_DIR / "feed_a_program.log"
        mode = cfg.get("program_audio_mode", "full")
        remaining = max(0.0, float(cfg.get("program_audio_seconds") or 0.0))
        bad: set[Path] = set()
        fails = 0
        step = PCM_SEC // 2
        try:
            while not sess.stop.is_set() and not sess.opening_done.wait(0.4):
                pass
            if sess.stop.is_set():
                return

            playlist = list(audios)
            if mode in ("target", "shuffle"):
                random.shuffle(playlist)
            while playlist and not sess.stop.is_set():
                audio = playlist.pop(0)
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
                       "-i", str(audio)]
                if mode == "target":
                    if remaining <= 0.02:
                        break
                    cmd += ["-t", f"{remaining:.3f}"]
                cmd += ["-map", "0:a:0", "-vn", "-f", "s16le",
                        "-ar", str(PCM_RATE), "-ac", str(PCM_CH), "pipe:1"]
                got, marked = 0, False
                with open(errlog, "wb") as ef:
                    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
                    sess.procs.append(pr)
                    while not sess.stop.is_set():
                        buf = pr.stdout.read(step)
                        if not buf:
                            break
                        got += len(buf)
                        if not marked:
                            if not self._put(sess, q, ("mark", audio.name, "program")):
                                break
                            marked = True
                        if not self._put(sess, q, buf):
                            break
                    pr.stdout.close()
                    pr.wait()
                if pr in sess.procs:
                    sess.procs.remove(pr)
                if sess.stop.is_set():
                    return
                if got == 0 or pr.returncode != 0:
                    bad.add(audio)
                    fails += 1
                    detail = errlog.read_text(errors="replace")[-200:]
                    self._log(f"audio program gagal ({audio.name}): {detail}")
                    if fails > 5 or len(bad) >= len(audios):
                        raise RuntimeError(f"Audio program tidak bisa diputar: {audio.name}")
                    playlist = [p for p in audios if p not in bad]
                    if mode in ("target", "shuffle"):
                        random.shuffle(playlist)
                    continue

                fails = 0
                if mode == "target":
                    remaining -= got / PCM_SEC
                    if remaining > 0.02:
                        playlist = [p for p in audios if p not in bad]
                        random.shuffle(playlist)

            if not sess.stop.is_set():
                if not self._put(sess, q, ("audio_complete",)):
                    return
                self._log("audio program selesai; menunggu video end.")
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder audio program: {e}"
            sess.kill_all()

    @staticmethod
    def _gain(buf: bytes, prev: float, target: float) -> bytes:
        """Terapkan gain pada PCM s16le stereo, naik/turun halus dari `prev` ke `target` dalam satu potongan
        (tanpa bunyi 'klik'), dengan soft-clip lembut agar volume besar tidak pecah."""
        n = _align(len(buf))
        if np is None or n == 0 or (prev == 1.0 and target == 1.0):
            return buf
        x = np.frombuffer(buf, dtype="<i2", count=n // 2).astype(np.float32) / 32768.0
        ramp = np.repeat(np.linspace(prev, target, n // 4, dtype=np.float32), 2)
        y = x * ramp
        a = np.abs(y)
        knee = 0.85
        y = np.where(a > knee, np.sign(y) * (knee + (1 - knee) * np.tanh((a - knee) / (1 - knee))), y)
        return (y * 32767.0).astype("<i2").tobytes() + bytes(buf[n:])

    def _writer(self, sess: _Session, fobj, q: queue.Queue, silence: bytes | None, kind: str):
        """Write one live PCM/video pipe; an inactive audio source feeds silence to keep mux input valid."""
        g_prev = self.vol.get(kind, 1.0)
        try:
            while not sess.stop.is_set():
                try:
                    buf = q.get(timeout=0.4)
                except queue.Empty:
                    if silence is not None:
                        fobj.write(silence)
                        fobj.flush()
                    continue
                if buf is None:
                    break
                if isinstance(buf, tuple):          # penanda: file ini mulai benar-benar dikirim
                    if kind == "v":
                        if buf[0] == "stage_end":
                            if buf[1] == "opening":
                                sess.opening_done.set()
                                self._log("opening selesai; audio program dimulai.")
                        elif buf[0] == "program_done":
                            sess.program_done.set()
                            self._log("video end selesai; streaming dihentikan.")
                        else:
                            self.now_video, self.switches = buf[1], self.switches + 1
                            self._log(f"video -> {buf[1]} ({buf[2]})")
                    elif kind == "l":
                        self.now_loop = buf[1]
                        self._log(f"audio loop -> {buf[1]}")
                    elif buf[0] == "audio_complete":
                        sess.audio_done = True
                        self.now_audio = "Audio selesai"
                        self._log("audio program selesai dikirim.")
                    else:
                        self.now_audio = buf[1]
                        if buf[1]:
                            self._log(f"audio random -> {buf[1]}")
                    continue
                if kind in self.vol:
                    g_now = self.vol[kind]
                    buf = self._gain(buf, g_prev, g_now)
                    g_prev = g_now
                fobj.write(buf)
                fobj.flush()
        except (BrokenPipeError, ValueError, OSError):
            sess.stop.set()
        finally:
            try:
                fobj.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- sink ----------
    def build_cmd(self, cfg: dict, vfd: int, lfd: int, rfd: int) -> list[str]:
        """Mux video with LOOP + RANDOM PCM into one limited AAC audio stream."""
        out = cfg.get("_output") or (cfg["rtmp_url"].rstrip("/") + "/" + cfg["stream_key"])

        def vol(key, default):
            return max(0.0, min(2.0, float(cfg.get(key, default))))
        vl, vr = vol("vol_loop", 0.6), vol("vol_rand", 1.0)
        if np is not None:
            vl = vr = 1.0       # slider gain is applied in _writer so it can change while live
        pcm = ["-thread_queue_size", "512", "-re", "-f", "s16le",
               "-ar", str(PCM_RATE), "-ac", str(PCM_CH)]
        fc = (f"[1:a]aformat=sample_fmts=fltp:sample_rates={PCM_RATE}:channel_layouts=stereo,"
              f"volume={vl:.2f}[loop];"
              f"[2:a]aformat=sample_fmts=fltp:sample_rates={PCM_RATE}:channel_layouts=stereo,"
              f"volume={vr:.2f}[random];"
              "[loop][random]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
              "alimiter=limit=0.95:level=0,aresample=async=1000:first_pts=0[aout]")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-nostdin", "-nostats",
               "-progress", "pipe:1",
               "-probesize", "2M", "-analyzeduration", "2M", "-thread_queue_size", "1024",
               "-fflags", "+genpts", "-re", "-f", "mpegts", "-i", f"pipe:{vfd}",
               *pcm, "-i", f"pipe:{lfd}",
               *pcm, "-i", f"pipe:{rfd}"]
        cmd += ["-filter_complex", fc,
                "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", str(PCM_RATE), "-ac", "2",
                "-max_muxing_queue_size", "4096", "-flush_packets", "1",
                "-flvflags", "no_duration_filesize"]
        if out.lower().startswith(("rtmp://", "rtmps://")):
            # koneksi mati diam-diam terdeteksi ±20 dtk -> sesi cepat dibangun ulang
            cmd += ["-rtmp_live", "live", "-rw_timeout", "20000000"]
        cmd += ["-f", "flv", out]
        return cmd

    def _downshift(self, why: str):
        """Turunkan bitrate untuk video yang di-transkode (copy tidak bisa diubah) saat jaringan bermasalah."""
        if self._br_scale > 0.55:
            self._br_scale = round(max(0.55, self._br_scale - 0.15), 2)
            self._log(f"AUTO BITRATE: {why} -> bitrate transkode {int(self._br_scale * 100)}% dari target.")

    def _read_progress(self, proc):
        cur: dict = {}
        smp = None                      # (waktu dinding, out_time) awal jendela ukur 10 detik
        slow = 0
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            cur[k] = v
            if k == "out_time_us":
                try:
                    t = int(v) / 1e6
                except ValueError:
                    continue
                if t != self._out_time:
                    self._out_time, self._prog_change = t, time.time()
            elif k == "progress":
                now, t = time.time(), self._out_time
                if smp is None:
                    smp = (now, t)
                elif now - smp[0] >= 10 and t > 0:
                    rate = (t - smp[1]) / (now - smp[0])      # 1.00 = tepat waktu nyata
                    smp = (now, t)
                    if rate >= 0.97:
                        self.health, slow = "stabil", 0
                    elif rate >= 0.90:
                        self.health, slow = "agak lambat", 0
                    else:
                        self.health, slow = "lambat", slow + 1
                        if slow >= 4:                          # ±40 detik berturut-turut
                            self._downshift("jaringan upload lambat")
                            slow = 0
                self.progress = {"speed": cur.get("speed", ""), "bitrate": cur.get("bitrate", ""),
                                 "size": cur.get("total_size", ""), "out_time": self._out_time}
                cur = {}

    def _session(self, cfg: dict, videos, audios, deadline) -> str:
        sess = _Session()
        if cfg.get("program_mode"):
            # Keep the scheduled transition responsive; legacy mode retains its larger network buffer.
            sess.vq = queue.Queue(maxsize=4)
        self._sess = sess
        vr, vw = os.pipe()
        lr, lw = os.pipe()
        rr, rw = os.pipe()
        self.state, self.detail = "menghubungkan", "Memulai ffmpeg..."
        self.health = "-"
        self._out_time, self._prog_change = 0.0, time.time()
        t_launch = time.time()
        cmd = self.build_cmd(cfg, vr, lr, rr)
        fds = (vr, lr, rr)
        with open(LOG_FILE, "ab") as log:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True,
                                         stdin=subprocess.DEVNULL, pass_fds=fds)
        for x in fds:
            os.close(x)
        sess.procs.append(self.proc)
        threading.Thread(target=self._read_progress, args=(self.proc,), daemon=True).start()
        silence = bytes(PCM_SEC // 2)
        loops = [Path(p) for p in (cfg.get("a_loop") or [])][:1]
        rands = [Path(p) for p in (cfg.get("a_rand") or [])]
        work = [(self._video_feeder, (sess, cfg, videos)),
                (self._writer, (sess, os.fdopen(vw, "wb", buffering=0), sess.vq, None, "v")),
                (self._writer, (sess, os.fdopen(lw, "wb", buffering=0), sess.lq, silence, "l")),
                (self._writer, (sess, os.fdopen(rw, "wb", buffering=0), sess.aq, silence, "r"))]
        if loops:
            work.append((self._loop_audio_feeder, (sess, loops[0], sess.lq)))
        if cfg.get("program_mode") and (not cfg.get("v_open") or self._vstage > 0):
            sess.opening_done.set()
        if cfg.get("program_mode"):
            work.append((self._program_audio_feeder, (sess, rands, cfg, sess.aq)))
        elif rands:
            work.append((self._random_audio_feeder, (sess, rands, sess.aq)))
        for target, args in work:
            threading.Thread(target=target, args=args, daemon=True).start()

        reason = "died"
        while True:
            if self.stop_event.is_set():
                reason = "stop"
                break
            if deadline and time.time() >= deadline:
                reason = "deadline"
                break
            if sess.error:
                reason = "error"
                break
            if sess.program_done.is_set():
                reason = "finished"
                break
            if self.proc.poll() is not None:
                reason = "died"
                break
            now = time.time()
            if self._out_time > 0:
                self.state, self.detail = "live", ""
            if now - t_launch > 60 and now - self._prog_change > 25:
                self._log("Stream macet >25 detik, sesi di-restart.")
                reason = "stalled"
                break
            time.sleep(1)
        rc = self.proc.returncode
        sess.kill_all()
        self._kill(self.proc)
        self._sess = None
        if reason == "error":
            self._log(f"SESI ERROR: {sess.error}")
            self.detail = sess.error or ""
        return reason

    # ---------- loop utama ----------
    def _run(self, cfg: dict):
        # Each scheduled item is an independent program; do not carry its opening,
        # shuffle bags, media probes, or background preparation queue into the next one.
        self.set_volume("l", cfg.get("vol_loop", 0.6))
        self.set_volume("r", cfg.get("vol_rand", 1.0))
        self._vstage = 0
        self._bag, self._last, self._an, self._dur = {}, {}, {}, {}
        self._prep_queued = set()
        self._prep_q = queue.Queue()
        self._prep_stop = threading.Event()
        videos = [Path(p) for p in cfg["videos"]]
        requested_loop = [Path(p) for p in (cfg.get("a_loop") or [])][:1]
        requested_random = [Path(p) for p in (cfg.get("a_rand") or [])]
        requested_audio = list(dict.fromkeys([*requested_loop, *requested_random]))
        audio_skipped: list[tuple[Path, str]] = []
        try:
            if not videos:
                raise RuntimeError("Pilih minimal satu video untuk live.")
            for idx, p in enumerate(videos, start=1):
                self.detail = f"Menganalisis video ({idx}/{len(videos)}): {p.name}"
                i = media_info(p)
                if not i["ok"] or not i["v"]:
                    raise RuntimeError(f"File video tidak valid / tidak terbaca: {p.name}")

            valid_audio: set[Path] = set()
            for idx, p in enumerate(requested_audio, start=1):
                self.detail = f"Menguji audio sampai selesai ({idx}/{len(requested_audio)}): {p.name}"
                info = media_info(p)
                if not info["ok"] or not info["a"]:
                    audio_skipped.append((p, "stream audio tidak terbaca"))
                    continue
                valid, detail = self._validate_audio_decode(p, info["dur"])
                if valid:
                    valid_audio.add(p)
                else:
                    audio_skipped.append((p, detail))

            loop_audio = [p for p in requested_loop if p in valid_audio]
            random_audio = [p for p in requested_random if p in valid_audio]
            audios = list(dict.fromkeys([*loop_audio, *random_audio]))
            cfg["a_loop"], cfg["a_rand"], cfg["audios"] = loop_audio, random_audio, audios
            self.files = list(dict.fromkeys(
                [*self._playlist_files, *videos, *audios] if self.playlist_total else [*videos, *audios]
            ))

            for path, detail in audio_skipped:
                self._log(f"audio dilewati saat pemeriksaan awal ({path.name}): {detail}")
            if not audios:
                if audio_skipped:
                    errors = "; ".join(f"{p.name}: {why}" for p, why in audio_skipped[:4])
                    raise RuntimeError(f"Tidak ada audio yang lolos pemeriksaan decode. {errors}")
                raise RuntimeError("Pilih minimal satu file audio yang valid untuk diputar.")

            self.detail = "Menyiapkan jalur stream video dan audio..."
            W, H = RES.get(int(cfg.get("height", 720)), RES[720])
            fps = int(cfg.get("fps", 30))
            vmode = cfg.get("vmode", "auto")
            # target seragam = format video pertama jika sudah layak live, selain itu pengaturan di UI
            if vmode != "normalize":
                i0 = media_info(videos[0])
                ok0, _ = video_copy_ok([videos[0]], [i0])
                if ok0 or vmode == "copy":
                    self._target = (i0["v"]["w"], i0["v"]["h"], int(round(i0["v"]["fps"])) or fps)
                else:
                    self._target = (W, H, fps)
            else:
                self._target = (W, H, fps)
            self.notes = [
                *(f"Audio dilewati agar tidak terputus saat live: {p.name} ({why[:120]})"
                  for p, why in audio_skipped[:10]),
                "Urutan video: opening opsional sekali → video acak diputar penuh dan berulang; audio berjalan independen.",
                f"Target video seragam {self._target[0]}x{self._target[1]} {self._target[2]}fps.",
                ("Audio RANDOM diputar penuh dan berulang dalam urutan acak tanpa pengulangan berurutan; "
                 "audio dibatasi limiter lalu di-encode sekali menjadi AAC."
                 if random_audio and not loop_audio else
                 "Audio LOOP dan RANDOM opsional dicampur bersamaan, dibatasi limiter, "
                 "lalu di-encode sekali menjadi AAC."),
                f"Bitrate transkode: {self._spec(cfg)[3]} kbps"
                + (" (auto)" if not int(cfg.get("vbr_k") or 0) else "")
                + "; video yang sudah cocok dikirim copy tanpa re-encode.",
                "File video yang belum seragam diputar via transkode langsung sambil diseragamkan di background.",
                "Transisi antar audio tidak digunakan; setiap sumber berjalan sesuai file aslinya."]
            if vmode != "copy":
                for p in videos:
                    if not self._cache_for(p, cfg).exists() and not self._copy_ok(p):
                        self._enqueue_prep(p)
            threading.Thread(target=self._prep_worker, args=(cfg,), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            self.running, self.state, self.detail = False, "error", f"{e}"
            self._log(f"PERSIAPAN GAGAL: {e}")
            self._prep_stop.set()
            return

        deadline = self.started_at + self.max_seconds if self.max_seconds else None
        backoff = 2
        while not self.stop_event.is_set():
            if deadline and time.time() >= deadline:
                break
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                LOG_FILE.write_bytes(b"")
            t0 = time.time()
            self._log("sesi dimulai" + (f" (restart #{self.restarts})" if self.restarts else ""))
            reason = self._session(cfg, videos, audios, deadline)
            if reason in ("stop", "deadline"):
                break
            if reason == "finished":
                self.finished = True
                break
            self.restarts += 1
            ran = time.time() - t0
            wait_s = backoff
            backoff = 2 if ran > 120 else min(backoff * 2, 30)
            self._short_runs = self._short_runs + 1 if ran < 60 else 0
            if self._short_runs >= 3:
                self._downshift("sesi sering putus")
                self._short_runs = 0
            self.state = "menyambung ulang"
            self.detail = f"{reason}; coba lagi {wait_s}s" + (f" ({self.detail})" if reason == "error" and self.detail else "")
            self.stop_event.wait(wait_s)
        self.running = False
        self.state = "selesai" if self.finished else "idle"
        self.detail = ""
        self._prep_stop.set()
        self._kill(self.prep_proc)

    # ---------- status ----------
    def uptime(self) -> str:
        if not self.running or not self.started_at:
            return "-"
        s = int(time.time() - self.started_at)
        return f"{s // 3600}j {(s % 3600) // 60}m {s % 60}d"

    def log_tail(self, n: int = 6000) -> str:
        if not LOG_FILE.exists():
            return ""
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            text = f.read().decode("utf-8", errors="replace")
        for secret in set([self.stream_key, *self._secret_values]):
            if secret:
                text = text.replace(secret, "********")
        return text

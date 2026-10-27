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

    def attempt(embed: bool, thumb: bool, client: str | None = None) -> None:
        """Sama seperti notebook Colab: audio saja; cover/thumbnail opsional dan boleh gagal."""
        o = dict(opts)
        o["js_runtimes"] = {"deno": {}}                 # penyelesai tantangan JS YouTube (wajib agar tidak 403)
        o["remote_components"] = ["ejs:github"]
        if client:
            o["extractor_args"] = {"youtube": {"player_client": [client]}}
        if hint == "audio":
            codec = ao["codec"]
            o["format"] = "ba[ext=m4a]/ba/b" if codec == "m4a" else "ba/b"   # hanya stream audio
            pps = [{"key": "FFmpegExtractAudio", "preferredcodec": codec,
                    "preferredquality": str(ao["bitrate"])}]
            if embed or thumb:
                o["writethumbnail"] = True
            if embed:
                pps += [{"key": "FFmpegMetadata", "add_metadata": True},
                        {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                        {"key": "EmbedThumbnail", "already_have_thumbnail": bool(thumb)}]
            elif thumb:
                pps += [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
            o["postprocessors"] = pps
        else:
            h = height
            o["format"] = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
                           f"bv*[height<={h}]+ba/b[height<={h}]/b")
            o["merge_output_format"] = "mp4"
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.extract_info(url, download=True)

    def clean_stage():
        for f in stage.glob("*"):
            if f != cookie_file:
                f.unlink(missing_ok=True)

    def run_attempts(client: str | None) -> None:
        """Lengkap -> tanpa embed -> tanpa thumbnail. Hanya diulang jika gagal di tahap thumbnail/pemrosesan."""
        plan = [(bool(ao["embed"]), bool(ao["thumb"])), (False, bool(ao["thumb"])), (False, False)]
        tried, last = [], None
        for embed, thumb in plan:
            if hint != "audio" and tried:
                break
            if (embed, thumb) in tried:
                continue
            tried.append((embed, thumb))
            try:
                if len(tried) > 1:
                    job["detail"] = "Cover/thumbnail gagal, mengulang dengan opsi lebih sederhana..."
                    clean_stage()
                attempt(embed, thumb, client)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                low = str(e).lower()
                if "postprocessing" not in low and "thumbnail" not in low:
                    raise
        raise last

    try:
        last = None
        # 403 Forbidden biasanya khusus satu "client" YouTube; coba beberapa jalur berurutan.
        for n, client in enumerate((None, "tv", "mweb", "web_safari", "android_vr")):
            try:
                if n:
                    job["detail"] = f"Diblokir (403), mencoba jalur lain {n}/4..."
                    clean_stage()
                run_attempts(client)
                return True, ""
            except Exception as e:  # noqa: BLE001
                last = e
                low = str(e).lower()
                if "not a bot" in low or "confirm you" in low:
                    break                      # diblokir per-IP: ganti jalur tidak membantu, hanya cookies
                if "403" not in low and "forbidden" not in low:
                    break
        raise last
    except Exception as e:  # noqa: BLE001
        clean = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        low = clean.lower()
        hint403 = (" | YouTube memblokir IP server ini (dianggap bot). Tempel cookies YouTube di tab Impor "
                   "(sama seperti kolom Cookies JSON di notebook), atau unduh audio di komputermu lalu upload."
                   ) if ("403" in low or "forbidden" in low or "not a bot" in low) else ""
        return False, "yt-dlp: " + clean[:300] + hint403
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
             "bitrate": int(x.get("bit_rate") or 0),
             "bframes": int(x.get("has_b_frames") or 0),
             "vfr": (x.get("r_frame_rate") or "") != (x.get("avg_frame_rate") or ""),
             "nb_frames": int(x.get("nb_frames") or 0) if str(x.get("nb_frames") or "").isdigit() else 0}
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
        ts = sorted(float(x) for x in out.split() if re.fullmatch(r"-?\d+(\.\d+)?", x))
    except Exception:  # noqa: BLE001
        return None
    if not ts:
        return None
    end = min(dur, window) if dur else ts[-1]
    pts = ts + [end]
    return max(b - a for a, b in zip(pts, pts[1:])) if len(pts) > 1 else None


def video_copy_ok(paths, infos, relaxed: bool = False) -> tuple[bool, str]:
    """Apakah semua video bisa disambung & dikirim apa adanya (tanpa re-encode)?"""
    if not infos or any(not i["ok"] or not i["v"] for i in infos):
        return False, "ada file tanpa video"
    f = infos[0]
    for p, i in zip(paths, infos):
        v = i["v"]
        if v["codec"] != "h264" or v["pix"] != "yuv420p":
            return False, f"{Path(p).name}: bukan H.264 yuv420p"
        if v.get("bframes", 0) > 0:
            return False, f"{Path(p).name}: memakai B-frame (disambung mulus hanya setelah diseragamkan)"
        if v.get("vfr"):
            return False, f"{Path(p).name}: frame rate tidak konstan (VFR)"
        if (v["w"], v["h"]) != (f["v"]["w"], f["v"]["h"]) or abs(v["fps"] - f["v"]["fps"]) > 0.02:
            return False, "resolusi/fps antar video berbeda"
        if i["sig"] != f["sig"]:
            return False, "susunan stream antar video berbeda"
        if v["w"] % 2 or v["h"] % 2:
            return False, "resolusi ganjil"
        if relaxed:
            continue
        if v["bitrate"] and v["bitrate"] > 8_000_000:
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


PCM_RATE, PCM_CH = 44100, 2
PCM_FRAME = PCM_CH * 2                     # byte per sample-frame (s16le stereo)
PCM_SEC = PCM_RATE * PCM_FRAME             # byte per detik PCM (176400)
VCHUNK = 64 * 1024                         # ukuran potongan video mentah di antrean


class _Session:
    """Satu sesi koneksi ke YouTube: feeder video/audio -> 2 pipe -> 1 ffmpeg sink (copy video, AAC audio)."""

    def __init__(self):
        self.stop = threading.Event()
        self.live = threading.Event()                       # set setelah sink tersambung (baru boleh isi hening)
        self.vq: queue.Queue = queue.Queue(maxsize=256)     # ±16 MB H.264 mentah
        self.aq: queue.Queue = queue.Queue(maxsize=90)      # ±45 detik PCM
        self.procs: list = []
        self.error: str | None = None
        self.audio_done = False
        self.underruns = 0
        self.v_secs = 0.0                                   # detik video yang sudah selesai masuk antrean

    def kill_all(self):
        self.stop.set()
        for p in list(self.procs):
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass


class StreamManager:
    """Live YouTube 24 jam tanpa putus dan tanpa frame rusak ("null").

    Arsitektur (satu koneksi RTMP yang TIDAK putus saat ganti video/lagu):
      feeder video -> H.264 mentah (Annex-B, tanpa MPEG-TS) -> pipe ┐
                                                                     ├-> ffmpeg sink -> RTMP YouTube
      feeder audio -> PCM mentah                            -> pipe ┘

    Kenapa begini:
    - MPEG-TS yang disambung antar file menghasilkan paket korup + DTS mundur di titik pergantian
      (continuity counter & PES terputus) -> frame rusak -> YouTube menampilkan blank/"null".
      H.264 mentah hanya urutan frame; timestamp dibuat sink dari frame-rate -> selalu kontinu.
    - Hanya video yang SIAP (H.264 tanpa B-frame, CFR, seragam) yang disiarkan. Video lain diseragamkan
      dulu di background (cache). Tidak ada transkode realtime di jalur live -> tidak ada macet CPU.
    - Koneksi ke YouTube baru dibuka setelah buffer video & audio terisi (tidak ada awal kosong).
    - Audio selalu ada: PCM -> AAC di satu aliran; telat -> hening disisipkan (tercatat di log).
    - Sumber audio dipilih: 'file' (video dibisukan, audio dari playlist) atau 'video' (audio asli video;
      video tanpa audio otomatis memakai playlist audio / hening). Mode 'video' menyelaraskan audio ke
      jumlah frame video secara eksak, jadi lipsync terjaga.
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
        self.switches = 0
        self.prep_info = ""
        self.buf_v = 0.0
        self.buf_a = 0.0
        self.underruns = 0
        self._out_time = 0.0
        self._prog_change = time.time()
        self._lock = threading.Lock()
        self._bag: dict = {}
        self._last: dict = {}
        self._an: dict = {}
        self._frames: dict = {}
        self._durs: dict = {}
        self._target = (1280, 720, 30)
        self._prep_q: queue.Queue = queue.Queue()
        self._prep_queued: set = set()
        self._prep_failed: set = set()
        self._pl: dict = {}
        self._sess: _Session | None = None

    # ---------- kontrol ----------
    def start(self, cfg: dict, max_hours: float):
        if self.running:
            return
        self.stop_event.clear()
        self.restarts, self.finished, self.notes, self.progress = 0, False, [], {}
        self.switches, self.now_video, self.now_audio, self.prep_info = 0, "", "", ""
        self.underruns, self.buf_v, self.buf_a = 0, 0.0, 0.0
        self._bag, self._last, self._an, self._frames = {}, {}, {}, {}
        self._prep_queued, self._prep_failed = set(), set()
        self._prep_q = queue.Queue()
        self.started_at = time.time()
        self.stream_key = cfg["stream_key"]
        self.files = [Path(p) for p in (*cfg["videos"], *cfg.get("audios", []))]
        self.video = self.files[0] if self.files else None
        self.max_seconds = int(max_hours * 3600)
        self.running, self.state, self.detail = True, "menyiapkan", "Menganalisis file pertama..."
        LOG_FILE.write_bytes(b"")
        threading.Thread(target=self._run, args=(cfg,), daemon=True).start()

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

    # ---------- analisis & kesiapan video ----------
    def _cache_for(self, p: Path, cfg: dict) -> Path:
        W, H, fps = self._target
        return self._ck(p, f"v2|{W}|{H}|{fps}|{int(cfg.get('vbr_k', 2500))}", ".mp4")

    @staticmethod
    def _ck(path: Path, params: str, ext: str) -> Path:
        st = path.stat()
        h = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}|{params}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{h}{ext}"

    def _copy_ok(self, p: Path, relaxed: bool) -> bool:
        key = (p, relaxed)
        ok = self._an.get(key)
        if ok is None:
            info = media_info(p)
            ok = bool(info["ok"] and info["v"])
            if ok:
                ok, why = video_copy_ok([p], [info], relaxed)
                if not ok:
                    self._log(f"{p.name}: diseragamkan dulu — {why}")
            if ok:
                v = info["v"]
                W, H, fps = self._target
                ok = (v["w"], v["h"]) == (W, H) and abs(v["fps"] - fps) <= 0.02
                if not ok:
                    self._log(f"{p.name}: diseragamkan dulu — beda resolusi/fps dari target {W}x{H}@{fps}")
            self._an[key] = ok
        return ok

    def _src(self, p: Path, cfg: dict) -> Path | None:
        """File yang BOLEH disiarkan sekarang (asli atau cache). None = belum siap (diantrekan ke background)."""
        if p in self._prep_failed:
            return None
        mode = cfg.get("vmode", "auto")
        cache = self._cache_for(p, cfg)
        if cache.exists():
            return cache
        if mode != "normalize" and self._copy_ok(p, relaxed=(mode == "copy")):
            return p
        self._enqueue_prep(p)
        return None

    def _frames_of(self, src: Path) -> int:
        n = self._frames.get(src)
        if n:
            return n
        info = media_info(src)
        n = (info["v"] or {}).get("nb_frames", 0) if info["ok"] else 0
        if n <= 0:
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(src)],
                    capture_output=True, text=True, timeout=900).stdout.strip()
                n = int(out.split(",")[0])
            except Exception:  # noqa: BLE001
                n = 0
        if n <= 0:
            n = max(1, int(round(info["dur"] * self._target[2])))
        self._frames[src] = n
        return n

    def _dur_of(self, src: Path) -> float:
        d = self._durs.get(src)
        if d is None:
            d = self._durs[src] = max(media_info(src)["dur"], 0.5)
        return d

    def _venc(self, cfg: dict) -> list[str]:
        W, H, fps = self._target
        vbr = int(cfg.get("vbr_k", 2500))
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p")
        g = str(fps * 2)
        return ["-vf", vf, "-c:v", "libx264", "-preset", "superfast", "-profile:v", "high",
                "-b:v", f"{vbr}k", "-maxrate", f"{int(vbr * 1.2)}k", "-bufsize", f"{vbr * 2}k",
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
        while not self.stop_event.is_set() and self.running:
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
                    self._log(f"cache dilewati (disk penuh): {p.name}")
                    self._prep_failed.add(p)
                    continue
                part = Path(str(dst) + ".part")
                part.unlink(missing_ok=True)
                self.prep_info = f"Menyeragamkan di background: {p.name}"
                dur = media_info(p)["dur"]
                cmd = ["nice", "-n", "15"] if shutil.which("nice") else []
                cmd += ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-progress", "pipe:1", "-nostats", "-i", str(p), "-map", "0:v:0", "-an",
                        *self._venc(cfg), "-movflags", "+faststart", "-f", "mp4", str(part)]
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
                    if not self.stop_event.is_set():
                        self._prep_failed.add(p)
                        tail = (LIVE_DIR / "prep_err.log").read_text(errors="replace")[-200:]
                        self._log(f"cache gagal: {p.name}: {tail}")
            except Exception as e:  # noqa: BLE001
                self._prep_failed.add(p)
                self._log(f"prep error: {e}")
            finally:
                self.prep_info = ""
        self.prep_info = ""

    # ---------- util feeder ----------
    def _put(self, sess: _Session, q: queue.Queue, item) -> bool:
        while not sess.stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    @staticmethod
    def _pcm_cmd(path: Path, seconds: float | None = None, ss: float | None = None) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if ss:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", str(path), "-map", "0:a:0", "-vn", "-ar", str(PCM_RATE), "-ac", str(PCM_CH)]
        if seconds:
            cmd += ["-t", f"{seconds:.6f}"]
        return cmd + ["-f", "s16le", "pipe:1"]

    def _pump(self, sess: _Session, q: queue.Queue, cmd: list[str], limit: int | None = None) -> tuple[int, int]:
        """Jalankan ffmpeg PCM dan dorong stdout ke antrean (maks `limit` byte). Return (byte_terkirim, returncode)."""
        sent = 0
        with open(LIVE_DIR / "feed_a.log", "wb") as ef:
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
            sess.procs.append(pr)
            try:
                while not sess.stop.is_set():
                    want = PCM_SEC // 2 if limit is None else min(PCM_SEC // 2, limit - sent)
                    if want <= 0:
                        break
                    buf = pr.stdout.read(want)
                    if not buf:
                        break
                    buf = buf[:len(buf) - len(buf) % PCM_FRAME]
                    if buf and not self._put(sess, q, buf):
                        break
                    sent += len(buf)
            finally:
                try:
                    pr.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
                if pr.poll() is None:
                    pr.kill()
                pr.wait()
                if pr in sess.procs:
                    sess.procs.remove(pr)
        return sent, pr.returncode

    def _push_zeros(self, sess: _Session, nbytes: int):
        step = PCM_SEC // 2
        while nbytes > 0 and not sess.stop.is_set():
            n = min(step, nbytes)
            if not self._put(sess, sess.aq, bytes(n)):
                return
            nbytes -= n

    # ---------- feeder video ----------
    def _video_feeder(self, sess: _Session, cfg: dict, videos: list[Path], audios: list[Path]):
        shuffle = bool(cfg.get("shuffle", True))
        from_video = cfg.get("audio_src", "file") == "video"
        fps = self._target[2]
        fails, bad = 0, set()
        frames_acc = samples_acc = 0
        errlog = LIVE_DIR / "feed_v.log"
        try:
            while not sess.stop.is_set():
                ready = []
                for v in videos:
                    if v in bad:
                        continue
                    try:
                        s = self._src(v, cfg)
                    except Exception as e:  # noqa: BLE001
                        bad.add(v)
                        self._log(f"video dilewati ({v.name}): {e}")
                        continue
                    if s is not None:
                        ready.append((v, s))
                if not ready:
                    if all(v in bad or v in self._prep_failed for v in videos):
                        raise RuntimeError("Semua video gagal disiapkan/dibaca.")
                    time.sleep(1)
                    continue
                p = self._next("v", [r[0] for r in ready], shuffle)
                src = dict(ready)[p]

                at, has_a = None, False
                if from_video:
                    frames_acc += self._frames_of(src)
                    target = round(frames_acc * PCM_RATE / fps)      # audio dipaku ke jumlah frame (lipsync eksak)
                    nbytes, samples_acc = (target - samples_acc) * PCM_FRAME, target
                    has_a = media_info(p)["a"] is not None
                if not self._put(sess, sess.vq, ("mark", p.name, "asli" if src == p else "cache")):
                    return
                if from_video:
                    at = threading.Thread(target=self._audio_for_video,
                                          args=(sess, cfg, audios, p, nbytes, has_a), daemon=True)
                    at.start()

                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
                       "-map", "0:v:0", "-an", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
                       "-f", "h264", "pipe:1"]
                got = 0
                with open(errlog, "wb") as ef:
                    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL)
                    sess.procs.append(pr)
                    while not sess.stop.is_set():
                        buf = pr.stdout.read(VCHUNK)
                        if not buf:
                            break
                        got += len(buf)
                        if not self._put(sess, sess.vq, buf):
                            break
                    pr.stdout.close()
                    pr.wait()
                if pr in sess.procs:
                    sess.procs.remove(pr)
                if at:
                    at.join()
                if sess.stop.is_set():
                    return
                if pr.returncode != 0 or got == 0:
                    bad.add(p)
                    fails += 1
                    tail = errlog.read_text(errors="replace")[-200:]
                    self._log(f"video gagal ({p.name}): {tail}")
                    if from_video:      # audio file ini sudah antre -> sesi diulang bersih agar tetap sinkron
                        raise RuntimeError(f"Video gagal dibaca: {p.name}")
                    if fails > 5 or len(bad) >= len(videos):
                        raise RuntimeError(f"Video gagal diputar: {p.name}: {tail}")
                    continue
                fails = 0
                sess.v_secs += self._dur_of(src)
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder video: {e}"
            sess.kill_all()

    # ---------- feeder audio ----------
    def _audio_feeder(self, sess: _Session, cfg: dict, audios: list[Path]):
        """Mode 'file': playlist audio berjalan sendiri, lagu demi lagu."""
        shuffle = bool(cfg.get("shuffle", True))
        loop = bool(cfg.get("loop_audio", True))
        played, fails, bad = 0, 0, set()
        try:
            while not sess.stop.is_set():
                if not loop and played >= len(audios):
                    break
                p = self._next("a", audios, shuffle and loop, skip=bad)
                if not self._put(sess, sess.aq, ("mark", p.name, "")):
                    return
                sent, rc = self._pump(sess, sess.aq, self._pcm_cmd(p))
                if sess.stop.is_set():
                    return
                played += 1
                if rc != 0 or sent == 0:
                    bad.add(p)
                    fails += 1
                    tail = (LIVE_DIR / "feed_a.log").read_text(errors="replace")[-200:]
                    self._log(f"audio gagal ({p.name}): {tail}")
                    if fails > 5 or len(bad) >= len(audios):
                        raise RuntimeError(f"Audio tidak bisa didecode: {p.name}")
                    continue
                fails = 0
            sess.audio_done = True
            self._put(sess, sess.aq, None)
        except Exception as e:  # noqa: BLE001
            sess.error = f"feeder audio: {e}"
            sess.kill_all()

    def _audio_for_video(self, sess: _Session, cfg: dict, audios: list[Path], p: Path, nbytes: int, has_a: bool):
        """Mode 'video': audio sepanjang tepat satu video (aslinya, atau playlist/hening jika video tanpa audio)."""
        try:
            if has_a:
                self._put(sess, sess.aq, ("mark", p.name, "audio dari video"))
                sent, rc = self._pump(sess, sess.aq, self._pcm_cmd(p, seconds=nbytes / PCM_SEC), limit=nbytes)
                if sent < nbytes and not sess.stop.is_set():
                    if sent == 0:
                        self._log(f"audio video tidak terbaca, diisi hening: {p.name}")
                    self._push_zeros(sess, nbytes - sent)     # audio lebih pendek dari video -> sisanya hening
            else:
                self._fill_from_playlist(sess, cfg, audios, nbytes)
        except Exception as e:  # noqa: BLE001
            sess.error = f"audio video: {e}"
            sess.kill_all()

    def _fill_from_playlist(self, sess: _Session, cfg: dict, audios: list[Path], nbytes: int):
        """Ambil `nbytes` PCM dari playlist audio, lanjut dari posisi terakhir (lintas video)."""
        st, need, fails = self._pl, nbytes, 0
        shuffle = bool(cfg.get("shuffle", True))
        while need > 0 and not sess.stop.is_set():
            if not audios or fails > 10:
                break
            if st["track"] is None:
                cand = [a for a in audios if a not in st["bad"]]
                if not cand:
                    break
                st["track"], st["pos"] = self._next("a", cand, shuffle), 0.0
                st["dur"] = media_info(st["track"])["dur"]
                self._put(sess, sess.aq, ("mark", st["track"].name, "playlist"))
            take = min(need, int(max(st["dur"] - st["pos"], 0.0) * PCM_RATE) * PCM_FRAME)
            if take < PCM_FRAME * 64:                          # sisa lagu terlalu pendek -> lagu berikutnya
                st["track"] = None
                fails += 1
                continue
            sent, _rc = self._pump(sess, sess.aq,
                                   self._pcm_cmd(st["track"], seconds=take / PCM_SEC, ss=st["pos"]), limit=take)
            if sent == 0:
                st["bad"].add(st["track"])
                st["track"] = None
                fails += 1
                continue
            fails = 0
            need -= sent
            st["pos"] += sent / PCM_SEC
            if sent < take or st["pos"] >= st["dur"] - 0.02:
                st["track"] = None
        if need > 0 and not sess.stop.is_set():
            self._push_zeros(sess, need)                       # tidak ada audio -> hening (tetap tersinkron)

    # ---------- penulis pipe ----------
    def _writer(self, sess: _Session, fobj, q: queue.Queue, silence: bytes | None):
        """Tulis antrean ke pipe sink. Audio telat -> sisipkan hening agar aliran tidak pernah kosong."""
        try:
            while not sess.stop.is_set():
                try:
                    buf = q.get(timeout=0.4)
                except queue.Empty:
                    if silence is not None and sess.live.is_set():
                        sess.underruns += 1
                        self.underruns = sess.underruns
                        if sess.underruns in (1, 10, 100, 1000):
                            self._log(f"audio telat, hening disisipkan (total {sess.underruns}x)")
                        fobj.write(silence)
                        fobj.flush()
                    continue
                if buf is None:
                    break
                if isinstance(buf, tuple):          # penanda: file ini mulai masuk antrean kirim
                    if silence is None:
                        self.now_video, self.switches = buf[1], self.switches + 1
                        self._log(f"video -> {buf[1]} ({buf[2]})")
                    else:
                        self.now_audio = buf[1]
                        self._log(f"audio -> {buf[1]}" + (f" ({buf[2]})" if buf[2] else ""))
                    continue
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
    def build_cmd(self, cfg: dict, vfd: int, afd: int) -> list[str]:
        out = cfg.get("_output") or (cfg["rtmp_url"].rstrip("/") + "/" + cfg["stream_key"])
        fps = self._target[2]
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-nostdin", "-nostats",
               "-progress", "pipe:1",
               "-probesize", "2M", "-analyzeduration", "2M",
               "-thread_queue_size", "1024", "-re", "-f", "h264", "-framerate", str(fps), "-i", f"pipe:{vfd}",
               "-thread_queue_size", "1024", "-re", "-f", "s16le", "-ar", str(PCM_RATE), "-ac", str(PCM_CH),
               "-i", f"pipe:{afd}",
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "128k", "-ar", str(PCM_RATE), "-ac", "2",
               "-af", "aresample=async=1000"]
        if cfg.get("audio_src", "file") == "file" and not cfg.get("loop_audio", True):
            cmd += ["-shortest"]
        cmd += ["-flvflags", "no_duration_filesize", "-f", "flv", out]
        return cmd

    def _read_progress(self, proc):
        cur: dict = {}
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
                self.progress = {"speed": cur.get("speed", ""), "bitrate": cur.get("bitrate", ""),
                                 "size": cur.get("total_size", ""), "out_time": self._out_time}
                cur = {}

    def _session(self, cfg: dict, videos, audios, deadline) -> str:
        sess = _Session()
        self._sess = sess
        self._pl = {"track": None, "pos": 0.0, "dur": 0.0, "bad": set()}
        vr, vw = os.pipe()
        ar, aw = os.pipe()
        self.state, self.detail = "menyiapkan", "Mengisi buffer video & audio sebelum tayang..."
        silence = bytes(PCM_SEC // 2)
        from_video = cfg.get("audio_src", "file") == "video"
        feeders = [(self._video_feeder, (sess, cfg, videos, audios))]
        if not from_video:
            feeders.append((self._audio_feeder, (sess, cfg, audios)))
        feeders += [(self._writer, (sess, os.fdopen(vw, "wb", buffering=0), sess.vq, None)),
                    (self._writer, (sess, os.fdopen(aw, "wb", buffering=0), sess.aq, silence))]
        for target, args in feeders:
            threading.Thread(target=target, args=args, daemon=True).start()

        # Buka koneksi ke YouTube HANYA setelah buffer terisi -> tidak ada awal kosong / macet.
        t_wait, early = time.time(), None
        while not ((sess.vq.qsize() >= 24 or sess.v_secs >= 5) and (sess.aq.qsize() >= 6 or sess.audio_done)):
            if self.stop_event.is_set():
                early = "stop"
            elif deadline and time.time() >= deadline:
                early = "deadline"
            elif sess.error:
                early = "error"
            elif time.time() - t_wait > 60:
                self._log("buffer awal lambat (>60 dtk), tetap mulai.")
                break
            if early:
                break
            time.sleep(0.3)
        if early:
            sess.kill_all()
            os.close(vr)
            os.close(ar)
            self._sess = None
            if early == "error":
                self._log(f"SESI ERROR: {sess.error}")
                self.detail = sess.error or ""
            return early

        self.state, self.detail = "menghubungkan", "Memulai ffmpeg..."
        self._out_time, self._prog_change = 0.0, time.time()
        t_launch = time.time()
        cmd = self.build_cmd(cfg, vr, ar)
        with open(LOG_FILE, "ab") as log:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True,
                                         stdin=subprocess.DEVNULL, pass_fds=(vr, ar))
        os.close(vr)
        os.close(ar)
        sess.live.set()
        sess.procs.append(self.proc)
        threading.Thread(target=self._read_progress, args=(self.proc,), daemon=True).start()

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
            if self.proc.poll() is not None:
                reason = "died"
                break
            now = time.time()
            self.buf_v = sess.vq.qsize() * VCHUNK / 1e6
            self.buf_a = sess.aq.qsize() * 0.5
            if self._out_time > 0:
                self.state, self.detail = "live", ""
            if now - t_launch > 90 and now - self._prog_change > 30:
                self._log("Stream macet >30 detik, sesi di-restart.")
                reason = "stalled"
                break
            time.sleep(1)
        rc = self.proc.returncode
        sess.kill_all()
        self._kill(self.proc)
        self._sess = None
        if reason == "died" and rc == 0 and not from_video and not cfg.get("loop_audio", True):
            reason = "finished"
        if reason == "error":
            self._log(f"SESI ERROR: {sess.error}")
            self.detail = sess.error or ""
        return reason

    # ---------- loop utama ----------
    def _run(self, cfg: dict):
        videos = [Path(p) for p in cfg["videos"]]
        audios = [Path(p) for p in cfg.get("audios", [])]
        from_video = cfg.get("audio_src", "file") == "video"
        try:
            if not from_video and not audios:
                raise RuntimeError("Mode audio 'dari file' membutuhkan minimal 1 file audio.")
            for p in videos:
                i = media_info(p)
                if not i["ok"] or not i["v"]:
                    raise RuntimeError(f"File video tidak valid / tidak terbaca: {p.name}")
            good = []
            for p in audios:
                i = media_info(p)
                if i["ok"] and i["a"]:
                    good.append(p)
                elif not from_video:
                    raise RuntimeError(f"File audio tidak valid / tidak terbaca: {p.name}")
                else:
                    self._log(f"audio dilewati (tidak valid): {p.name}")
            audios = good
            cfg["audios"] = audios
            W, H = RES.get(int(cfg.get("height", 720)), RES[720])
            fps = int(cfg.get("fps", 30))
            vmode = cfg.get("vmode", "auto")
            # target seragam = format video pertama jika sudah layak live, selain itu pengaturan di UI
            if vmode != "normalize":
                i0 = media_info(videos[0])
                ok0, _ = video_copy_ok([videos[0]], [i0], relaxed=(vmode == "copy"))
                if ok0:
                    self._target = (i0["v"]["w"], i0["v"]["h"], int(round(i0["v"]["fps"])) or fps)
                else:
                    self._target = (W, H, fps)
            else:
                self._target = (W, H, fps)
            self.notes = [
                "Video diacak tiap pergantian." if cfg.get("shuffle", True) else "Video diputar berurutan dan diulang.",
                f"Target video seragam {self._target[0]}x{self._target[1]} {self._target[2]}fps (H.264, tanpa B-frame).",
                ("Audio: dari video masing-masing; video tanpa audio memakai playlist audio"
                 + (" / hening." if not audios else ".")) if from_video
                else "Audio: playlist audio, video dibisukan.",
                "Hanya video yang sudah siap yang disiarkan; sisanya diseragamkan di background lalu otomatis masuk putaran.",
                "Koneksi ke YouTube dibuka setelah buffer terisi, audio tidak pernah kosong (hening disisipkan jika telat)."]
            threading.Thread(target=self._prep_worker, args=(cfg,), daemon=True).start()
            # video yang sudah siap lebih dulu; yang perlu diseragamkan diurut dari yang terpendek (cepat tayang)
            order = sorted(videos, key=lambda p: media_info(p)["dur"])
            for p in order:
                self._src(p, cfg)

            self.state, self.detail = "menyiapkan", "Menyeragamkan video pertama — live dimulai setelah ada video siap..."
            while not self.stop_event.is_set():
                if any(self._src(p, cfg) is not None for p in videos):
                    break
                if all(p in self._prep_failed for p in videos):
                    raise RuntimeError("Semua video gagal diseragamkan (lihat log / cek disk).")
                time.sleep(1)
        except Exception as e:  # noqa: BLE001
            self.running, self.state, self.detail = False, "error", f"{e}"
            self._log(f"PERSIAPAN GAGAL: {e}")
            self._kill(self.prep_proc)
            return

        deadline = self.started_at + self.max_seconds if self.max_seconds else None
        backoff = 3
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
            backoff = 3 if ran > 120 else min(backoff * 2, 60)
            self.state = "menyambung ulang"
            self.detail = f"{reason}; coba lagi {wait_s}s" + (f" ({self.detail})" if reason == "error" and self.detail else "")
            self.stop_event.wait(wait_s)
        self.running = False
        self.state = "selesai" if self.finished else "idle"
        self.detail = ""
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
        return text.replace(self.stream_key, "********") if self.stream_key else text

"""Tes keseragaman & normalisasi file media (audio / video) untuk Studio Render & Live.

Dua tugas utama:
  1. TES      -> ffprobe / ebur128 / tes decode, lalu dibandingkan dengan profil target
                 (hasil: ✅ OK · ⚠️ tidak seragam · ❌ bermasalah, lengkap dengan alasannya).
  2. NORMALISASI -> samakan format (audio AAC .m4a, video H.264 ukuran/fps seragam) lewat job
                 background; hasil ditulis ke file sementara dulu, diverifikasi, baru dipindah
                 ke Library (tidak pernah ada file setengah jadi di Library).

Video hasil normalisasi memenuhi syarat `services.video_copy_ok` (H.264 yuv420p, keyframe tiap
2 detik, bitrate di bawah batas) sehingga saat Live dikirim tanpa re-encode (CPU ≈ 0).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import services as svc

OK, WARN, BAD = "✅ OK", "⚠️ Tidak seragam", "❌ Bermasalah"
LOW_PRIO = ["nice", "-n", "15"] if shutil.which("nice") else []   # jangan mengganggu live yang sedang jalan


# ───────────────────────── profil target ─────────────────────────
@dataclass(frozen=True)
class AudioTarget:
    sr: int = 44100                 # sama dengan PCM_RATE di jalur live
    ch: int = 2
    bitrate_k: int = 160
    lufs: float = -16.0             # loudness target (EBU R128, cocok untuk YouTube/streaming)
    tp: float = -1.5                # batas true-peak (dBTP)
    lra: float = 11.0
    tol_lu: float = 2.0             # toleransi selisih loudness antar file (LU)
    ext: str = ".m4a"
    min_dur: float = 0.0            # file lebih pendek dari ini diberi catatan (mis. 3 × crossfade maks)


@dataclass(frozen=True)
class VideoTarget:
    height: int = 720
    fps: int = 30
    vbr_k: int = 0                  # 0 = otomatis (svc.auto_vbr)
    keep_audio: bool = True
    abr_k: int = 128
    preset: str = "veryfast"

    @property
    def size(self) -> tuple[int, int]:
        return svc.RES[self.height]

    @property
    def bitrate(self) -> int:
        return self.vbr_k or svc.auto_vbr(self.height, self.fps)


# ───────────────────────── util ffprobe ─────────────────────────
def _ffprobe(path: Path) -> dict | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                            str(path)], capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return None
    return j if r.returncode == 0 and j.get("streams") else None


def _rate(x: str | None) -> float:
    try:
        n, d = (x or "0/1").split("/")
        return round(int(n) / int(d), 3) if int(d) else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _decode_check(cmd: list[str], timeout: int) -> tuple[str, float | None]:
    """Jalankan ffmpeg ... -f null; kembalikan (teks error, detik yang berhasil didecode).
    Error '' = bersih. Detik terdecode dipakai mendeteksi file terpotong (durasi header > isi nyata)."""
    cmd = [*cmd[:-3], "-progress", "pipe:1", "-nostats", *cmd[-3:]]       # sisipkan sebelum '-f null -'
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return "tes decode melewati batas waktu", None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}", None
    decoded = None
    for line in r.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                decoded = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                pass
    err = (r.stderr or "").strip()
    if r.returncode != 0 and not err:
        err = f"ffmpeg keluar dengan kode {r.returncode}"
    return err[:300], decoded


# cache hasil ukur: kunci = (path, ukuran, mtime, opsi) -> ganti target tidak perlu mengukur ulang
_CACHE: dict[tuple, dict] = {}
_CLOCK = threading.Lock()


def _cached(key: tuple, fn):
    with _CLOCK:
        if key in _CACHE:
            return _CACHE[key]
    val = fn()
    with _CLOCK:
        if len(_CACHE) > 3000:
            _CACHE.clear()
        _CACHE[key] = val
    return val


def _fkey(path: Path, *opts) -> tuple:
    st = path.stat()
    return (str(path), st.st_size, st.st_mtime_ns, *opts)


# ───────────────────────── TES AUDIO ─────────────────────────
_I_RE = re.compile(r"\bI:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS")
_LRA_RE = re.compile(r"\bLRA:\s+(-?\d+(?:\.\d+)?)\s+LU\b")
_TP_RE = re.compile(r"\bPeak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS")


def _loudness(path: Path) -> dict:
    cmd = [*LOW_PRIO, "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", str(path),
           "-map", "0:a:0", "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    text = r.stderr.rsplit("Summary:", 1)[-1] if "Summary:" in r.stderr else ""
    mi, ml, mt = _I_RE.search(text), _LRA_RE.search(text), _TP_RE.search(text)
    if not mi:
        return {}

    def f(v):
        return -70.0 if v == "-inf" else float(v)
    return {"lufs": f(mi.group(1)), "lra": float(ml.group(1)) if ml else None,
            "tp": f(mt.group(1)) if mt else None}


def measure_audio(path: Path, loudness: bool = True, decode: bool = True) -> dict:
    """Ukur satu file audio (di-cache). Tidak membandingkan dengan target."""
    path = Path(path)

    def work() -> dict:
        j = _ffprobe(path)
        raw = {"name": path.name, "ext": path.suffix.lower(), "mb": path.stat().st_size / 1024 / 1024,
               "probe_ok": False}
        if not j:
            return raw
        streams = j["streams"]
        aud = [s for s in streams if s.get("codec_type") == "audio"]
        fmt = j.get("format", {})
        raw.update(
            probe_ok=True, container=fmt.get("format_name", ""), nstreams=len(streams),
            extra=len(streams) - len(aud), dur=_num(fmt.get("duration")),
            has_audio=bool(aud))
        if aud:
            a = aud[0]
            raw.update(codec=a.get("codec_name"), sr=int(_num(a.get("sample_rate"))),
                       ch=int(_num(a.get("channels"))),
                       kbps=int(_num(a.get("bit_rate")) or _num(fmt.get("bit_rate"))) // 1000)
        if aud and loudness:
            raw.update(_loudness(path))
            raw["loud_done"] = True
        if aud and decode:
            raw["decode_err"], raw["decoded"] = _decode_check(
                [*LOW_PRIO, "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0",
                 "-f", "null", "-"], timeout=900)
        return raw
    return _cached(_fkey(path, "a", loudness, decode), work)


def evaluate_audio(raw: dict, t: AudioTarget) -> dict:
    """Bandingkan hasil ukur dengan target -> status + daftar catatan."""
    bad: list[str] = []
    warn: list[str] = []
    if not raw.get("probe_ok"):
        bad.append("tidak terbaca ffprobe (rusak / bukan media)")
    elif not raw.get("has_audio"):
        bad.append("tidak ada stream audio")
    else:
        if raw.get("dur", 0) <= 0:
            bad.append("durasi 0")
        if raw.get("decode_err"):
            bad.append("error decode: " + raw["decode_err"].splitlines()[0][:120])
        dec = raw.get("decoded")
        if dec is not None and raw.get("dur", 0) > 2 and dec < raw["dur"] * 0.95 - 1:
            bad.append(f"file terpotong/rusak: hanya {dec:.0f}s dari {raw['dur']:.0f}s yang bisa didecode")
        if raw.get("codec") != "aac":
            warn.append(f"codec {raw.get('codec')} (target aac)")
        if raw.get("sr") != t.sr:
            warn.append(f"{raw.get('sr')} Hz (target {t.sr})")
        if raw.get("ch") != t.ch:
            warn.append(f"{raw.get('ch')} channel (target {t.ch})")
        if raw["ext"] != t.ext:
            warn.append(f"ekstensi {raw['ext']} (target {t.ext})")
        if raw.get("extra"):
            warn.append("ada cover/stream tambahan")
        if raw.get("loud_done"):
            lu = raw.get("lufs")
            if lu is None:
                warn.append("loudness tidak terukur")
            elif lu <= -60:
                bad.append("senyap (tidak ada suara)")
            elif abs(lu - t.lufs) > t.tol_lu:
                warn.append(f"loudness {lu:.1f} LUFS (target {t.lufs:g} ±{t.tol_lu:g})")
            tp = raw.get("tp")
            if tp is not None and tp > -1.0:
                warn.append(f"puncak {tp:.1f} dBFS (rawan clipping saat di-mix)")
        if t.min_dur and 0 < raw.get("dur", 0) < t.min_dur:
            warn.append(f"pendek ({raw['dur']:.0f}s < {t.min_dur:.0f}s): crossfade akan diperpendek")
    status = BAD if bad else WARN if warn else OK
    return {"status": status, "notes": "; ".join(bad + warn)}


# ───────────────────────── TES VIDEO ─────────────────────────
def measure_video(path: Path, gop: bool = True, decode: str = "none") -> dict:
    """decode: 'none' | 'quick' (60 dtk awal + 60 dtk akhir) | 'full'."""
    path = Path(path)

    def work() -> dict:
        j = _ffprobe(path)
        raw = {"name": path.name, "ext": path.suffix.lower(), "mb": path.stat().st_size / 1024 / 1024,
               "probe_ok": False}
        if not j:
            return raw
        streams = j["streams"]
        vids = [s for s in streams if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
        auds = [s for s in streams if s.get("codec_type") == "audio"]
        fmt = j.get("format", {})
        dur = _num(fmt.get("duration")) or max([_num(s.get("duration")) for s in streams] or [0.0])
        raw.update(probe_ok=True, dur=dur, has_video=bool(vids), has_audio=bool(auds))
        if vids:
            v = vids[0]
            rot = _num((v.get("tags") or {}).get("rotate"))
            for sd in v.get("side_data_list") or []:
                if "rotation" in sd:
                    rot = _num(sd["rotation"])
            raw.update(codec=v.get("codec_name"), profile=v.get("profile"), pix=v.get("pix_fmt"),
                       w=int(_num(v.get("width"))), h=int(_num(v.get("height"))),
                       fps=_rate(v.get("avg_frame_rate")), rfps=_rate(v.get("r_frame_rate")),
                       sar=v.get("sample_aspect_ratio"), rot=int(abs(rot)) % 360,
                       kbps=int(_num(v.get("bit_rate")) or _num(fmt.get("bit_rate"))) // 1000)
            if gop and v.get("codec_name") == "h264":          # codec lain pasti di-transkode, tak perlu diukur
                raw["gap"] = svc.keyframe_gap(path, dur)
                raw["gop_done"] = True
        if auds:
            a = auds[0]
            raw.update(acodec=a.get("codec_name"), asr=int(_num(a.get("sample_rate"))),
                       ach=int(_num(a.get("channels"))))
        if vids and decode != "none":
            base = [*LOW_PRIO, "ffmpeg", "-v", "error", "-nostdin"]
            tail = ["-i", str(path), "-map", "0:v:0", "-threads", "2", "-f", "null", "-"]
            decoded = None
            if decode == "full":
                errs, decoded = _decode_check([*base, *tail], timeout=int(max(600, dur * 0.8)))
            else:
                errs, _ = _decode_check([*base, "-t", "60", *tail], timeout=300)
                if not errs and dur > 130:
                    errs, _ = _decode_check([*base, "-sseof", "-60", *tail], timeout=300)
            raw["decoded"] = decoded
            raw["decode_err"] = errs
        return raw
    return _cached(_fkey(path, "v", gop, decode), work)


def evaluate_video(raw: dict, t: VideoTarget) -> dict:
    bad: list[str] = []
    warn: list[str] = []
    if not raw.get("probe_ok"):
        bad.append("tidak terbaca ffprobe (rusak / bukan media)")
    elif not raw.get("has_video"):
        bad.append("tidak ada stream video")
    else:
        W, H = t.size
        if raw.get("dur", 0) <= 0:
            bad.append("durasi 0")
        if raw.get("decode_err"):
            bad.append("error decode: " + raw["decode_err"].splitlines()[0][:120])
        dec = raw.get("decoded")
        if dec is not None and raw.get("dur", 0) > 2 and dec < raw["dur"] * 0.95 - 1:
            bad.append(f"file terpotong/rusak: hanya {dec:.0f}s dari {raw['dur']:.0f}s yang bisa didecode")
        if raw.get("codec") != "h264":
            warn.append(f"codec {raw.get('codec')} (target h264)")
        if raw.get("pix") != "yuv420p":
            warn.append(f"pix_fmt {raw.get('pix')} (target yuv420p)")
        if (raw["w"], raw["h"]) != (W, H):
            warn.append(f"{raw['w']}x{raw['h']} (target {W}x{H})")
        if raw["w"] % 2 or raw["h"] % 2:
            warn.append("resolusi ganjil")
        if abs(raw["fps"] - t.fps) > 0.02:
            warn.append(f"{raw['fps']:g} fps (target {t.fps})")
        if raw["fps"] and raw["rfps"] and abs(raw["fps"] - raw["rfps"]) > 0.5:
            warn.append("frame rate tidak konstan (VFR)")
        if raw.get("rot"):
            warn.append(f"metadata rotasi {raw['rot']}°")
        if raw.get("sar") not in (None, "", "1:1", "N/A", "0:1"):
            warn.append(f"SAR {raw['sar']} (target 1:1)")
        if raw.get("kbps") and raw["kbps"] * 1000 > svc.br_cap(t.height):
            warn.append(f"bitrate {raw['kbps']} kbps terlalu tinggi untuk live")
        if raw.get("gop_done"):
            gap = raw.get("gap")
            if gap is None:
                warn.append("jarak keyframe tidak terukur")
            elif gap > 4.0:
                warn.append(f"keyframe tiap {gap:.1f} dtk (YouTube butuh ≤ 4)")
        if raw.get("has_audio"):
            if not t.keep_audio:
                warn.append("masih ada audio di dalam video (tidak dipakai live)")
            elif raw.get("acodec") != "aac":
                warn.append(f"audio {raw.get('acodec')} (target aac)")
    status = BAD if bad else WARN if warn else OK
    return {"status": status, "notes": "; ".join(bad + warn)}


# ───────────────────────── laporan ─────────────────────────
def _fmt_dur(sec: float) -> str:
    s = int(sec or 0)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def audio_row(path: Path, t: AudioTarget, loudness: bool, decode: bool) -> dict:
    raw = measure_audio(path, loudness, decode)
    ev = evaluate_audio(raw, t)
    return {"Status": ev["status"], "File": raw["name"], "Codec": raw.get("codec") or "-",
            "Hz": raw.get("sr") or 0, "Ch": raw.get("ch") or 0, "kbps": raw.get("kbps") or 0,
            "Durasi": _fmt_dur(raw.get("dur", 0)), "LUFS": raw.get("lufs"), "Peak dBFS": raw.get("tp"),
            "LRA": raw.get("lra"), "MB": round(raw["mb"], 1), "Catatan": ev["notes"], "_path": str(path)}


def video_row(path: Path, t: VideoTarget, gop: bool, decode: str) -> dict:
    raw = measure_video(path, gop, decode)
    ev = evaluate_video(raw, t)
    gap = raw.get("gap")
    return {"Status": ev["status"], "File": raw["name"], "Codec": raw.get("codec") or "-",
            "Ukuran": f"{raw.get('w', 0)}x{raw.get('h', 0)}", "FPS": raw.get("fps") or 0,
            "kbps": raw.get("kbps") or 0, "Keyframe (dtk)": round(gap, 1) if gap else None,
            "Audio": raw.get("acodec") or "-", "Durasi": _fmt_dur(raw.get("dur", 0)),
            "MB": round(raw["mb"], 1), "Catatan": ev["notes"], "_path": str(path)}


def run_tests(paths, row_fn, workers: int = 3, progress=None) -> list[dict]:
    """Jalankan row_fn(path) paralel (urutan hasil = urutan paths). progress(done, total) opsional."""
    paths = list(paths)
    rows: list[dict | None] = [None] * len(paths)

    def one(i: int):
        try:
            rows[i] = row_fn(paths[i])
        except Exception as e:  # noqa: BLE001
            rows[i] = {"Status": BAD, "File": Path(paths[i]).name, "Catatan": f"{type(e).__name__}: {e}",
                       "_path": str(paths[i])}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for fut in [ex.submit(one, i) for i in range(len(paths))]:
            fut.result()
            done += 1
            if progress:
                progress(done, len(paths))
    return [r for r in rows if r]


def summarize(rows: list[dict], kind: str, t_audio: AudioTarget | None = None) -> dict:
    """Ringkasan: jumlah per status, verdict seragam/aman, dan kelompok format yang berbeda."""
    c = Counter(r["Status"] for r in rows)
    if kind == "audio":
        groups = Counter(f"{r.get('Codec')} · {r.get('Hz')} Hz · {r.get('Ch')} ch" for r in rows
                         if r["Status"] != BAD)
        lufs = [r["LUFS"] for r in rows if r.get("LUFS") is not None and r["Status"] != BAD]
        spread = (max(lufs) - min(lufs)) if len(lufs) > 1 else 0.0
    else:
        groups = Counter(f"{r.get('Codec')} · {r.get('Ukuran')} · {r.get('FPS'):g} fps" for r in rows
                         if r["Status"] != BAD)
        spread = 0.0
    uniform = bool(rows) and c[OK] == len(rows)
    notes: list[str] = []
    if kind == "audio" and spread > 2 * (t_audio.tol_lu if t_audio else 2.0):
        notes.append(f"Selisih loudness antar file {spread:.1f} LU: volume terasa naik-turun saat lagu berganti.")
    if kind == "video":
        has_a = Counter(r.get("Audio") not in (None, "-") for r in rows if r["Status"] != BAD)
        if len(has_a) > 1:
            notes.append("Sebagian video punya audio, sebagian tidak. Aman untuk Live (audio video tidak dipakai), "
                         "tetapi bisa menghalangi penyambungan tanpa re-encode (merge copy).")
    return {"total": len(rows), "ok": c[OK], "warn": c[WARN], "bad": c[BAD], "uniform": uniform,
            "groups": groups.most_common(), "lufs_spread": spread, "notes": notes}


# ───────────────────────── NORMALISASI ─────────────────────────
def _run_ffmpeg(cmd: list[str], dur: float, job: dict, label: str) -> tuple[int, str]:
    """Jalankan ffmpeg dengan -progress pipe:1; update job['detail'] berupa persen. stderr ke file sementara."""
    with tempfile.TemporaryFile() as ef:
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, text=True, stdin=subprocess.DEVNULL)
        try:
            for line in pr.stdout:
                if line.startswith("out_time_us=") and dur > 0:
                    try:
                        t = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    job["detail"] = f"{label} {min(99, t / dur * 100):.0f}%"
        finally:
            pr.stdout.close()
            rc = pr.wait()
        ef.seek(0)
        tail = ef.read()[-500:].decode("utf-8", "replace")
    return rc, tail


def _disk_ok(src: Path, disk_min_gb: float) -> str | None:
    free = shutil.disk_usage(svc.BASE).free
    need = disk_min_gb * 1024**3 + src.stat().st_size
    if free < need:
        return f"Disk kurang ({free / 1024**3:.2f} GB bebas, butuh ±{need / 1024**3:.2f} GB)."
    return None


def _finalize(tmp: Path, src: Path, ext: str, mode: str, protected_fn) -> Path:
    """Pindahkan hasil ke Library. mode 'new' = file baru *_norm; 'replace' = gantikan file asli."""
    if mode == "replace":
        prot = {Path(p).resolve() for p in (protected_fn() if protected_fn else []) if p}
        if src.resolve() in prot:
            raise RuntimeError("file sedang dipakai live; hentikan live atau pilih mode 'file baru'")
        dest = src if src.suffix.lower() == ext else svc.unique_path(src.parent, svc.safe_name(src.stem + ext))
        if dest == src:
            tmp.replace(src)
        else:
            shutil.move(str(tmp), str(dest))
            src.unlink(missing_ok=True)
        return dest
    dest = svc.unique_path(src.parent, svc.safe_name(src.stem + "_norm" + ext))
    shutil.move(str(tmp), str(dest))
    return dest


def _loudnorm_pass1(src: Path, t: AudioTarget) -> dict | None:
    cmd = [*LOW_PRIO, "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", str(src), "-map", "0:a:0", "-vn",
           "-af", f"loudnorm=I={t.lufs}:TP={t.tp}:LRA={t.lra}:print_format=json", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, errors="replace")
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
        d = json.loads(m.group(0)) if m else None
    except Exception:  # noqa: BLE001
        return None
    if not d or any(str(d.get(k, "")).strip("-").lower() in ("inf", "nan", "") for k in
                    ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        return None                       # senyap / tidak terukur -> pakai mode 1-pass
    return d


def normalize_audio(job: dict, src: Path, t: AudioTarget, opts: dict, disk_min_gb: float, protected_fn=None):
    """Audio apa pun -> AAC .m4a seragam (sample rate, channel, bitrate, loudness). Return (ok, info)."""
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        return False, f"File tidak ada / 0 byte: {src.name}"
    loud = opts.get("loud", "two")                   # two | one | off
    ev = evaluate_audio(measure_audio(src, loudness=loud != "off", decode=False), t)
    if ev["status"] == OK and opts.get("skip_ok", True):
        return True, "sudah seragam, dilewati"
    if ev["status"] == BAD:
        return False, "dilewati, file bermasalah: " + ev["notes"]
    if (msg := _disk_ok(src, disk_min_gb)):
        return False, msg
    info = svc.media_info(src)
    if not info["ok"] or not info["a"]:
        return False, f"Tidak ada audio yang bisa dibaca: {src.name}"
    dur = info["dur"]
    job["detail"] = "Mengukur loudness..." if loud == "two" else "Memproses..."
    af, note = [], "format saja"
    if loud != "off":
        d = _loudnorm_pass1(src, t) if loud == "two" else None
        base = f"loudnorm=I={t.lufs}:TP={t.tp}:LRA={t.lra}"
        if d:
            af.append(f"{base}:measured_I={d['input_i']}:measured_TP={d['input_tp']}:measured_LRA={d['input_lra']}"
                      f":measured_thresh={d['input_thresh']}:offset={d['target_offset']}:linear=true")
            note = f"{float(d['input_i']):.1f} → {t.lufs:g} LUFS (2-pass)"
        else:
            af.append(base)
            note = f"→ {t.lufs:g} LUFS (1-pass)"
    tmp = svc.TMP / f"norm_{uuid.uuid4().hex[:8]}.m4a"
    cmd = [*LOW_PRIO, "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1",
           "-nostats", "-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_chapters", "-1"]
    cmd += ["-map_metadata", "-1" if opts.get("strip_meta") else "0"]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:a", "aac", "-b:a", f"{t.bitrate_k}k", "-ar", str(t.sr), "-ac", str(t.ch),
            "-movflags", "+faststart", "-f", "ipod", str(tmp)]
    try:
        rc, tail = _run_ffmpeg(cmd, dur, job, "Encode AAC")
        if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return False, f"ffmpeg gagal: {tail.strip()[-300:]}"
        out = svc.media_info(tmp)
        o = out.get("a") or {}
        if not out["ok"] or o.get("codec") != "aac" or o.get("sr") != t.sr or o.get("ch") != t.ch:
            return False, "Verifikasi hasil gagal (format keluaran tidak sesuai)."
        if abs(out["dur"] - dur) > max(1.5, dur * 0.02):
            return False, f"Verifikasi gagal: durasi berubah {dur:.1f}s → {out['dur']:.1f}s."
        dest = _finalize(tmp, src, ".m4a", opts.get("mode", "new"), protected_fn)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    return True, f"{dest.name} | AAC {t.bitrate_k}k {t.sr}Hz · {note}"


def video_encode_args(t: VideoTarget, threads: int = 0) -> list[str]:
    """Argumen encoder seragam. Bitrate nyaris konstan + keyframe tiap 2 detik -> aman untuk live (copy)."""
    W, H = t.size
    vbr = t.bitrate
    g = str(t.fps * 2)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={t.fps},format=yuv420p")
    args = ["-vf", vf, "-c:v", "libx264", "-preset", t.preset, "-profile:v", "high",
            "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{int(vbr * 1.5)}k",
            "-g", g, "-keyint_min", g, "-sc_threshold", "0", "-bf", "2"]
    if threads > 0:
        args += ["-threads", str(threads)]
    return args


def normalize_video(job: dict, src: Path, t: VideoTarget, opts: dict, disk_min_gb: float, protected_fn=None):
    """Video apa pun -> MP4 H.264 yuv420p, ukuran/fps/GOP seragam. Return (ok, info)."""
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        return False, f"File tidak ada / 0 byte: {src.name}"
    ev = evaluate_video(measure_video(src, gop=True, decode="none"), t)
    if ev["status"] == OK and opts.get("skip_ok", True):
        return True, "sudah seragam, dilewati"
    if ev["status"] == BAD:
        return False, "dilewati, file bermasalah: " + ev["notes"]
    if (msg := _disk_ok(src, disk_min_gb)):
        return False, msg
    info = svc.media_info(src)
    if not info["ok"] or not info["v"]:
        return False, f"Tidak ada video yang bisa dibaca: {src.name}"
    dur = info["dur"]
    keep_a = bool(t.keep_audio and info["a"])
    tmp = svc.TMP / f"norm_{uuid.uuid4().hex[:8]}.mp4"
    cmd = [*LOW_PRIO, "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1",
           "-nostats", "-i", str(src), "-map", "0:v:0"]
    if keep_a:
        cmd += ["-map", "0:a:0"]
    cmd += ["-sn", "-dn", "-map_chapters", "-1", *video_encode_args(t, int(opts.get("threads", 2)))]
    if keep_a:
        cmd += ["-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", f"{t.abr_k}k", "-ar", "44100",
                "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        rc, tail = _run_ffmpeg(cmd, dur, job, f"Encode {t.height}p{t.fps}")
        if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return False, f"ffmpeg gagal: {tail.strip()[-300:]}"
        out = svc.media_info(tmp)
        v = out.get("v") or {}
        W, H = t.size
        if not out["ok"] or v.get("codec") != "h264" or (v.get("w"), v.get("h")) != (W, H):
            return False, "Verifikasi hasil gagal (codec/ukuran keluaran tidak sesuai)."
        if abs(out["dur"] - dur) > max(2.0, dur * 0.02):
            return False, f"Verifikasi gagal: durasi berubah {dur:.1f}s → {out['dur']:.1f}s."
        dest = _finalize(tmp, src, ".mp4", opts.get("mode", "new"), protected_fn)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    return True, f"{dest.name} | H.264 {W}x{H} {t.fps}fps {t.bitrate}k" + (" + AAC" if keep_a else " (tanpa audio)")


def submit_normalize(runner, kind: str, paths, t, opts: dict, disk_min_gb: float, protected_fn=None) -> int:
    """Antrekan satu job per file ke JobRunner (1 worker -> berurutan, hemat CPU). Return jumlah job."""
    fn = normalize_audio if kind == "audio" else normalize_video
    label = "Normalisasi audio" if kind == "audio" else "Normalisasi video"
    n = 0
    for p in paths:
        p = Path(p)
        runner.submit(f"{label} · {p.name}"[:60],
                      lambda job, p=p: fn(job, p, t, opts, disk_min_gb, protected_fn))
        n += 1
    return n

import hashlib
import shutil
import time
from urllib.parse import quote
from functools import partial

import os
import secrets

import pandas as pd
import streamlit as st

import mediatools as mt
import services as svc
import uploader

st.set_page_config(page_title="Studio Render & Live", page_icon="📡", layout="wide")


KIND_LABEL = {"video": "🎞️ Video", "audio": "🎵 Audio", "output": "📦 Hasil",
              "thumb": "🖼️ Thumbnail", "other": "🗂️ Lainnya"}
STATUS_ICON = {"antre": "⏳ antre", "berjalan": "🔄 berjalan", "selesai": "✅ selesai",
               "gagal": "❌ gagal", "dibatalkan": "🚫 dibatalkan", "diulang": "🔁 diulang"}
HINT_LABEL = {"auto": "Otomatis (deteksi isi file)", "video": "Video", "audio": "Audio"}


@st.cache_data(ttl=15, show_spinner=False)
def _disk_info() -> dict:
    return svc.disk_info()


@st.cache_resource
def get_runner(name: str, workers: int) -> svc.JobRunner:
    return svc.JobRunner(workers)


@st.cache_resource
def get_stream() -> svc.StreamManager:
    return svc.StreamManager()


@st.cache_resource
def get_upload_server():
    """Server upload resumable di port sendiri (default 8502) + tunnel opsional."""
    port = int(os.environ.get("UPLOAD_PORT", "8502"))
    token = os.environ.get("UPLOAD_TOKEN")
    if not token:
        try:
            token = st.secrets.get("UPLOAD_TOKEN")
        except Exception:  # noqa: BLE001
            token = None
    token = token or secrets.token_urlsafe(18)
    srv = uploader.UploadServer(port, token)
    srv.start()
    return srv, uploader.Tunnel(port)


dl_jobs = get_runner("download", 2)   # 2 unduhan paralel
jobs = get_runner("process", 1)       # render/merge satu per satu (hemat CPU/RAM)
stream = get_stream()

if shutil.which("ffmpeg") is None:
    st.error("ffmpeg tidak ditemukan. Pastikan `packages.txt` berisi `ffmpeg`.")
    st.stop()


def protected_files():
    return list(stream.files) if stream.running else []


def jobs_table(rows, key=""):
    if not rows:
        st.caption("Belum ada job.")
        return
    df = pd.DataFrame(rows)
    df["Status"] = df["Status"].map(lambda s: STATUS_ICON.get(s, s))
    st.dataframe(df, hide_index=True, width="stretch")
    failed = [r for r in rows if r["Status"] == "gagal"]
    if failed:
        with st.expander(f"❌ Detail error ({len(failed)} gagal)", expanded=True):
            for r in failed[:8]:
                st.markdown(f"**{r['Job']}** · `{r['ID']}`")
                st.code(str(r["Info"]) or "(tanpa pesan)", language="text")


# ───────────── sidebar ─────────────
with st.sidebar:
    st.title("📡 Studio")
    di = _disk_info()
    c1, c2 = st.columns(2)
    c1.metric("Disk kosong", f"{di['free_gb']:.1f} GB")
    c2.metric("Data app", f"{di['data_gb']:.2f} GB")
    disk_min = st.number_input("Batas minimum sisa disk (GB)", 0.0, 50.0, 0.5, 0.1, key="disk_min",
                               help="Unduh, render, dan merge dibatalkan jika sisa disk di bawah angka ini.")
    st.caption(f"Antrean — unduh: {dl_jobs.pending()} · render/merge: {jobs.pending()}")
    st.caption("🔴 Live: " + ("berjalan" if stream.running else "mati"))
    if st.button("🧹 Bersihkan folder tmp", width="stretch"):
        if dl_jobs.pending() or jobs.pending():
            st.toast("Tunggu semua job selesai dulu.")
        else:
            svc.clear_tmp()
            st.toast("Folder tmp dibersihkan")
    with st.expander("🔧 Komponen"):
        ts = svc.tool_status()
        st.write(("✅" if ts["ffmpeg"] else "❌") + " ffmpeg")
        st.write(("✅" if ts["deno"] else "⚠️") + " deno (untuk YouTube)")
        st.write(("✅ yt-dlp " + ts["yt-dlp"]) if ts["yt-dlp"] else "❌ yt-dlp")
    st.divider()
    st.caption("⚠️ Penyimpanan Streamlit Cloud tidak permanen. File bisa hilang saat app "
               "restart/redeploy; download hasil penting dari tab Library.")

st.title("Studio Render & Live")

tab_lib, tab_imp, tab_norm, tab_live, tab_playlist, tab_jobs = st.tabs(
    ["📁 Library", "⬇️ Impor", "🧰 Normalisasi & Tes", "🔴 Live 24 Jam",
     "🔁 Multi-stream berurutan", "⚙️ Job"])

# ───────────── LIBRARY ─────────────
with tab_lib:
    counts = {k: len(svc.list_paths(k)) for k in svc.DIRS}
    kind = st.radio("Folder", list(svc.DIRS), horizontal=True, key="lib_kind",
                    format_func=lambda k: f"{KIND_LABEL[k]} ({counts[k]})")
    files = svc.list_files(kind)
    if not files:
        st.info("Folder kosong. Tambahkan file lewat tab **Impor**.")
    else:
        q = st.text_input("🔎 Cari file", key=f"lib_q_{kind}", placeholder="ketik sebagian nama file…")
        shown = [f for f in files if q.lower() in f["name"].lower()] if q else files
        ver_k, all_k = f"tv_{kind}", f"tall_{kind}"
        s1, s2, s3 = st.columns([1, 1, 4])
        if s1.button("☑️ Pilih semua", key=f"sa_{kind}", disabled=not shown, width="stretch"):
            st.session_state[all_k] = True
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        if s2.button("⬜ Kosongkan", key=f"sn_{kind}", width="stretch"):
            st.session_state[all_k] = False
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        s3.caption(f"{len(shown)} file ditampilkan" + (f" dari {len(files)}" if q else "")
                   + " · centang manual per baris, atau pakai Pilih semua.")
        df = pd.DataFrame({"Pilih": bool(st.session_state.get(all_k, False)) if shown else [],
                           "File": [f["name"] for f in shown],
                           "MB": [round(f["mb"], 1) for f in shown],
                           "Diubah": [f["mtime"] for f in shown]})
        # kunci tabel ikut berubah saat daftar file / filter berubah, agar centang tidak bergeser ke baris lain
        sig = hashlib.md5("|".join(f["name"] for f in shown).encode()).hexdigest()[:8]
        tkey = f"tbl_{kind}_{st.session_state.get(ver_k, 0)}_{sig}"
        edited = st.data_editor(df, key=tkey, hide_index=True, width="stretch",
                                disabled=["File", "MB", "Diubah"],
                                column_config={"Pilih": st.column_config.CheckboxColumn("Pilih", width="small")})
        chosen = [svc.DIRS[kind] / n for n in edited.loc[edited["Pilih"], "File"]]
        if chosen:
            tot_mb = sum(f["mb"] for f in shown if f["name"] in {c.name for c in chosen})
            st.caption(f"✔ {len(chosen)} file dipilih · {tot_mb:,.1f} MB")
            a_, b_, c_ = st.columns([1, 1, 1])
            with a_.popover(f"🗑️ Hapus {len(chosen)} file", width="stretch"):
                st.warning(f"{len(chosen)} file akan dihapus permanen.")
                if st.button("Ya, hapus sekarang", type="primary", key=f"lib_del_{kind}"):
                    n = svc.delete_files(chosen, protected=protected_files())
                    st.session_state[all_k] = False
                    st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                    skipped = len(chosen) - n
                    st.toast(f"{n} file dihapus" + (f" · {skipped} dilewati (sedang dipakai live)" if skipped else ""))
                    st.rerun()
            dest = b_.selectbox("Pindahkan ke", [k for k in svc.DIRS if k != kind],
                                format_func=lambda k: KIND_LABEL[k], label_visibility="collapsed")
            if c_.button("📂 Pindahkan", key=f"lib_move_{kind}", width="stretch"):
                n = svc.move_files(chosen, dest, protected=protected_files())
                st.session_state[all_k] = False
                st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                st.toast(f"{n} file dipindahkan ke {KIND_LABEL[dest]}")
                st.rerun()
        with st.expander("Preview & download"):
            paths = svc.list_paths(kind)
            sel = st.selectbox("File", paths, format_func=lambda p: p.name, index=None,
                               placeholder="Pilih file...", key="lib_prev")
            if sel is not None:
                mb = sel.stat().st_size / 1024 / 1024
                _srv, _tn = get_upload_server()
                fq = f"/file?kind={kind}&name={quote(sel.name)}&t={_srv.token}"
                base_url = _tn.url if _tn.running and _tn.url else None
                ext = sel.suffix.lower()
                if ext in svc.AUDIO_EXTS and mb < 200:
                    st.audio(str(sel))
                elif ext in svc.IMAGE_EXTS:
                    st.image(str(sel), width=240)
                elif ext in svc.VIDEO_EXTS and mb < 200:
                    st.video(str(sel))
                elif ext in svc.VIDEO_EXTS | svc.AUDIO_EXTS and base_url:
                    tag = "video" if ext in svc.VIDEO_EXTS else "audio"
                    st.components.v1.html(
                        f'<{tag} controls preload="metadata" style="width:100%;max-height:420px" '
                        f'src="{base_url}{fq}"></{tag}>', height=440 if tag == "video" else 60)
                else:
                    st.caption("Preview file besar butuh tunnel (buka dari tab Impor → Upload file besar).")
                if mb < 300:
                    st.download_button("⬇️ Download ke komputer", sel.read_bytes(),
                                       file_name=sel.name, key="lib_dl")
                elif base_url:
                    st.link_button(f"⬇️ Download {mb:.0f} MB (streaming, bisa dilanjut)",
                                   f"{base_url}{fq}&dl=1")
                else:
                    st.caption(f"File {mb:.0f} MB: buka tunnel di tab Impor dulu agar link download "
                               "streaming muncul (tanpa batas ukuran, tidak memakai RAM).")

# ───────────── IMPOR ─────────────
with tab_imp:
    def _cookie_text() -> str:
        """Cookies dari file upload (prioritas) atau dari kotak teks."""
        ss = st.session_state
        up_file = ss.get(f"ck_up_{ss.get('ck_up_n', 0)}")
        if up_file is not None:
            return up_file.getvalue().decode("utf-8", errors="replace")
        return ss.get("imp_cookies", "")

    def queue_downloads():
        """Callback tombol: antrekan semua link, lalu hapus link & cookies dari form."""
        ss = st.session_state
        urls_ = svc.parse_urls(ss.get("imp_raw", ""))
        if not urls_:
            return
        audio_opts = {"codec": ss.get("au_codec", "mp3"), "bitrate": ss.get("au_bitrate", "192"),
                      "embed": ss.get("au_embed", True), "thumb": ss.get("au_thumb", True)}
        box = None
        ctext = _cookie_text().strip()
        if ctext and any(svc.classify_url(u) == "web" for u in urls_):
            try:
                netscape, _, _ = svc.parse_cookies(ctext)
            except ValueError as e:
                ss["imp_msg"] = ("error", f"Cookies tidak valid: {e}")
                return
            box = svc.CookieBox(netscape)
        runner = get_runner("download", 2)
        for u in urls_:
            use = box if (box is not None and svc.classify_url(u) == "web") else None
            runner.submit(
                svc.short_title(u),
                partial(svc.run_download, url=u, hint=ss.get("imp_hint", "auto"),
                        height=ss.get("imp_height", 720), noplaylist=ss.get("imp_noplaylist", True),
                        disk_min_gb=ss.get("disk_min", 0.5), audio_opts=audio_opts, cookies=use),
                cleanup=use.make_releaser() if use is not None else None)
        ss["imp_raw"] = ""
        ss["imp_cookies"] = ""
        ss["ck_up_n"] = ss.get("ck_up_n", 0) + 1     # reset uploader cookies
        ss["imp_msg"] = ("success", f"{len(urls_)} unduhan diantrekan"
                                    + (" (dengan cookies)" if box is not None else ""))

    with st.container(border=True):
        st.subheader("🔗 Impor dari link")
        st.caption("Tempel banyak link sekaligus, dipisah koma, spasi, atau baris baru. "
                   "Jenis link dikenali otomatis: Google Drive (file/folder), YouTube dan situs "
                   "lain yang didukung yt-dlp, atau link langsung ke file.")
        flash = st.session_state.pop("imp_msg", None)
        if flash:
            (st.success if flash[0] == "success" else st.error)(flash[1])
        raw = st.text_area("Link", height=150, key="imp_raw", label_visibility="collapsed",
                           placeholder="https://drive.google.com/file/d/.../view, https://youtu.be/...")
        urls = svc.parse_urls(raw)
        o1, o2, o3 = st.columns(3)
        hint = o1.radio("Simpan sebagai", list(HINT_LABEL), format_func=HINT_LABEL.get, key="imp_hint",
                        help="Otomatis memeriksa isi file dengan ffprobe lalu memasukkannya ke folder "
                             "yang tepat. Pilih Audio untuk mengambil audio (MP3/M4A) dari link YouTube.")
        o2.selectbox("Maks. tinggi video (yt-dlp)", [360, 480, 720, 1080], index=2, key="imp_height",
                     disabled=hint == "audio")
        o3.checkbox("Abaikan playlist (1 video saja)", value=True, key="imp_noplaylist",
                    help="Matikan untuk mengunduh seluruh playlist dari link playlist.")

        with st.expander("🎧 Opsi audio (MP3 / M4A, cover, thumbnail)", expanded=(hint == "audio")):
            a1, a2 = st.columns(2)
            a1.selectbox("Format", ["mp3", "m4a"], key="au_codec",
                         format_func=lambda c: {"mp3": "MP3", "m4a": "M4A (AAC)"}[c])
            a2.selectbox("Bitrate (kbps)", ["320", "256", "192", "128"], index=2, key="au_bitrate")
            st.checkbox("Sematkan cover & metadata ke dalam file audio", value=True, key="au_embed")
            st.checkbox("Simpan thumbnail .jpg terpisah (folder Thumbnail)", value=True, key="au_thumb")
            st.caption("Berlaku saat 'Simpan sebagai' = Audio. Bitrate di atas kualitas asli YouTube tidak "
                       "menambah kualitas; 192 kbps cukup untuk kebanyakan kasus. Jika cover/thumbnail "
                       "gagal diproses, unduhan otomatis diulang dengan opsi lebih sederhana.")

        with st.expander("🍪 Cookies untuk yt-dlp (opsional)"):
            st.caption("Untuk video yang meminta login atau error \"not a bot\". Login ke YouTube di browser → "
                       "ekstensi Cookie-Editor → Export → JSON → upload file atau tempel di bawah. "
                       "Dipakai untuk semua link web/yt-dlp dalam satu batch (bulk), bukan untuk Google Drive.")
            ck_n = st.session_state.setdefault("ck_up_n", 0)
            st.file_uploader("Upload cookies (.json / .txt)", type=["json", "txt"], key=f"ck_up_{ck_n}")
            st.text_area("Atau tempel cookies JSON", height=120, key="imp_cookies",
                         placeholder="[{\"domain\": \".youtube.com\", \"name\": \"...\", \"value\": \"...\"}, ...]")
            ctext = _cookie_text().strip()
            if ctext:
                try:
                    _, n_cookie, doms = svc.parse_cookies(ctext)
                    st.success(f"{n_cookie} cookie terbaca · domain: {', '.join(doms[:6])}"
                               + (" …" if len(doms) > 6 else ""))
                except ValueError as e:
                    st.error(f"Cookies tidak valid: {e}")
            st.warning("Cookies = akses ke akunmu. Jangan dibagikan atau di-screenshot. Cookies hanya "
                       "disimpan di memori server selama batch berjalan, ditulis ke file sementara "
                       "per unduhan, lalu dihapus. Form dikosongkan otomatis setelah diantrekan. "
                       "Pakai akun cadangan dan jaga agar app ini privat.")

        if urls:
            st.dataframe(
                pd.DataFrame({"No": range(1, len(urls) + 1),
                              "Sumber": [svc.SOURCE_LABEL[svc.classify_url(u)] for u in urls],
                              "Link": [u[:90] for u in urls]}),
                hide_index=True, width="stretch")
        st.button(f"⬇️ Mulai unduh {len(urls)} link", type="primary", disabled=not urls,
                  key="imp_go", on_click=queue_downloads)

    @st.fragment(run_every=2)
    def dl_progress():
        c = dl_jobs.counts()
        total = sum(c.values())
        if not total:
            return
        done = c["selesai"] + c["gagal"]
        st.progress(done / total, text=f"{done}/{total} selesai · {c['berjalan']} berjalan · "
                                       f"{c['antre']} antre · {c['gagal']} gagal")
        jobs_table(dl_jobs.snapshot()[:30])

    dl_progress()
    r1, r2, r3 = st.columns(3)
    if r1.button("🔁 Coba ulang yang gagal", key="dl_retry"):
        st.toast(f"{dl_jobs.retry_failed()} unduhan diulang")
    if r2.button("🚫 Batalkan yang antre", key="dl_cancel"):
        st.toast(f"{dl_jobs.cancel_queued()} dibatalkan")
    if r3.button("🧽 Bersihkan riwayat", key="dl_clear"):
        dl_jobs.clear_finished()
        st.rerun()

    with st.container(border=True):
        st.subheader("📥 Upload dari komputer")
        up_n = st.session_state.setdefault("up_n", 0)
        for msg in st.session_state.pop("up_msg", []):
            st.success(msg)
        up = st.file_uploader("Pilih file apa saja (video, audio, dll.)", accept_multiple_files=True,
                              key=f"imp_up_{up_n}")
        if up and st.button(f"💾 Simpan {len(up)} file (mode: {HINT_LABEL[hint]})", key="up_go"):
            msgs = []
            for f in up:
                k, dest = svc.import_bytes(f.name, f.getbuffer(), hint)
                msgs.append(f"{dest.name} → {KIND_LABEL[k]}")
            st.session_state["up_msg"] = msgs
            st.session_state["up_n"] = up_n + 1
            st.rerun()
        st.caption("Upload lewat tombol di atas dibatasi (default 200 MB per file). Untuk file besar, "
                   "pakai upload resumable di bawah atau link Google Drive.")

    with st.expander("🚀 Upload file besar (pilih file di browser, resumable)", expanded=True):
        up_srv, up_tunnel = get_upload_server()
        if up_srv.error:
            st.error(up_srv.error)
        else:
            st.markdown("File besar dikirim per potongan langsung ke folder internal app (tanpa batas 200 MB). "
                        "Jika koneksi putus, pilih file yang sama lagi — upload **lanjut** dari bagian terakhir.")
            st.markdown("Streamlit Cloud hanya membuka satu port, jadi halaman upload dibuka lewat tunnel "
                        "publik (cloudflared, tanpa akun).")
            t1, t2 = st.columns(2)
            if t1.button("🌐 Buka tunnel upload", key="tn_open", disabled=up_tunnel.running):
                up_tunnel.start()
            if t2.button("⛔ Tutup tunnel", key="tn_close", disabled=not up_tunnel.running):
                up_tunnel.stop()

            @st.fragment(run_every=3)
            def tunnel_panel():
                if up_tunnel.error:
                    st.error(up_tunnel.error)
                if up_tunnel.running and not up_tunnel.url:
                    st.info("Menyiapkan tunnel... (beberapa detik)")
                link = f"{up_tunnel.url}/#t={up_srv.token}" if up_tunnel.url else None
                if link:
                    st.link_button("📂 Buka halaman upload (tab baru)", link, type="primary")
                    st.caption("Link ini sudah berisi token — jangan dibagikan. Di HP/komputer lain cukup "
                               "buka link yang sama.")
                else:
                    st.caption(f"Tanpa tunnel (VPS/lokal): buka `http://IP-SERVER:{up_srv.port}/#t=TOKEN`.")
                    st.caption("Token:")
                    st.code(up_srv.token, language="text")
                parts = up_srv.partials()
                if parts:
                    st.caption("Upload terjeda/sedang jalan: " + ", ".join(
                        f"{x['name']} ({x['mb']:.0f} MB)" for x in parts))

            tunnel_panel()
            if up_tunnel.url:
                st.components.v1.iframe(f"{up_tunnel.url}/#t={up_srv.token}", height=520, scrolling=True)

# ───────────── NORMALISASI & TES ─────────────
LMODE = {"two": "Presisi (2-pass, disarankan)", "one": "Cepat (1-pass)",
         "off": "Tanpa loudness (hanya samakan format)"}
OUTMODE = {"new": "File baru (*_norm), aslinya dipertahankan", "replace": "Ganti file asli (setelah lolos verifikasi)"}
DECMODE = {"none": "Tidak", "quick": "Cepat (60 dtk awal + akhir)", "full": "Penuh (lambat, paling teliti)"}


def _pick_files(prefix: str, paths: list, noun: str) -> list:
    if st.checkbox(f"Semua {noun} ({len(paths)})", value=True, key=f"mt_{prefix}_all"):
        return list(paths)
    return st.multiselect(f"Pilih {noun}", paths, format_func=svc.label, key=f"mt_{prefix}_sel")


def _report(rows: list, kind: str, key: str, tgt_audio=None) -> dict:
    S = mt.summarize(rows, kind, tgt_audio)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Diuji", S["total"])
    c2.metric("✅ Seragam & aman", S["ok"])
    c3.metric("⚠️ Tidak seragam", S["warn"])
    c4.metric("❌ Bermasalah", S["bad"])
    if S["uniform"]:
        st.success("Semua file seragam dan aman dipakai.")
    elif S["bad"]:
        st.error(f"{S['bad']} file bermasalah (rusak / terpotong / senyap). Jangan dipakai live: ganti atau hapus.")
    else:
        st.warning(f"{S['warn']} file belum seragam. Jalankan normalisasi di bawah.")
    for n in S["notes"]:
        st.info(n)
    if S["groups"]:
        st.caption("Format terdeteksi: " + " · ".join(f"{g} (×{n})" for g, n in S["groups"]))
    only = st.checkbox("Tampilkan hanya yang tidak OK", value=S["total"] > 8, key=f"mt_{key}_only")
    df = pd.DataFrame(rows)
    if only:
        df = df[df["Status"] != mt.OK]
    if len(df):
        st.dataframe(df.drop(columns=["_path"]), hide_index=True, width="stretch")
    return S


with tab_norm:
    st.caption("Samakan format semua file agar live stabil dan volume tidak naik-turun: audio → AAC .m4a "
               "(sample rate, channel, bitrate, loudness seragam), video → H.264 dengan ukuran & FPS seragam. "
               "Alur: **Tes** dulu untuk melihat mana yang tidak seragam, lalu **Normalisasi**. Hasil ditulis ke "
               "file sementara, diverifikasi, baru masuk Library.")
    if stream.running:
        st.warning("Live sedang berjalan. Tes & normalisasi berjalan dengan prioritas rendah, tetapi tetap memakai "
                   "CPU; untuk live yang paling aman, lakukan saat live mati atau batasi thread di bawah.")
    n_aud, n_vid = st.tabs(["🎵 Audio (AAC / M4A)", "🎞️ Video (H.264 seragam)"])

    # ---------------- AUDIO ----------------
    with n_aud:
        apaths = svc.list_paths("audio")
        if not apaths:
            st.info("Folder audio kosong. Tambahkan file lewat tab **Impor**.")
        else:
            xf_now = float(st.session_state.get("l_xfmax", 8.0)) if st.session_state.get("l_xfon", True) else 0.0
            with st.expander("🎯 Target audio seragam", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                a_sr = c1.selectbox("Sample rate (Hz)", [44100, 48000], key="mt_a_sr",
                                    help="44100 = sama dengan jalur live (tanpa resample tambahan).")
                a_ch = c2.selectbox("Channel", [2, 1], key="mt_a_ch", format_func={2: "Stereo", 1: "Mono"}.get)
                a_br = c3.selectbox("Bitrate AAC (kbps)", [128, 160, 192, 256], index=1, key="mt_a_br")
                a_lufs = c4.number_input("Loudness target (LUFS)", -30.0, -8.0, -16.0, 0.5, key="mt_a_lufs",
                                         help="-16 LUFS = umum untuk musik streaming/YouTube. Semua lagu dibawa ke "
                                              "angka ini sehingga volume antar lagu sama.")
                d1, d2 = st.columns(2)
                a_tol = d1.number_input("Toleransi selisih loudness (LU)", 0.5, 6.0, 2.0, 0.5, key="mt_a_tol")
                a_tp = d2.number_input("Batas true-peak (dBTP)", -6.0, -0.1, -1.5, 0.1, key="mt_a_tp",
                                       help="Puncak sinyal maksimum; -1.5 memberi ruang agar tidak clipping saat di-mix.")
            tgt_a = mt.AudioTarget(sr=a_sr, ch=a_ch, bitrate_k=a_br, lufs=a_lufs, tp=a_tp, tol_lu=a_tol,
                                   min_dur=3 * xf_now)

            st.markdown("##### 🧪 Tes audio")
            sel_a = _pick_files("a", apaths, "file audio")
            t1, t2 = st.columns(2)
            do_loud = t1.checkbox("Ukur loudness (LUFS & puncak)", value=True, key="mt_a_loud")
            do_dec = t2.checkbox("Tes decode penuh (deteksi file rusak / terpotong)", value=True, key="mt_a_dec")
            if st.button(f"🧪 Tes {len(sel_a)} file audio", type="primary", key="mt_a_test", disabled=not sel_a):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_a_rows"] = mt.run_tests(
                    sel_a, lambda p: mt.audio_row(p, tgt_a, do_loud, do_dec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_a = st.session_state.get("mt_a_rows") or []
            if rows_a:
                _report(rows_a, "audio", "a", tgt_a)
                st.caption("Hasil tes dihitung pada target saat tes dijalankan. Setelah normalisasi selesai, "
                           "jalankan tes lagi untuk memastikan.")

            st.markdown("##### 🔧 Normalisasi audio")
            n1, n2 = st.columns(2)
            a_lmode = n1.selectbox("Loudness", list(LMODE), format_func=LMODE.get, key="mt_a_lmode")
            a_out = n2.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_a_out")
            n3, n4 = st.columns(2)
            a_skip = n3.checkbox("Lewati file yang sudah seragam", value=True, key="mt_a_skip")
            a_strip = n4.checkbox("Buang metadata & cover", value=False, key="mt_a_strip")
            fix_a = [r["_path"] for r in rows_a if r["Status"] == mt.WARN]

            def _submit_a(paths: list) -> None:
                opts = {"mode": a_out, "loud": a_lmode, "skip_ok": a_skip, "strip_meta": a_strip}
                n = mt.submit_normalize(jobs, "audio", paths, tgt_a, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi audio diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_a)} file yang tidak seragam", type="primary",
                         key="mt_a_fix", disabled=not fix_a, width="stretch"):
                _submit_a(fix_a)
            if b2.button(f"🔧 Normalisasi semua {len(sel_a)} file terpilih", key="mt_a_fixall",
                         disabled=not sel_a, width="stretch"):
                _submit_a(sel_a)

    # ---------------- VIDEO ----------------
    with n_vid:
        vpaths = svc.library_videos()
        if not vpaths:
            st.info("Belum ada video. Tambahkan file lewat tab **Impor**.")
        else:
            with st.expander("🎯 Target video seragam", expanded=True):
                c1, c2, c3 = st.columns(3)
                v_h = c1.selectbox("Resolusi", [480, 720, 1080], index=1, key="mt_v_h",
                                   format_func=lambda h: f"{h}p ({svc.RES[h][0]}x{svc.RES[h][1]})")
                v_fps = c2.selectbox("FPS", [24, 25, 30], index=2, key="mt_v_fps")
                v_br = c3.selectbox("Bitrate video (kbps)", [0, 1500, 2500, 4000, 6000], key="mt_v_br",
                                    format_func=lambda b: "Auto (sesuai resolusi)" if b == 0 else str(b))
                d1, d2, d3 = st.columns(3)
                v_aud = d1.radio("Audio di dalam video", ["keep", "drop"], horizontal=True, key="mt_v_aud",
                                 format_func={"keep": "Pertahankan (AAC)", "drop": "Hapus"}.get,
                                 help="Live memakai audio dari panel audio, jadi audio video tidak terpakai. "
                                      "Menghapusnya menghemat disk.")
                v_pre = d2.selectbox("Kecepatan encode", ["superfast", "veryfast", "faster", "medium"], index=1,
                                     key="mt_v_pre", help="Lebih lambat = file lebih kecil / kualitas lebih baik.")
                v_thr = d3.selectbox("Thread CPU", [0, 1, 2, 4], index=2, key="mt_v_thr",
                                     format_func=lambda n: "Otomatis" if n == 0 else str(n),
                                     help="Batasi agar live yang sedang berjalan tidak tersendat.")
            tgt_v = mt.VideoTarget(height=v_h, fps=v_fps, vbr_k=v_br, keep_audio=v_aud == "keep", preset=v_pre)
            st.caption(f"Target: H.264 High · yuv420p · {tgt_v.size[0]}x{tgt_v.size[1]} · {v_fps} fps · "
                       f"{tgt_v.bitrate} kbps · keyframe tiap 2 detik → langsung bisa dikirim tanpa re-encode.")

            st.markdown("##### 🧪 Tes video")
            sel_v = _pick_files("v", vpaths, "video")
            t1, t2 = st.columns(2)
            do_gop = t1.checkbox("Ukur jarak keyframe", value=True, key="mt_v_gop",
                                 help="YouTube Live butuh keyframe ≤ 4 detik.")
            do_vdec = t2.selectbox("Tes decode (deteksi file rusak / terpotong)", list(DECMODE), index=1,
                                   format_func=DECMODE.get, key="mt_v_dec")
            if st.button(f"🧪 Tes {len(sel_v)} file video", type="primary", key="mt_v_test", disabled=not sel_v):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_v_rows"] = mt.run_tests(
                    sel_v, lambda p: mt.video_row(p, tgt_v, do_gop, do_vdec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_v = st.session_state.get("mt_v_rows") or []
            if rows_v:
                S_v = _report(rows_v, "video", "v")
                st.caption(f"{S_v['ok']} dari {S_v['total']} video siap dikirim apa adanya saat live (CPU ≈ 0). "
                           "Hasil tes dihitung pada target saat tes dijalankan.")

            st.markdown("##### 🔧 Normalisasi video")
            v_out = st.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_v_out")
            v_skip = st.checkbox("Lewati file yang sudah seragam", value=True, key="mt_v_skip")
            fix_v = [r["_path"] for r in rows_v if r["Status"] == mt.WARN]

            def _submit_v(paths: list) -> None:
                opts = {"mode": v_out, "skip_ok": v_skip, "threads": v_thr}
                n = mt.submit_normalize(jobs, "video", paths, tgt_v, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi video diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_v)} video yang tidak seragam", type="primary",
                         key="mt_v_fix", disabled=not fix_v, width="stretch"):
                _submit_v(fix_v)
            if b2.button(f"🔧 Normalisasi semua {len(sel_v)} video terpilih", key="mt_v_fixall",
                         disabled=not sel_v, width="stretch"):
                _submit_v(sel_v)

    @st.fragment(run_every=3)
    def norm_status():
        rows_ = [r for r in jobs.snapshot() if str(r["Job"]).startswith("Normalisasi")]
        if rows_:
            st.markdown("##### 📋 Antrean normalisasi")
            jobs_table(rows_[:30])
            st.caption("Diproses satu per satu. Job yang berjalan tidak bisa dibatalkan; yang antre bisa "
                       "dibatalkan dari tab Job.")

    norm_status()

# ───────────── LIVE ─────────────
def _mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


@st.cache_data(show_spinner=False)
def _dur(path_str: str, mtime: float) -> float:
    return svc.media_info(path_str)["dur"]


VOL_DEFAULT = {"l_vrand": 1.0}


def _push_vol():
    """Teruskan posisi slider ke stream yang sedang berjalan (tanpa restart)."""
    if "l_vrand" in st.session_state:
        stream.set_volume("r", st.session_state["l_vrand"])


def _reset_vol():
    st.session_state.update(VOL_DEFAULT)
    _push_vol()


with tab_live:
    program_mode = st.checkbox(
        "Mode terjadwal: opening → audio → video end → hentikan live",
        value=False,
        key="l_program_mode",
        help="Mode live lama tetap tersedia. Mode terjadwal memutar audio penuh atau sesuai target, "
             "lalu menunggu video end selesai sebelum menghentikan FFmpeg dan koneksi YouTube.",
    )
    st.caption(
        "Mode terjadwal: opening opsional dipilih acak dan diputar sekali → video utama acak selama audio → "
        "video end diputar penuh → FFmpeg dan pengiriman ke YouTube dihentikan."
        if program_mode else
        "Streaming tunggal ke YouTube Live langsung dari Library. Mode live lama tetap memutar opening "
        "opsional sekali, video acak berulang, serta audio random kontinu secara independen."
    )
    vlib = svc.library_videos()
    alib = svc.list_paths("audio")

    st.markdown("#### 🎞️ Alur video")
    use_opening = st.checkbox("Putar opening sekali saat live dimulai", value=True, key="l_open_on")
    program_video_choice = "Video acak (shuffle)"
    program_video_mode = "random"
    if program_mode:
        program_video_choice = st.radio(
            "Video selama audio",
            ["Video acak (shuffle)", "Satu video diulang"],
            horizontal=True,
            key="l_program_video_mode",
        )
        program_video_mode = "single" if program_video_choice == "Satu video diulang" else "random"
    f1, f2 = st.columns(2)
    v_open = f1.multiselect("1️⃣ Opening (acak jika memilih beberapa)", vlib, format_func=svc.label,
                            key="l_open", placeholder="Pilih video opening...", disabled=not use_opening)
    if program_mode and program_video_mode == "single":
        single_video = f2.selectbox(
            "2️⃣ Pilih satu video utama",
            vlib,
            format_func=svc.label,
            key="l_single_video",
            index=None,
            placeholder="Pilih satu video untuk diulang...",
            disabled=not vlib,
        )
        v_rand = [single_video] if single_video else []
    else:
        main_video_label = ("2️⃣ Video utama acak (selama audio)" if program_mode
                            else "2️⃣ Video acak (diputar penuh dan berulang)")
        v_rand = f2.multiselect(main_video_label, vlib, format_func=svc.label,
                                key="l_rand", placeholder="Pilih video acak...")
    active_opening = v_open if use_opening else []
    v_end = st.multiselect(
        "3️⃣ Video end (acak jika memilih beberapa; diputar penuh)",
        vlib,
        format_func=svc.label,
        key="l_end",
        placeholder="Pilih video end...",
    )
    if program_mode:
        main_video_caption = (
            "Satu video utama diulang sampai audio selesai."
            if program_video_mode == "single"
            else "Video utama dipilih acak dan diputar penuh selama audio."
        )
        st.caption("Opening dipilih acak dan diputar sekali. " + main_video_caption
                   + " Video end diputar penuh setelahnya, lalu live dihentikan.")
    else:
        st.caption("Opening boleh kosong atau dimatikan; live langsung memutar video acak. "
                   "Setelah opening selesai, video acak terus berjalan. Pilih lebih dari satu opening "
                   "agar video awal dipilih acak. Video end di atas digunakan jika mode terjadwal diaktifkan.")

    st.markdown("#### 🎵 Audio terjadwal" if program_mode else "#### 🎵 Audio random kontinu")
    a_rand = st.multiselect(
        "Pilih file audio" if program_mode else "🎲 Pilih file audio untuk diputar acak terus-menerus",
        alib,
        format_func=svc.label,
        key="l_arand",
        placeholder="Pilih satu atau beberapa file audio...",
    )
    selected_audio_seconds = sum(_dur(str(p), p.stat().st_mtime) for p in a_rand)
    program_audio_mode = "full"
    program_audio_seconds = selected_audio_seconds
    if program_mode:
        audio_mode_label = st.radio(
            "Cara memutar audio",
            ["Urutan pilihan", "Acak urutan semua file", "Gunakan target durasi"],
            horizontal=True,
            key="l_program_audio_mode",
        )
        program_audio_mode = {
            "Urutan pilihan": "full",
            "Acak urutan semua file": "shuffle",
            "Gunakan target durasi": "target",
        }[audio_mode_label]
        if program_audio_mode == "target":
            program_audio_hours = st.number_input(
                "Target durasi audio (jam)",
                min_value=0.05,
                max_value=720.0,
                value=1.0,
                step=0.25,
                key="l_program_audio_hours",
                help="Audio diputar acak sampai target; file terakhir dapat terpotong tepat pada batas durasi.",
            )
            program_audio_seconds = float(program_audio_hours) * 3600
        st.caption(
            f"Total durasi audio terpilih: {_mmss(selected_audio_seconds)}. "
            + ("Semua file diputar satu kali sesuai urutan pilihan."
               if program_audio_mode == "full"
               else "Semua file diputar satu kali dengan urutan acak."
               if program_audio_mode == "shuffle"
               else f"Audio diputar acak sampai target {_mmss(program_audio_seconds)}.")
        )
    else:
        st.caption("Setiap file diputar sampai selesai. Jika memilih beberapa file, urutannya diacak tanpa lagu "
                   "yang sama berturut-turut; setelah semua file diputar, urutan diacak kembali. Satu file akan diputar ulang.")

    st.markdown("##### 🎚️ Volume (bisa diubah saat live berjalan, langsung terdengar)")
    for _k, _v in VOL_DEFAULT.items():
        st.session_state.setdefault(_k, _v)
    h1, h2 = st.columns([3, 1.4])
    vol_rand = h1.slider("Volume audio random", 0.0, 2.0, step=0.05, key="l_vrand", on_change=_push_vol)
    h2.button("↩️ Default", key="l_vreset", on_click=_reset_vol, width="stretch",
              help="Kembalikan volume audio random ke 1,00.")

    active_end = v_end if program_mode else []
    all_v = list(dict.fromkeys([*active_opening, *v_rand, *active_end]))
    all_a = list(dict.fromkeys(a_rand))
    if all_v or all_a:
        rt = sum(_dur(str(p), p.stat().st_mtime) for p in v_rand)
        rat = sum(_dur(str(p), p.stat().st_mtime) for p in a_rand)
        opening_label = f"Opening ({len(active_opening)}) → " if active_opening else ""
        if program_mode:
            video_summary = ("1 video diulang" if program_video_mode == "single"
                             else f"{len(v_rand)} video acak")
            st.info(f"Alur terjadwal: {opening_label}{video_summary} → "
                    f"audio {_mmss(program_audio_seconds)} → video end ({len(v_end)} pilihan)")
        else:
            st.info(f"Alur: {opening_label}Video acak ({len(v_rand)} file · "
                    f"{_mmss(rt)} per putaran)   |   Audio random: {len(a_rand)} file · {_mmss(rat)}")

    RTMP_PRESETS = {"RTMP YouTube (standar)": "rtmp://a.rtmp.youtube.com/live2",
                    "RTMPS YouTube (terenkripsi, lebih tahan putus)": "rtmps://a.rtmps.youtube.com:443/live2",
                    "Kustom": ""}
    l0, l1, l2 = st.columns([1.2, 1.2, 1])
    srv = l0.selectbox("Server tujuan", list(RTMP_PRESETS), key="l_srv")
    rtmp = l1.text_input("RTMP URL", RTMP_PRESETS[srv] or "rtmp://", key=f"l_rtmp_{srv}",
                         disabled=srv != "Kustom")
    skey = l2.text_input("Stream Key", type="password", key="l_key")

    with st.expander("⚙️ Pengaturan lanjutan"):
        vmode = st.radio("Mode video", ["auto", "normalize", "copy"], horizontal=True, key="l_vmode",
                         format_func={"auto": "Otomatis (disarankan)",
                                      "normalize": "Selalu samakan",
                                      "copy": "Kirim apa adanya"}.get,
                         help="Otomatis: jika semua video sudah seragam & cocok untuk live, dikirim apa adanya "
                              "(CPU ≈ 0). Jika tidak, tiap video disamakan SEKALI ke H.264 seragam (disimpan di cache), "
                              "lalu stream tetap tanpa re-encode. 'Kirim apa adanya' hanya jika kamu yakin file seragam.")
        n1, n2, n3 = st.columns(3)
        l_h = n1.selectbox("Resolusi (jika disamakan)", [480, 720, 1080], index=1)
        l_fps = n2.selectbox("FPS (jika disamakan)", [24, 25, 30], index=2)
        l_br = n3.selectbox("Bitrate video kbps (jika disamakan)", [0, 1500, 2500, 4000, 6000], index=0,
                            format_func=lambda b: "Auto (sesuai resolusi, disarankan)" if b == 0 else str(b),
                            help="Auto: 480p≈1500 · 720p≈3000 · 1080p≈5000 kbps (×1.4 jika FPS di atas 30). "
                                 "Otomatis diturunkan bertahap jika jaringan upload lambat / sesi sering putus.")
        o1, o2 = st.columns(2)
        l_shuf = o1.checkbox("Acak video pada tiap pergantian", value=True, key="l_shuf",
                             help="Video berikutnya dipilih acak tanpa pengulangan berurutan.")
        l_hours = o2.number_input("Batas durasi (jam, 0 = tanpa batas)", 0.0, 720.0, 0.0, 1.0,
                                  key="l_hours", disabled=program_mode)
        if program_mode:
            o2.caption("Mode terjadwal berhenti otomatis setelah video end.")
        if st.button("🧹 Hapus cache hasil penyamaan", key="l_clrcache"):
            st.toast(f"{svc.clear_live_cache()} file cache dihapus")

    s1, s2 = st.columns(2)
    if s1.button("▶️ Mulai Live", type="primary", disabled=stream.running, width="stretch"):
        if not v_rand:
            st.error("Pilih minimal satu video utama untuk visual selama audio." if program_mode
                     else "Pilih minimal 1 video acak. Opening bersifat opsional.")
        elif program_mode and not v_end:
            st.error("Pilih minimal satu video end untuk mode terjadwal.")
        elif not a_rand:
            st.error("Pilih minimal satu file audio.")
        elif not skey.strip():
            st.error("Stream key wajib diisi.")
        else:
            stream.start({"videos": all_v, "audios": all_a,
                          "v_open": active_opening, "v_rand": v_rand,
                          "v_end": v_end, "program_mode": program_mode,
                          "program_video_mode": program_video_mode,
                          "program_audio_mode": program_audio_mode,
                          "program_audio_seconds": program_audio_seconds,
                          "a_loop": [], "a_rand": a_rand,
                          "vol_rand": vol_rand,
                          "rtmp_url": rtmp.strip(), "stream_key": skey.strip(),
                          "vmode": vmode, "height": l_h, "fps": l_fps, "vbr_k": l_br,
                          "shuffle": l_shuf, "disk_min_gb": disk_min},
                         0 if program_mode else l_hours)
            st.rerun()
    if s2.button("⏹️ Stop Live", disabled=not stream.running, width="stretch"):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def live_status():
        icon = {"live": "🔴 LIVE berjalan", "menyiapkan": "🛠️ Menyiapkan file...",
                "menghubungkan": "🔌 Menghubungkan ke YouTube...", "menyambung ulang": "🔁 Menyambung ulang...",
                "selesai": "✅ Selesai", "error": "❌ Gagal"}.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.warning)(
            icon + (f" — {stream.detail}" if stream.detail else ""))
        a, b, c, d, d2 = st.columns(5)
        a.metric("Uptime", stream.uptime())
        b.metric("Auto-restart", stream.restarts)
        pr = stream.progress or {}
        c.metric("Kecepatan", pr.get("speed", "-") or "-")
        d.metric("Bitrate", (pr.get("bitrate", "-") or "-").strip())
        d2.metric("Kualitas kirim", stream.health if stream.running else "-")
        if stream.running:
            e, f2_, g = st.columns(3)
            e.metric("Video diputar", (stream.now_video or "-")[:28])
            f2_.metric("Audio diputar" if program_mode else "Audio random",
                       (stream.now_audio or "-")[:28])
            g.metric("Pergantian video", stream.switches)
            if stream.prep_info:
                st.caption("🛠️ " + stream.prep_info)
        for n in stream.notes:
            st.caption("• " + n)
        with st.expander("Log ffmpeg"):
            st.code(stream.log_tail() or "(kosong)", language="text")

    live_status()

# ───────────── JADWAL MULTI-STREAM ─────────────
with tab_playlist:
    st.markdown("### Multi-stream berurutan")
    st.caption(
        "Tekan mulai untuk menjalankan Stream 1 sekarang. Semua audio diputar penuh satu kali, lalu video end "
        "diputar penuh; setelah itu koneksi FFmpeg ditutup dan stream berikutnya langsung dimulai. "
        "Setiap stream memakai pilihan media dan Stream Key sendiri."
    )
    st.info(
        "Gunakan **YouTube Stream Key** dari Live Control Room, bukan Google API key. "
        "Stream Key disimpan sebagai input tersembunyi dan disamarkan pada log."
    )

    st.session_state.setdefault("ms_count", 2)
    add_col, remove_col, count_col = st.columns([1, 1, 2])
    if add_col.button("＋ Tambah stream", key="ms_add",
                      disabled=st.session_state["ms_count"] >= 12):
        st.session_state["ms_count"] += 1
        st.rerun()
    if remove_col.button("− Hapus stream terakhir", key="ms_remove",
                         disabled=st.session_state["ms_count"] <= 2):
        removed = st.session_state["ms_count"]
        for key in list(st.session_state):
            if key.startswith(f"ms_{removed}_"):
                del st.session_state[key]
        st.session_state["ms_count"] -= 1
        st.rerun()
    count_col.caption(f"{st.session_state['ms_count']} stream terdaftar · maksimal 12")

    vlib = svc.library_videos()
    alib = svc.list_paths("audio")
    if not vlib:
        st.warning("Library video masih kosong. Tambahkan video di tab Impor sebelum membuat jadwal.")
    if not alib:
        st.warning("Library audio masih kosong. Tambahkan audio di tab Impor sebelum membuat jadwal.")

    playlist_configs = []
    playlist_errors = []
    playlist_rtmp_presets = {
        "RTMP YouTube (standar)": "rtmp://a.rtmp.youtube.com/live2",
        "RTMPS YouTube (terenkripsi)": "rtmps://a.rtmps.youtube.com:443/live2",
        "Kustom": "",
    }
    for stream_no in range(1, st.session_state["ms_count"] + 1):
        prefix = f"ms_{stream_no}_"
        with st.expander(f"Stream {stream_no} — pengaturan video, audio, dan tujuan", expanded=stream_no == 1):
            stream_name = st.text_input(
                "Nama stream", value=f"Stream {stream_no}", key=prefix + "name"
            ).strip() or f"Stream {stream_no}"

            opening_on = st.checkbox(
                "Putar opening opsional satu kali", value=True, key=prefix + "opening_on"
            )
            opening_videos = st.multiselect(
                "Video opening (acak jika memilih beberapa)",
                vlib,
                format_func=svc.label,
                key=prefix + "opening",
                disabled=not opening_on,
                placeholder="Opsional — pilih video opening",
            )

            video_mode_label = st.radio(
                "Video utama selama audio",
                ["Acak beberapa video", "Ulangi satu video"],
                horizontal=True,
                key=prefix + "video_mode",
            )
            if video_mode_label == "Ulangi satu video":
                single_video = st.selectbox(
                    "Pilih video utama",
                    vlib,
                    index=None,
                    format_func=svc.label,
                    key=prefix + "single_video",
                    placeholder="Pilih satu video...",
                    disabled=not vlib,
                )
                main_videos = [single_video] if single_video else []
                program_video_mode = "single"
            else:
                main_videos = st.multiselect(
                    "Video utama (diputar penuh dan diacak)",
                    vlib,
                    format_func=svc.label,
                    key=prefix + "main_videos",
                    placeholder="Pilih satu atau beberapa video...",
                )
                program_video_mode = "random"

            end_videos = st.multiselect(
                "Video end (diputar penuh sebelum berpindah)",
                vlib,
                format_func=svc.label,
                key=prefix + "end_videos",
                placeholder="Pilih minimal satu video end...",
            )

            audio_files = st.multiselect(
                "Audio untuk stream ini",
                alib,
                format_func=svc.label,
                key=prefix + "audio",
                placeholder="Pilih satu atau beberapa file audio...",
            )
            selected_audio_seconds = sum(
                _dur(str(path), path.stat().st_mtime) for path in audio_files
            )
            audio_mode_label = st.radio(
                "Urutan audio",
                ["Urutan pilihan", "Acak satu kali"],
                horizontal=True,
                key=prefix + "audio_mode",
            )
            program_audio_mode = {
                "Urutan pilihan": "full",
                "Acak satu kali": "shuffle",
            }[audio_mode_label]
            program_audio_seconds = selected_audio_seconds
            st.caption(f"Durasi audio terpilih: {_mmss(selected_audio_seconds)}")

            srv = st.selectbox(
                "Server tujuan",
                list(playlist_rtmp_presets),
                key=prefix + "server",
            )
            rtmp_input = st.text_input(
                "RTMP URL",
                value=playlist_rtmp_presets[srv] or "rtmp://",
                key=prefix + "rtmp_custom",
                disabled=srv != "Kustom",
                help="Tujuan RTMP dapat berbeda untuk setiap stream.",
            )
            rtmp_url = rtmp_input if srv == "Kustom" else playlist_rtmp_presets[srv]
            stream_key = st.text_input(
                "YouTube Stream Key",
                type="password",
                key=prefix + "stream_key",
                placeholder="Masukkan Stream Key untuk stream ini",
            ).strip()

            with st.expander(f"Pengaturan lanjutan — {stream_name}"):
                vmode_label = st.radio(
                    "Mode video",
                    ["Otomatis", "Selalu samakan", "Kirim apa adanya"],
                    horizontal=True,
                    key=prefix + "vmode",
                )
                vmode = {
                    "Otomatis": "auto",
                    "Selalu samakan": "normalize",
                    "Kirim apa adanya": "copy",
                }[vmode_label]
                resolution_col, fps_col, bitrate_col = st.columns(3)
                height = resolution_col.selectbox(
                    "Resolusi jika disamakan", [480, 720, 1080], index=1,
                    key=prefix + "height",
                )
                fps = fps_col.selectbox(
                    "FPS jika disamakan", [24, 25, 30], index=2,
                    key=prefix + "fps",
                )
                vbr_k = bitrate_col.selectbox(
                    "Bitrate video (kbps)",
                    [0, 1500, 2500, 4000, 6000],
                    format_func=lambda value: "Otomatis" if value == 0 else str(value),
                    key=prefix + "vbr",
                )
                shuffle_video = st.checkbox(
                    "Acak video pada tiap pergantian", value=True, key=prefix + "shuffle"
                )
                volume = st.slider(
                    "Volume audio", 0.0, 2.0, 1.0, 0.05, key=prefix + "volume"
                )

            active_opening = opening_videos if opening_on else []
            all_videos = list(dict.fromkeys([*active_opening, *main_videos, *end_videos]))
            playlist_configs.append({
                "playlist_name": stream_name,
                "videos": all_videos,
                "audios": list(audio_files),
                "v_open": list(active_opening),
                "v_rand": list(main_videos),
                "v_end": list(end_videos),
                "program_mode": True,
                "program_video_mode": program_video_mode,
                "program_audio_mode": program_audio_mode,
                "program_audio_seconds": program_audio_seconds,
                "a_loop": [],
                "a_rand": list(audio_files),
                "vol_rand": volume,
                "rtmp_url": rtmp_url.strip(),
                "stream_key": stream_key,
                "vmode": vmode,
                "height": height,
                "fps": fps,
                "vbr_k": vbr_k,
                "shuffle": shuffle_video,
                "disk_min_gb": disk_min,
            })
            if not main_videos:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu video utama.")
            if not end_videos:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu video end.")
            if not audio_files:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu file audio.")
            if not stream_key:
                playlist_errors.append(f"Stream {stream_no}: Stream Key wajib diisi.")
            if not rtmp_url.strip().lower().startswith(("rtmp://", "rtmps://")):
                playlist_errors.append(f"Stream {stream_no}: RTMP URL harus diawali rtmp:// atau rtmps://.")

    st.markdown("#### Stream terakhir: berhenti")
    with st.container(border=True):
        st.write("**Slot kosong / STOP — otomatis setelah stream terakhir selesai**")
        st.caption(
            "Tidak ada server atau Stream Key pada slot ini. FFmpeg dan koneksi ke YouTube dihentikan; "
            "tidak ada nilai 0 atau key kosong yang dikirim."
        )

    if playlist_errors:
        st.warning("Periksa konfigurasi sebelum memulai:\n\n" + "\n".join(f"• {item}" for item in playlist_errors))

    start_col, stop_col = st.columns(2)
    if start_col.button(
        "▶️ Mulai multi-stream sekarang",
        type="primary",
        key="ms_start",
        disabled=stream.running,
        width="stretch",
    ):
        if playlist_errors:
            st.error("Jadwal belum lengkap. Lengkapi setiap stream dan periksa pesan di atas.")
        else:
            try:
                stream.start_playlist(playlist_configs)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Jadwal tidak dapat dimulai: {exc}")
    if stop_col.button(
        "⏹️ Hentikan jadwal",
        key="ms_stop",
        disabled=not stream.running,
        width="stretch",
    ):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def playlist_status():
        icon = {
            "live": "🔴 LIVE berjalan",
            "beralih": "⏭️ Beralih ke stream berikutnya",
            "menyiapkan": "🛠️ Menyiapkan stream",
            "menghubungkan": "🔌 Menghubungkan ke YouTube",
            "menyambung ulang": "🔁 Menyambung ulang",
            "selesai": "✅ Semua stream selesai — FFmpeg dihentikan",
            "error": "❌ Urutan streaming gagal",
        }.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.info)(
            icon + (f" — {stream.detail}" if stream.detail else "")
        )
        if stream.playlist_total:
            current_name = stream.playlist_name or "Belum dimulai"
            st.caption(
                f"Urutan: {stream.playlist_index}/{stream.playlist_total} · "
                f"{current_name} · uptime {stream.uptime()}"
            )
        if stream.running:
            a, b, c = st.columns(3)
            a.metric("Video", (stream.now_video or "-")[:32])
            b.metric("Audio", (stream.now_audio or "-")[:32])
            c.metric("Kualitas kirim", stream.health or "-")
        with st.expander("Log FFmpeg / jadwal"):
            st.code(stream.log_tail() or "(belum ada log)", language="text")

    playlist_status()

# ───────────── JOB ─────────────
with tab_jobs:
    b1, b2, b3 = st.columns(3)
    if b1.button("🔁 Coba ulang yang gagal", key="pj_retry"):
        st.toast(f"{jobs.retry_failed() + dl_jobs.retry_failed()} job diulang")
    if b2.button("🚫 Batalkan yang antre", key="pj_cancel"):
        st.toast(f"{jobs.cancel_queued() + dl_jobs.cancel_queued()} job dibatalkan")
    if b3.button("🧽 Bersihkan riwayat", key="pj_clear"):
        jobs.clear_finished()
        dl_jobs.clear_finished()
        st.rerun()

    @st.fragment(run_every=3)
    def jobs_panel():
        rm = jobs.snapshot()
        if rm:                                   # panel Render/Merge dihapus; job lama tetap terlihat di sini
            st.markdown("**Normalisasi, Render & Merge**")
            jobs_table(rm)
        st.markdown("**Impor / Unduhan**")
        jobs_table(dl_jobs.snapshot())

    jobs_panel()
    st.caption("Job yang sedang berjalan tidak bisa dibatalkan. Hasil unduhan muncul di Library.")

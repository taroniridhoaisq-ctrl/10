import random
import shutil
from urllib.parse import quote
from datetime import datetime
from functools import partial

import os
import secrets

import pandas as pd
import streamlit as st

import services as svc
import uploader

st.set_page_config(page_title="Studio Render & Live", page_icon="📡", layout="wide")

KIND_LABEL = {"video": "🎞️ Video", "audio": "🎵 Audio", "output": "📦 Hasil",
              "thumb": "🖼️ Thumbnail", "other": "🗂️ Lainnya"}
STATUS_ICON = {"antre": "⏳ antre", "berjalan": "🔄 berjalan", "selesai": "✅ selesai",
               "gagal": "❌ gagal", "dibatalkan": "🚫 dibatalkan", "diulang": "🔁 diulang"}
HINT_LABEL = {"auto": "Otomatis (deteksi isi file)", "video": "Video", "audio": "Audio"}


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
    di = svc.disk_info()
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

tab_lib, tab_imp, tab_render, tab_merge, tab_live, tab_jobs = st.tabs(
    ["📁 Library", "⬇️ Impor", "🎬 Render", "🔗 Merge", "🔴 Live 24 Jam", "⚙️ Job"])

# ───────────── LIBRARY ─────────────
with tab_lib:
    counts = {k: len(svc.list_paths(k)) for k in svc.DIRS}
    kind = st.radio("Folder", list(svc.DIRS), horizontal=True, key="lib_kind",
                    format_func=lambda k: f"{KIND_LABEL[k]} ({counts[k]})")
    files = svc.list_files(kind)
    if not files:
        st.info("Folder kosong. Tambahkan file lewat tab **Impor**.")
    else:
        df = pd.DataFrame({"Pilih": False, "File": [f["name"] for f in files],
                           "MB": [round(f["mb"], 1) for f in files],
                           "Diubah": [f["mtime"] for f in files]})
        tkey = f"tbl_{kind}"
        edited = st.data_editor(df, key=tkey, hide_index=True, width="stretch",
                                disabled=["File", "MB", "Diubah"])
        chosen = [svc.DIRS[kind] / n for n in edited.loc[edited["Pilih"], "File"]]
        if chosen:
            st.caption(f"{len(chosen)} file dipilih")
            a, b, c = st.columns([1, 1, 1])
            if a.button(f"🗑️ Hapus {len(chosen)} file", key="lib_del"):
                n = svc.delete_files(chosen, protected=protected_files())
                st.session_state.pop(tkey, None)
                st.toast(f"{n} file dihapus")
                st.rerun()
            dest = b.selectbox("Pindahkan ke", [k for k in svc.DIRS if k != kind],
                               format_func=lambda k: KIND_LABEL[k], label_visibility="collapsed")
            if c.button("📂 Pindahkan", key="lib_move"):
                n = svc.move_files(chosen, dest, protected=protected_files())
                st.session_state.pop(tkey, None)
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

# ───────────── RENDER ─────────────
with tab_render:
    st.caption("Gabungkan video + audio menjadi satu file (memakai render.py). Nama hasil = nama audio.")
    vids = svc.library_videos()
    auds = svc.list_paths("audio")
    sel_v = st.multiselect("Video", vids, format_func=svc.label, key="r_v")
    sel_a = st.multiselect("Audio — 1 audio dipakai semua video · lebih dari 1 dipilih acak per video",
                           auds, format_func=svc.label, key="r_a")
    mode = st.radio("Mode", ["copy", "fast"], horizontal=True, key="r_mode",
                    format_func=lambda m: "COPY — tanpa re-encode (tercepat)" if m == "copy"
                    else "FAST — re-encode ringan, kualitas turun (hemat disk)")
    opts = {"vbr_k": 100, "fps": 10, "scale": 640}
    if mode == "fast":
        f1, f2, f3 = st.columns(3)
        opts["vbr_k"] = f1.number_input("Bitrate video (kbps)", 50, 4000, 100, 50)
        opts["fps"] = f2.number_input("FPS", 5, 60, 10)
        opts["scale"] = f3.number_input("Lebar maks (px)", 160, 1920, 640, 80)
    if st.button("🎬 Antrekan render", type="primary", key="r_go"):
        if not sel_v or not sel_a:
            st.warning("Pilih minimal 1 video dan 1 audio.")
        else:
            for v in sel_v:
                a = sel_a[0] if len(sel_a) == 1 else random.choice(sel_a)
                jobs.submit(f"Render {v.name[:28]} + {a.name[:28]}",
                            partial(svc.run_render, video=v, audio=a, mode=mode,
                                    opts=dict(opts), disk_min_gb=disk_min))
            st.success(f"{len(sel_v)} job render diantrekan. Pantau di tab Job.")

# ───────────── MERGE ─────────────
with tab_merge:
    st.caption("Gabung dua video (opening lalu ending) memakai marge.py. Tiap hasil mengambil opening "
               "acak dari pool opening dan ending acak dari pool target.")
    vids2 = svc.library_videos()
    pool_open = st.multiselect("Pool OPENING (diambil acak)", vids2, format_func=svc.label, key="m_open")
    pool_end = st.multiselect("Pool ENDING / TARGET (isi 1 untuk target tetap)", vids2,
                              format_func=svc.label, key="m_end")
    m1, m2 = st.columns(2)
    m_mode = m1.radio("Mode", ["auto", "copy", "fast"], horizontal=True, key="m_mode",
                      help="auto: copy jika format sama, selain itu opening disamakan ke ending.")
    m_count = m2.number_input("Jumlah file hasil", 1, 50, 1, key="m_count")
    if st.button("🔗 Antrekan merge", type="primary", key="m_go"):
        if not pool_open or not pool_end:
            st.warning("Isi pool opening dan pool ending.")
        else:
            stamp = datetime.now().strftime("%H%M%S")
            made = 0
            for i in range(int(m_count)):
                op = random.choice(pool_open)
                cands = [e for e in pool_end if e != op]
                if not cands:
                    continue
                en = random.choice(cands)
                name = f"merge_{stamp}_{i + 1}_{op.stem[:18]}_{en.stem[:18]}.mp4"
                jobs.submit(f"Merge {op.name[:22]} + {en.name[:22]}",
                            partial(svc.run_merge, opening=op, ending=en, out_name=name,
                                    mode=m_mode, disk_min_gb=disk_min))
                made += 1
            if made:
                st.success(f"{made} job merge diantrekan. Pantau di tab Job.")
            else:
                st.warning("Opening dan ending tidak boleh file yang sama.")

# ───────────── LIVE ─────────────
def _mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


@st.cache_data(show_spinner=False)
def _dur(path_str: str, mtime: float) -> float:
    return svc.media_info(path_str)["dur"]


with tab_live:
    st.caption("Streaming tunggal ke YouTube Live langsung dari file di Library — tanpa render/merge manual. "
               "Satu koneksi ke YouTube tidak putus saat ganti video/lagu. Video & audio dipilih acak tiap "
               "pergantian (atau berurutan jika 'Acak' dimatikan). Video yang belum seragam diseragamkan dulu "
               "di background; live mulai begitu ada satu video siap, sisanya masuk putaran otomatis.")
    lv = st.multiselect("🎞️ Video (urutan = urutan putar, diulang terus)", svc.library_videos(),
                        format_func=svc.label, key="l_vids", placeholder="Pilih satu atau lebih video...")
    audio_src = st.radio("🔊 Sumber audio", ["file", "video"], horizontal=True, key="l_asrc",
                         format_func={"file": "🎵 Dari file audio (video dibisukan)",
                                      "video": "🎞️ Dari video (jika video punya audio)"}.get,
                         help="File audio: video dikirim tanpa suara, suara dari playlist audio di bawah. "
                              "Dari video: tiap video memakai audio aslinya (lipsync terjaga). Video yang tidak "
                              "punya audio otomatis memakai playlist audio di bawah, atau hening jika playlist kosong.")
    la = st.multiselect("🎵 Audio (urutan = urutan putar, diulang terus)" if audio_src == "file"
                        else "🎵 Audio cadangan (untuk video tanpa audio) — opsional",
                        svc.list_paths("audio"), format_func=svc.label, key="l_auds",
                        placeholder="Pilih satu atau lebih audio...")
    if lv or la:
        vt = sum(_dur(str(p), p.stat().st_mtime) for p in lv)
        at = sum(_dur(str(p), p.stat().st_mtime) for p in la)
        st.info(f"Video: {len(lv)} file · {_mmss(vt)} per putaran   |   Audio: {len(la)} file · {_mmss(at)} per putaran")

    l1, l2 = st.columns(2)
    rtmp = l1.text_input("RTMP URL", "rtmp://a.rtmp.youtube.com/live2", key="l_rtmp")
    skey = l2.text_input("Stream Key", type="password", key="l_key")

    with st.expander("⚙️ Pengaturan lanjutan"):
        vmode = st.radio("Mode video", ["auto", "normalize", "copy"], horizontal=True, key="l_vmode",
                         format_func={"auto": "Otomatis (disarankan)",
                                      "normalize": "Selalu samakan",
                                      "copy": "Kirim apa adanya"}.get,
                         help="Otomatis: video yang sudah seragam (H.264, tanpa B-frame, fps konstan, resolusi sama) dikirim "
                              "apa adanya (CPU ≈ 0). Video lain disamakan SEKALI di background (disimpan di cache) "
                              "dan baru disiarkan setelah siap, jadi live tidak pernah tersendat. "
                              "'Kirim apa adanya' hanya melonggarkan cek bitrate & keyframe.")
        n1, n2, n3 = st.columns(3)
        l_h = n1.selectbox("Resolusi (jika disamakan)", [480, 720, 1080], index=1)
        l_fps = n2.selectbox("FPS (jika disamakan)", [24, 25, 30], index=2)
        l_br = n3.selectbox("Bitrate video kbps (jika disamakan)", [1500, 2500, 4000], index=1)
        o1, o2, o3 = st.columns(3)
        l_loopa = o1.checkbox("Ulang audio terus (24/7)", value=True, key="l_loopa",
                              help="Matikan agar live berhenti sendiri saat playlist audio selesai satu putaran.")
        l_shuf = o2.checkbox("Acak tiap pergantian", value=True, key="l_shuf",
                             help="Video & lagu berikutnya dipilih acak (tanpa pengulangan berurutan) "
                                  "dan sudah disiapkan sebelum yang sekarang habis.")
        l_hours = o3.number_input("Batas durasi (jam, 0 = tanpa batas)", 0.0, 720.0, 0.0, 1.0)
        if st.button("🧹 Hapus cache hasil penyamaan", key="l_clrcache"):
            st.toast(f"{svc.clear_live_cache()} file cache dihapus")

    s1, s2 = st.columns(2)
    if s1.button("▶️ Mulai Live", type="primary", disabled=stream.running, width="stretch"):
        if not lv or (audio_src == "file" and not la):
            st.error("Pilih minimal 1 video" + (" dan 1 audio." if audio_src == "file" else "."))
        elif not skey.strip():
            st.error("Stream key wajib diisi.")
        else:
            stream.start({"videos": lv, "audios": la, "rtmp_url": rtmp.strip(), "stream_key": skey.strip(),
                          "vmode": vmode, "height": l_h, "fps": l_fps, "vbr_k": l_br,
                          "loop_audio": l_loopa, "shuffle": l_shuf, "disk_min_gb": disk_min,
                          "audio_src": audio_src}, l_hours)
            st.rerun()
    if s2.button("⏹️ Stop Live", disabled=not stream.running, width="stretch"):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def live_status():
        icon = {"live": "🔴 LIVE berjalan", "menyiapkan": "🛠️ Menyiapkan file...",
                "menghubungkan": "🔌 Menghubungkan ke YouTube...", "menyambung ulang": "🔁 Menyambung ulang...",
                "selesai": "✅ Selesai (audio habis)", "error": "❌ Gagal"}.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.warning)(
            icon + (f" — {stream.detail}" if stream.detail else ""))
        a, b, c, d = st.columns(4)
        a.metric("Uptime", stream.uptime())
        b.metric("Auto-restart", stream.restarts)
        pr = stream.progress or {}
        c.metric("Kecepatan", pr.get("speed", "-") or "-")
        d.metric("Bitrate", (pr.get("bitrate", "-") or "-").strip())
        if stream.running and stream.state == "live":
            h1, h2, h3 = st.columns(3)
            h1.metric("Buffer video", f"{stream.buf_v:.1f} MB")
            h2.metric("Buffer audio", f"{stream.buf_a:.0f} dtk")
            h3.metric("Hening disisipkan", stream.underruns,
                      help="Berapa kali audio telat dan diisi hening. Idealnya 0.")
        if stream.running:
            e, f_, g = st.columns(3)
            e.metric("Video diputar", (stream.now_video or "-")[:28])
            f_.metric("Audio diputar", (stream.now_audio or "-")[:28])
            g.metric("Pergantian video", stream.switches)
            if stream.prep_info:
                st.caption("🛠️ " + stream.prep_info)
        for n in stream.notes:
            st.caption("• " + n)
        with st.expander("Log ffmpeg"):
            st.code(stream.log_tail() or "(kosong)", language="text")

    live_status()

# ───────────── JOB ─────────────
with tab_jobs:
    b1, b2, b3 = st.columns(3)
    if b1.button("🔁 Coba ulang yang gagal", key="pj_retry"):
        st.toast(f"{jobs.retry_failed()} job diulang")
    if b2.button("🚫 Batalkan yang antre", key="pj_cancel"):
        st.toast(f"{jobs.cancel_queued()} job dibatalkan")
    if b3.button("🧽 Bersihkan riwayat", key="pj_clear"):
        jobs.clear_finished()
        st.rerun()

    @st.fragment(run_every=3)
    def jobs_panel():
        st.markdown("**Render & Merge**")
        jobs_table(jobs.snapshot())
        st.markdown("**Impor / Unduhan**")
        jobs_table(dl_jobs.snapshot())

    jobs_panel()
    st.caption("Job yang sedang berjalan tidak bisa dibatalkan; hasil render/merge muncul di "
               "Library → Hasil.")

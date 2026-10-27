"""Server upload resumable (HTTP) yang berjalan di dalam proses app + tunnel opsional.

Alur klien (upload_client.py):
  GET  /status?name=X              -> {"offset": N}   (sudah berapa byte tersimpan)
  PUT  /chunk?name=X&offset=N      -> tambah potongan di posisi N (harus sama dengan offset server)
  POST /finish?name=X&hint=auto&size=S -> pindahkan ke folder yang tepat (video/audio/thumbnail/lainnya)
  POST /abort?name=X               -> hapus potongan
Semua request wajib header  X-Token: <token>.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import services as svc

PART_DIR = svc.BASE / "partial"          # di luar tmp/ agar tidak ikut terhapus "Bersihkan folder tmp"
PART_DIR.mkdir(parents=True, exist_ok=True)


PAGE = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload file besar</title>
<style>
  :root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--mut:#6b6b6b;--card:#f4f5f7;--line:#d9dbe0;--acc:#ff4b4b;--ok:#1a9d55}
  @media (prefers-color-scheme:dark){:root{--bg:#0e1117;--fg:#eee;--mut:#9aa0aa;--card:#1a1d24;--line:#2d313b}}
  *{box-sizing:border-box} body{margin:0;padding:14px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
  .drop{border:2px dashed var(--line);border-radius:12px;padding:26px;text-align:center;background:var(--card);cursor:pointer}
  .drop.over{border-color:var(--acc)} .drop b{display:block;font-size:17px;margin-bottom:4px}
  .mut{color:var(--mut);font-size:13px} .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}
  select,input,button{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
  button{cursor:pointer} button.p{background:var(--acc);border-color:var(--acc);color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .f{background:var(--card);border-radius:10px;padding:10px 12px;margin:8px 0}
  .f .t{display:flex;justify-content:space-between;gap:8px;word-break:break-all}
  .bar{height:8px;background:var(--line);border-radius:6px;overflow:hidden;margin:8px 0 4px}
  .bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .2s}
  .done .bar i{background:var(--ok)} .err{color:var(--acc)} .okc{color:var(--ok)}
</style></head><body>
<div id="tokrow" class="row" style="display:none">
  <input id="tok" type="password" placeholder="Token" style="flex:1;min-width:200px">
  <button id="toksave">Simpan</button>
</div>
<div class="drop" id="drop"><b>📂 Pilih file atau seret ke sini</b>
  <span class="mut">Video / audio / gambar — ukuran besar OK. Putus koneksi? Pilih file yang sama lagi, upload lanjut.</span>
  <input id="pick" type="file" multiple hidden>
</div>
<div class="row">
  <label>Simpan sebagai
    <select id="hint"><option value="auto">Otomatis</option><option value="video">Video</option><option value="audio">Audio</option></select></label>
  <label>Potongan
    <select id="chunk"><option value="4">4 MB</option><option value="8" selected>8 MB</option><option value="16">16 MB</option></select></label>
  <button id="go" class="p" disabled>⬆️ Mulai upload</button>
  <button id="stop" disabled>⏸️ Jeda</button>
</div>
<div id="list"></div>
<script>
const $=id=>document.getElementById(id);
let token=(location.hash.match(/[#&]t=([^&]+)/)||[])[1]||"";
try{ if(token){localStorage.setItem("up_tok",token); history.replaceState(null,"",location.pathname);} else token=localStorage.getItem("up_tok")||""; }catch(e){}
if(!token){$("tokrow").style.display="flex";}
$("toksave").onclick=()=>{token=$("tok").value.trim(); try{localStorage.setItem("up_tok",token)}catch(e){}; if(token)$("tokrow").style.display="none";};
const base=location.origin;
let items=[], paused=false, running=false, curXhr=null;
const fmt=n=>{const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+" "+u[i]};
function add(files){ for(const f of files){ if(items.some(x=>x.file.name===f.name&&x.file.size===f.size&&!x.done))continue;
  const el=document.createElement("div");el.className="f";
  el.innerHTML='<div class="t"><span></span><span class="mut"></span></div><div class="bar"><i></i></div><div class="mut st">Menunggu</div>';
  el.querySelector(".t span").textContent=f.name; el.querySelector(".t .mut").textContent=fmt(f.size);
  $("list").appendChild(el); items.push({file:f,el,done:false}); }
  $("go").disabled=!items.some(x=>!x.done); }
const drop=$("drop");
drop.onclick=()=>$("pick").click();
$("pick").onchange=e=>{add(e.target.files);e.target.value=""};
drop.ondragover=e=>{e.preventDefault();drop.classList.add("over")};
drop.ondragleave=()=>drop.classList.remove("over");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");add(e.dataTransfer.files)};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function api(method,path,body){
  const r=await fetch(base+path,{method,headers:{"X-Token":token},body});
  let j={};try{j=await r.json()}catch(e){} return [r.status,j];
}
function putChunk(name,offset,blob,onprog){
  return new Promise(res=>{
    const x=new XMLHttpRequest(); curXhr=x;
    x.open("PUT",base+"/chunk?name="+encodeURIComponent(name)+"&offset="+offset);
    x.setRequestHeader("X-Token",token);
    x.upload.onprogress=e=>{if(e.lengthComputable)onprog(e.loaded)};
    x.onload=()=>{let j={};try{j=JSON.parse(x.responseText)}catch(e){} res([x.status,j])};
    x.onerror=()=>res([0,{error:"koneksi putus"}]); x.ontimeout=()=>res([0,{error:"timeout"}]); x.onabort=()=>res([-1,{}]);
    x.timeout=300000; x.send(blob);
  });
}
async function send(it){
  const f=it.file, el=it.el, bar=el.querySelector(".bar i"), st=el.querySelector(".st");
  const csize=parseInt($("chunk").value)*1048576, name=f.name;
  let [c,j]=await api("GET","/status?name="+encodeURIComponent(name));
  if(c===401){st.innerHTML='<span class="err">Token salah</span>';return false}
  if(c!==200){st.innerHTML='<span class="err">Server menolak ('+c+')</span>';return false}
  let off=j.offset; if(off>f.size){await api("POST","/abort?name="+encodeURIComponent(name));off=0}
  if(off)st.textContent="Lanjut dari "+fmt(off);
  const t0=Date.now(), o0=off; let fails=0;
  while(off<f.size){
    if(paused){st.textContent="Dijeda di "+fmt(off);return false}
    const end=Math.min(off+csize,f.size);
    const [code,r]=await putChunk(name,off,f.slice(off,end),loaded=>{
      const cur=off+loaded; bar.style.width=(cur/f.size*100)+"%";
      st.textContent=(cur/f.size*100).toFixed(1)+"% · "+fmt(cur)+" / "+fmt(f.size)+" · "+fmt((cur-o0)/Math.max((Date.now()-t0)/1000,.001))+"/s";});
    if(code===-1){st.textContent="Dijeda di "+fmt(off);return false}
    if(code===200){off=r.offset;fails=0;bar.style.width=(off/f.size*100)+"%"}
    else if(code===409){off=r.offset}
    else if(code===401||code===507){st.innerHTML='<span class="err">'+(r.error||code)+'</span>';return false}
    else{ fails++; if(fails>12){st.innerHTML='<span class="err">Gagal terus: '+(r.error||code)+' — pilih file lagi untuk melanjutkan</span>';return false}
      const w=Math.min(2**fails,30); st.innerHTML='<span class="err">Gangguan ('+(r.error||code)+'), ulang '+w+'s…</span>';
      await sleep(w*1000); const [c2,j2]=await api("GET","/status?name="+encodeURIComponent(name).replace(/%20/g,"%20")).catch(()=>[0,{}]); if(c2===200)off=j2.offset; }
  }
  st.textContent="Menyimpan…";
  const [fc,fr]=await api("POST","/finish?name="+encodeURIComponent(name)+"&hint="+$("hint").value+"&size="+f.size);
  if(fc!==200){st.innerHTML='<span class="err">Finalisasi gagal: '+(fr.error||fc)+'</span>';return false}
  el.classList.add("done"); bar.style.width="100%";
  st.innerHTML='<span class="okc">✅ Selesai → '+fr.kind+' / '+fr.saved_as+'</span>'; it.done=true; return true;
}
$("go").onclick=async()=>{
  if(!token){$("tokrow").style.display="flex";return}
  if(running)return; running=true; paused=false; $("go").disabled=true; $("stop").disabled=false;
  for(const it of items){ if(it.done)continue; if(paused)break; await send(it); }
  running=false; $("stop").disabled=true; $("go").disabled=!items.some(x=>!x.done);
};
$("stop").onclick=()=>{paused=true; if(curXhr)curXhr.abort();};
window.addEventListener("beforeunload",e=>{if(running){e.preventDefault();e.returnValue=""}});
</script></body></html>"""


class UploadServer:
    def __init__(self, port: int, token: str, disk_min_gb: float = 0.5):
        self.port, self.token, self.disk_min_gb = port, token, disk_min_gb
        self.error: str | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()

    # ---------- util ----------
    def part_path(self, name: str) -> Path:
        return PART_DIR / (svc.safe_name(name) + ".part")

    def lock_for(self, name: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(svc.safe_name(name), threading.Lock())

    def partials(self) -> list[dict]:
        return [{"name": p.name[:-5], "mb": p.stat().st_size / 1024 / 1024} for p in PART_DIR.glob("*.part")]

    # ---------- server ----------
    def start(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # senyap
                pass

            def _send(self, code: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "X-Token, Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _auth(self):
                if not hmac.compare_digest(self.headers.get("X-Token", ""), outer.token):
                    self._send(401, {"error": "token salah"})
                    return None
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                if not name:
                    self._send(400, {"error": "parameter name wajib"})
                    return None
                return q, name

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    body = PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/ping":
                    return self._send(200, {"ok": True})
                if path == "/file":
                    return self._serve_file()
                a = self._auth()
                if a is None:
                    return
                _, name = a
                p = outer.part_path(name)
                self._send(200, {"offset": p.stat().st_size if p.exists() else 0})

            def do_PUT(self):
                a = self._auth()
                if a is None:
                    return
                q, name = a
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    offset = int((q.get("offset") or ["0"])[0])
                except ValueError:
                    return self._send(400, {"error": "offset tidak valid"})
                p = outer.part_path(name)
                with outer.lock_for(name):
                    cur = p.stat().st_size if p.exists() else 0
                    if offset != cur:
                        # buang body agar koneksi tetap sinkron
                        self._drain(length)
                        return self._send(409, {"error": "offset tidak cocok", "offset": cur})
                    if shutil.disk_usage(svc.BASE).free < outer.disk_min_gb * 1024**3 + length:
                        self._drain(length)
                        return self._send(507, {"error": "disk server penuh"})
                    left = length
                    with open(p, "ab") as f:
                        while left > 0:
                            buf = self.rfile.read(min(1024 * 1024, left))
                            if not buf:
                                break           # koneksi putus; yang sudah tertulis tetap dipakai
                            f.write(buf)
                            left -= len(buf)
                    new = p.stat().st_size
                if left > 0:
                    self.close_connection = True
                    return
                self._send(200, {"offset": new})

            def do_POST(self):
                a = self._auth()
                if a is None:
                    return
                q, name = a
                path = urlparse(self.path).path
                p = outer.part_path(name)
                with outer.lock_for(name):
                    if path == "/abort":
                        p.unlink(missing_ok=True)
                        return self._send(200, {"ok": True})
                    if path != "/finish":
                        return self._send(404, {"error": "tidak ada"})
                    if not p.exists():
                        return self._send(404, {"error": "tidak ada potongan untuk file ini"})
                    size = p.stat().st_size
                    want = (q.get("size") or [""])[0]
                    if want and int(want) != size:
                        return self._send(409, {"error": "ukuran tidak cocok", "offset": size})
                    hint = (q.get("hint") or ["auto"])[0]
                    final_tmp = p.with_name(svc.safe_name(name))
                    p.rename(final_tmp)
                    try:
                        kind, dest = svc.place_file(final_tmp, hint if hint in ("auto", "video", "audio") else "auto")
                    except Exception as e:  # noqa: BLE001
                        final_tmp.rename(p)
                        return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                self._send(200, {"kind": kind, "saved_as": dest.name, "bytes": size})

            def _serve_file(self):
                """Kirim file library dengan dukungan Range: download besar & seek video tanpa RAM."""
                q = parse_qs(urlparse(self.path).query)
                tok = (q.get("t") or [""])[0] or self.headers.get("X-Token", "")
                if not hmac.compare_digest(tok, outer.token):
                    return self._send(401, {"error": "token salah"})
                kind = (q.get("kind") or [""])[0]
                name = Path((q.get("name") or [""])[0]).name
                if kind not in svc.DIRS or not name:
                    return self._send(400, {"error": "kind/name tidak valid"})
                fp = svc.DIRS[kind] / name
                if not fp.is_file():
                    return self._send(404, {"error": "file tidak ada"})
                size = fp.stat().st_size
                start, end, code = 0, size - 1, 200
                rng = self.headers.get("Range", "")
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:                                   # "bytes=-N" = N byte terakhir
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                dl = (q.get("dl") or ["0"])[0] == "1"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if dl:
                    self.send_header("Content-Disposition", 'attachment; filename="' + name.replace('"', "") + '"')
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                left = end - start + 1
                try:
                    with open(fp, "rb") as f:
                        f.seek(start)
                        while left > 0:
                            buf = f.read(min(1024 * 1024, left))
                            if not buf:
                                break
                            self.wfile.write(buf)
                            left -= len(buf)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _drain(self, n: int):
                while n > 0:
                    b = self.rfile.read(min(1024 * 1024, n))
                    if not b:
                        break
                    n -= len(b)

        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), H)
        except OSError as e:
            self.error = f"Port {self.port} tidak bisa dipakai: {e}"
            return
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


# ───────────────────────── tunnel publik (opsional) ─────────────────────────
TRY_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Tunnel:
    """cloudflared quick tunnel agar port upload bisa dijangkau dari luar (mis. di Streamlit Cloud,
    yang hanya membuka satu port). Tidak perlu akun. Butuh internet keluar dari server app."""

    def __init__(self, port: int):
        self.port = port
        self.url: str | None = None
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None

    def _binary(self) -> str:
        found = shutil.which("cloudflared")
        if found:
            return found
        dest = svc.BASE / "bin" / "cloudflared"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
            urllib.request.urlretrieve(url, dest)
            dest.chmod(0o755)
        return str(dest)

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.url = self.error = None
        try:
            self.proc = subprocess.Popen(
                [self._binary(), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except Exception as e:  # noqa: BLE001
            self.error = f"Gagal menjalankan cloudflared: {e}"
            return

        def read():
            for line in self.proc.stderr:
                m = TRY_RE.search(line)
                if m and not self.url:
                    self.url = m.group(0)
        threading.Thread(target=read, daemon=True).start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.url = None

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

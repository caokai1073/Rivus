"""
server.py — FastAPI backend
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import zipfile
import tempfile
from queue import Queue, Empty
from typing import Iterator
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from db import (init_db, list_documents, delete_document, export_all, DB_PATH,
                list_folders, create_folder, rename_folder, delete_folder, move_document,
                get_document_by_id, search_documents, find_by_url, rename_document)
from ingest import ingest_url, ingest_text, ingest_pdf, ingest_docx, ingest_md, ingest_excel
from query import answer_stream, list_ollama_models, DEFAULT_MODEL
from config import (get_cloud_keys, set_cloud_keys, get_enabled_cloud_models,
                    CLOUD_PROVIDERS, APP_VERSION, UPDATE_CHECK_URL,
                    get_ollama_options, set_ollama_options, OLLAMA_OPTIONS_DEFAULTS,
                    get_remote_config, set_remote_config)
import remote as _remote_mod

app = FastAPI()
init_db()

# ── Embedding model warm-up (background thread to avoid first-query lag) ─────
_embed_ready = [False]

def _prewarm_embed():
    try:
        from ingest import get_embed_model
        get_embed_model()
        _embed_ready[0] = True
        print("[embed] OK Warm-up complete")
    except Exception as e:
        print(f"[embed] Warm-up failed: {e}")

threading.Thread(target=_prewarm_embed, daemon=True).start()

UI_DIR = Path(__file__).parent / "ui"
UI_PATH = UI_DIR / "index.html"
app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

# Persistent PDF storage directory (same level as the database)
PDF_DIR = DB_PATH.parent / "pdfs"
PDF_DIR.mkdir(exist_ok=True)


def _save_pdf_copy(src: str, original_name: str) -> str:
    """Copy an uploaded file to the data directory, returns the permanent path"""
    safe_name = f"{int(time.time())}_{Path(original_name).name}"
    dest = PDF_DIR / safe_name
    shutil.copy2(src, dest)
    return str(dest)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        content=UI_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/docs")
def get_docs(q: Optional[str] = None):
    if q and q.strip():
        return search_documents(q.strip())
    return list_documents()


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: int):
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.delete("/api/docs/{doc_id}")
def del_doc(doc_id: int):
    delete_document(doc_id)
    return {"ok": True}


@app.delete("/api/docs")
def del_docs_bulk(body: dict):
    """Bulk delete documents by ID list. Body: {"ids": [1, 2, 3]}"""
    ids = body.get("ids", [])
    for doc_id in ids:
        try:
            delete_document(int(doc_id))
        except Exception:
            pass
    return {"ok": True, "deleted": len(ids)}


@app.get("/api/docs/{doc_id}/file")
def get_doc_file(doc_id: int):
    """Serve the original file for download. For User text entries, generates a .txt on the fly."""
    from fastapi.responses import Response as FastAPIResponse
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    url = doc.get("url", "")
    if url and not url.startswith("http://") and not url.startswith("https://"):
        if not os.path.isfile(url):
            raise HTTPException(status_code=404, detail=f"File not found on disk: {url}")
        filename = Path(url).name
        return FileResponse(url, filename=filename)
    # No local file (User text or URL source): serve full_text as .txt
    full_text = doc.get("full_text") or ""
    title = re.sub(r'[^\w\s\-]', '', doc.get("title", "document"))[:60].strip() or "document"
    return FastAPIResponse(
        content=full_text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{title}.txt"'},
    )


@app.get("/api/docs/{doc_id}/open-path")
def get_doc_open_path(doc_id: int):
    """Return a local filesystem path suitable for open_file() in native mode.
    For User text entries, writes content to a temp .txt file and returns that path."""
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    url = doc.get("url", "")
    if url and not url.startswith("http://") and not url.startswith("https://"):
        if os.path.isfile(url):
            return {"path": url}
        raise HTTPException(status_code=404, detail=f"File not found on disk: {url}")
    # Generate a temp .txt for User text
    title = re.sub(r'[^\w\s\-]', '', doc.get("title", "document"))[:40].strip() or "document"
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".txt",
        prefix=title + "_",
        mode="w", encoding="utf-8",
    )
    tmp.write(doc.get("full_text") or "")
    tmp.close()
    return {"path": tmp.name}


def _stream_ingest(fn, fn_kwargs: dict, cleanup_path: str = None) -> Iterator[str]:
    """
    Runs an ingest function in a background thread, collects progress via a Queue,
    and yields SSE events.
    Event format: data: {"type": "progress"|"done"|"error", ...}
    """
    q: Queue = Queue()

    def progress_cb(done: int, total: int, phase: str):
        q.put({"type": "progress", "done": done, "total": total, "phase": phase})

    fn_kwargs["progress_cb"] = progress_cb

    def run():
        try:
            result = fn(**fn_kwargs)
            q.put({"type": "done", "result": result})
        except Exception as e:
            traceback.print_exc()
            q.put({"type": "error", "message": str(e)})
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()

    deadline = 600  # total timeout: 10 minutes
    elapsed = 0
    while elapsed < deadline:
        try:
            event = q.get(timeout=5)  # check every 5 s
        except Empty:
            elapsed += 5
            # Send a keepalive comment to prevent WebView2/WKWebView from closing the connection
            yield ": keepalive\n\n"
            continue
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        if event["type"] in ("done", "error"):
            return
    yield f'data: {json.dumps({"type": "error", "message": "Timeout after 10 minutes"})}\n\n'


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.post("/api/ingest/url")
def api_ingest_url(url: str = Form(...), custom_title: Optional[str] = Form(None), folder_id: Optional[int] = Form(None)):
    # Dedup: don't re-ingest a URL that's already in the database
    existing = find_by_url(url.strip())
    if existing:
        raise HTTPException(status_code=409, detail=f"__duplicate__{existing['id']}__{existing['title']}")
    return StreamingResponse(
        _stream_ingest(ingest_url, dict(
            url=url, custom_title=custom_title or None, folder_id=folder_id,
        )),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.patch("/api/docs/{doc_id}/title")
def api_rename_doc(doc_id: int, title: str = Form(...)):
    if not title.strip():
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    rename_document(doc_id, title.strip())
    return {"ok": True}


@app.post("/api/ingest/text")
def api_ingest_text(title: str = Form(...), text: str = Form(...), folder_id: Optional[int] = Form(None)):
    try:
        return ingest_text(title=title, text=text, folder_id=folder_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/ingest/pdf")
def api_ingest_pdf(file: UploadFile = File(...), custom_title: Optional[str] = Form(None), folder_id: Optional[int] = Form(None)):
    """Browser file upload PDF; returns progress via SSE stream"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    shutil.copyfileobj(file.file, tmp)
    tmp.close()
    if os.path.getsize(tmp.name) == 0:
        os.unlink(tmp.name)
        raise HTTPException(status_code=400, detail="Uploaded file is empty. Please select a valid file.")
    stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.pdf")
    return StreamingResponse(
        _stream_ingest(ingest_pdf, dict(
            file_path=tmp.name, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=stored_path,
        ), cleanup_path=tmp.name),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/pdf-path")
def api_ingest_pdf_path(path: str = Form(...), custom_title: Optional[str] = Form(None), folder_id: Optional[int] = Form(None)):
    """pywebview native PDF picker; returns progress via SSE stream"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    return StreamingResponse(
        _stream_ingest(ingest_pdf, dict(
            file_path=path, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=path,
        )),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.get("/api/folders")
def get_folders():
    return list_folders()

@app.post("/api/folders")
def api_create_folder(name: str = Form(...)):
    fid = create_folder(name.strip())
    return {"id": fid, "name": name.strip()}

@app.patch("/api/folders/{folder_id}")
def api_rename_folder(folder_id: int, name: str = Form(...)):
    rename_folder(folder_id, name.strip())
    return {"ok": True}

@app.delete("/api/folders/{folder_id}")
def api_delete_folder(folder_id: int):
    delete_folder(folder_id)
    return {"ok": True}

@app.patch("/api/docs/{doc_id}/folder")
def api_move_doc(doc_id: int, folder_id: Optional[int] = Form(None)):
    move_document(doc_id, folder_id)
    return {"ok": True}

@app.get("/api/ollama-status")
def ollama_status():
    """Check whether Ollama is installed and running"""
    import shutil, requests as req

    running = False
    try:
        r = req.get("http://localhost:11434/api/tags", timeout=2)
        running = r.ok
    except Exception:
        pass

    # Check installation path
    installed = bool(shutil.which("ollama"))
    if not installed:
        candidates = [
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            str(Path.home() / "Applications/Ollama.app/Contents/MacOS/Ollama"),
            "/Applications/Ollama.app/Contents/MacOS/Ollama",
        ]
        installed = any(os.path.exists(p) for p in candidates)

    # Check for Ollama.app bundle
    app_path = None
    for p in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(p):
            app_path = p
            break

    return {"running": running, "installed": installed, "app_path": app_path}


@app.post("/api/ollama-fix")
def ollama_fix():
    """Fix Ollama permissions and launch (macOS / Windows)"""
    import platform, stat as _stat

    if platform.system() == "Windows":
        ollama_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
        if not ollama_exe.exists():
            ollama_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if not ollama_exe.exists():
            bin_path = shutil.which("ollama")
            if bin_path:
                subprocess.Popen([bin_path, "serve"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                return {"ok": True}
            return {"ok": False, "error": "Ollama not found. Please install it first."}
        subprocess.Popen([str(ollama_exe)], creationflags=subprocess.CREATE_NO_WINDOW)
        return {"ok": True}

    # macOS
    app_path = None
    for p in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(p):
            app_path = p
            break
    if not app_path:
        return {"ok": False, "error": "Ollama.app not found"}

    for root, dirs, files in os.walk(app_path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                cur = os.stat(fp).st_mode
                os.chmod(fp, cur | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
            except Exception:
                pass

    subprocess.run(["xattr", "-rd", "com.apple.quarantine", app_path], capture_output=True)
    subprocess.Popen(["open", app_path])
    return {"ok": True}


@app.post("/api/ollama-start")
def ollama_start():
    """Trigger Ollama launch (non-blocking; frontend polls status)"""
    import platform, shutil, subprocess, requests as req

    # Already running
    try:
        if req.get("http://localhost:11434/api/tags", timeout=2).ok:
            return {"ok": True, "already_running": True}
    except Exception:
        pass

    if platform.system() == "Windows":
        # Prefer "Ollama app.exe" (with tray icon), fall back to ollama.exe serve
        for exe_name in ["ollama app.exe", "ollama.exe"]:
            exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / exe_name
            if exe.exists():
                subprocess.Popen([str(exe)], creationflags=subprocess.CREATE_NO_WINDOW)
                return {"ok": True, "launched": True}
        ollama_bin = shutil.which("ollama")
        if ollama_bin:
            subprocess.Popen([ollama_bin, "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return {"ok": True, "launched": True}
        return {"ok": False, "error": "Ollama is not installed"}

    # macOS: use `open` if Ollama.app is found
    for app_dir in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(app_dir):
            subprocess.Popen(["open", app_dir])
            return {"ok": True, "launched": True}

    # No .app found, try ollama serve
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        subprocess.Popen([ollama_bin, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "launched": True}

    return {"ok": False, "error": "Ollama is not installed"}


@app.get("/api/ollama-install")
def ollama_install():
    """Stream Ollama download and installation (macOS and Windows)"""
    import platform, zipfile, tempfile, subprocess, requests as req, time

    def generate():
        try:
            system = platform.system()
            if system not in ("Darwin", "Windows"):
                yield f'data: {json.dumps({"status":"error","error":"Only macOS and Windows are supported","done":True})}\n\n'
                return

            # ── Windows: redirect to website, user installs manually ──────────
            if system == "Windows":
                import webbrowser
                webbrowser.open("https://ollama.com")
                yield f'data: {json.dumps({"status":"error","error":"Please download and install Ollama from the browser, then restart Rivus to use local models","done":True})}\n\n'
                return

            # ── macOS installation ────────────────────────────────────────────
            url = "https://ollama.com/download/Ollama-darwin.zip"
            yield f'data: {json.dumps({"status":"Downloading Ollama...","pct":0})}\n\n'

            resp = req.get(url, stream=True, timeout=600,
                           headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
            try:
                with os.fdopen(tmp_fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = int(downloaded / total * 70)
                                mb = downloaded // 1024 // 1024
                                yield f'data: {json.dumps({"status":f"Downloading Ollama... {mb} MB","pct":pct})}\n\n'

                yield f'data: {json.dumps({"status":"Extracting...","pct":72})}\n\n'

                extract_dir = tempfile.mkdtemp()
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(extract_dir)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            # Find Ollama.app in extracted contents
            app_src = None
            for root, dirs, _ in os.walk(extract_dir):
                for d in dirs:
                    if d == "Ollama.app":
                        app_src = os.path.join(root, d)
                        break
                if app_src:
                    break

            if not app_src:
                raise FileNotFoundError("Ollama.app not found after extraction")

            yield f'data: {json.dumps({"status":"Installing...","pct":85})}\n\n'

            # Install to ~/Applications (no admin rights needed)
            apps_dir = Path.home() / "Applications"
            apps_dir.mkdir(exist_ok=True)
            dest = apps_dir / "Ollama.app"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(app_src, str(dest))
            shutil.rmtree(extract_dir, ignore_errors=True)

            # Fix executable permissions (zipfile extraction doesn't preserve +x)
            import stat as _stat
            for root, dirs, files in os.walk(str(dest)):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        cur = os.stat(fp).st_mode
                        os.chmod(fp, cur | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
                    except Exception:
                        pass
            # Remove quarantine attribute (prevents Gatekeeper from blocking launch)
            subprocess.run(["xattr", "-rd", "com.apple.quarantine", str(dest)],
                           capture_output=True)

            subprocess.Popen(["open", str(dest)])
            yield f'data: {json.dumps({"status":"success","pct":100,"done":True})}\n\n'

        except Exception as e:
            yield f'data: {json.dumps({"status":"error","error":str(e),"done":True})}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/system-info")
def get_system_info():
    """Returns system RAM (GB) + list of installed LLM models (embedding models filtered out)"""
    import platform
    ram_gb = 0
    try:
        if platform.system() == "Darwin":
            import subprocess
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            ram_gb = int(out) // (1024 ** 3)
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        ram_gb = int(line.split()[1]) // (1024 ** 2)
                        break
        else:
            import ctypes
            kernel = ctypes.windll.kernel32
            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                             ("dwMemoryLoad", ctypes.c_ulong),
                             ("ullTotalPhys", ctypes.c_ulonglong),
                             ("ullAvailPhys", ctypes.c_ulonglong),
                             ("ullTotalPageFile", ctypes.c_ulonglong),
                             ("ullAvailPageFile", ctypes.c_ulonglong),
                             ("ullTotalVirtual", ctypes.c_ulonglong),
                             ("ullAvailVirtual", ctypes.c_ulonglong),
                             ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MemStatus()
            ms.dwLength = ctypes.sizeof(ms)
            kernel.GlobalMemoryStatusEx(ctypes.byref(ms))
            ram_gb = ms.ullTotalPhys // (1024 ** 3)
    except Exception:
        ram_gb = 0

    models = list_ollama_models()
    # Filter out embedding models (nomic series and other non-chat models)
    chat_models = [m for m in models if not any(
        m.lower().startswith(p) for p in ("nomic", "all-minilm", "mxbai", "bge", "snowflake")
    )]

    return {
        "ram_gb": ram_gb,
        "installed_models": chat_models,
        "platform": platform.system(),   # "Darwin" | "Windows" | "Linux"
    }


@app.get("/api/pull-model")
def pull_model(model: str):
    """Stream Ollama model pull progress as SSE"""
    import requests as req

    def generate():
        try:
            with req.post(
                "http://localhost:11434/api/pull",
                json={"model": model, "stream": True},
                stream=True,
                timeout=600,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    status = obj.get("status", "")
                    total = obj.get("total", 0)
                    completed = obj.get("completed", 0)
                    pct = int(completed / total * 100) if total else 0
                    payload = json.dumps({
                        "status": status,
                        "pct": pct,
                        "total_bytes": total,
                        "completed_bytes": completed,
                        "done": obj.get("status") == "success",
                    }, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
            yield 'data: {"status":"success","pct":100,"done":true}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"status":"error","error":str(e),"done":true})}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.delete("/api/delete-model")
def delete_model(model: str):
    """Delete a local Ollama model"""
    import requests as req
    try:
        resp = req.delete(
            "http://localhost:11434/api/delete",
            json={"name": model},
            timeout=30,
        )
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/cloud-config")
def get_cloud_config():
    """Returns current cloud configuration (API keys partially masked)"""
    keys = get_cloud_keys()
    result = {}
    for provider, meta in CLOUD_PROVIDERS.items():
        entry = keys.get(provider, {})
        raw_key = entry.get("api_key", "")
        masked = (raw_key[:6] + "****" + raw_key[-4:]) if len(raw_key) > 10 else ("****" if raw_key else "")
        result[provider] = {
            "name": meta["name"],
            "enabled": entry.get("enabled", bool(raw_key)),
            "api_key_masked": masked,
            "configured": bool(raw_key),
        }
    return result


@app.post("/api/cloud-config")
def save_cloud_config(payload: dict):
    """
    Save cloud API keys.
    payload: { provider: { api_key?: str, enabled?: bool } }
    An empty api_key string means "don't update" (keep existing value).
    """
    current = get_cloud_keys()
    for provider, data in payload.items():
        if provider not in CLOUD_PROVIDERS:
            continue
        entry = current.setdefault(provider, {})
        if data.get("api_key"):          # only overwrite if non-empty
            entry["api_key"] = data["api_key"]
        if "enabled" in data:
            entry["enabled"] = data["enabled"]
    set_cloud_keys(current)
    return {"ok": True}


@app.get("/api/ollama-options")
def get_ollama_options_api():
    return {"options": get_ollama_options(), "defaults": OLLAMA_OPTIONS_DEFAULTS}


@app.post("/api/ollama-options")
def save_ollama_options_api(payload: dict):
    set_ollama_options(payload)
    return {"ok": True}


# ── Remote Server ─────────────────────────────────────────────────────────────

@app.get("/api/remote/config")
def remote_config_get():
    return get_remote_config()


@app.get("/api/remote/status")
def remote_status():
    return _remote_mod.status()


@app.post("/api/remote/connect")
def remote_connect(payload: dict):
    # Save config (password is not persisted)
    cfg = {k: payload.get(k, v) for k, v in {
        "host": "", "user": "", "ssh_port": 22, "auth_mode": "key",
        "key_path": "~/.ssh/id_rsa", "remote_port": 11434, "local_port": 11435,
    }.items()}
    set_remote_config(cfg)
    result = _remote_mod.connect(
        host=cfg["host"],
        user=cfg["user"],
        ssh_port=int(cfg["ssh_port"]),
        auth_mode=cfg["auth_mode"],
        key_path=cfg.get("key_path", ""),
        password=payload.get("password", ""),   # password is not persisted
        remote_port=int(cfg["remote_port"]),
        local_port=int(cfg["local_port"]),
    )
    return result


@app.post("/api/remote/disconnect")
def remote_disconnect():
    _remote_mod.disconnect()
    return {"ok": True}


@app.get("/api/models")
def get_models():
    _EMBED_PREFIXES = ("nomic", "all-minilm", "mxbai", "bge", "snowflake")

    # Local Ollama models (always queries localhost)
    local_raw = list_ollama_models()
    chat_local = [m for m in local_raw if not any(m.lower().startswith(p) for p in _EMBED_PREFIXES)]

    # Remote SSH tunnel models (if connected)
    remote_raw = _remote_mod.list_remote_models()
    chat_remote = [m for m in remote_raw if not any(m.lower().startswith(p) for p in _EMBED_PREFIXES)]

    # Cloud models (with configured keys)
    cloud = get_enabled_cloud_models()

    all_models = (
        [{"id": m,              "label": m}              for m in chat_local]
        + [{"id": f"remote:{m}", "label": f"🌐 ssh: {m}"} for m in chat_remote]
        + [{"id": m["id"],       "label": m["label"]}     for m in cloud]
    )

    # Default model: prefer local, then remote, then cloud
    default_id = (
        chat_local[0]          if chat_local  else
        f"remote:{chat_remote[0]}" if chat_remote else
        cloud[0]["id"]         if cloud       else
        DEFAULT_MODEL
    )
    return {"models": all_models, "default": default_id}


@app.get("/api/embed-ready")
def embed_ready():
    return {"ready": _embed_ready[0]}


@app.get("/api/version")
def get_version():
    result = {"current": APP_VERSION, "latest": None, "update_available": False, "url": ""}
    if UPDATE_CHECK_URL:
        try:
            import requests as req
            r = req.get(UPDATE_CHECK_URL, timeout=6,
                        headers={"User-Agent": "Rivus-App"})
            data = r.json()
            latest = data.get("tag_name", "").lstrip("v")

            def _ver(v):
                try:
                    return tuple(int(x) for x in v.split("."))
                except Exception:
                    return (0,)

            if latest and _ver(latest) > _ver(APP_VERSION):
                result["latest"] = latest
                result["update_available"] = True
                # Find the platform-specific installer asset
                import platform as _platform
                suffix = ".dmg" if _platform.system() == "Darwin" else ".exe"
                assets = data.get("assets", [])
                asset = next((a for a in assets if a["name"].endswith(suffix)), None)
                result["url"] = asset["browser_download_url"] if asset else data.get("html_url", "")
        except Exception:
            pass
    return result


@app.get("/api/export")
def export_backup(save_path: Optional[str] = None):
    """Export knowledge base: pack into a zip at the user-specified path"""
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"rivus-backup-{date_str}.zip"

    if save_path:
        out_path = Path(save_path)
        # Ensure .zip extension
        if out_path.suffix.lower() != ".zip":
            out_path = out_path.with_suffix(".zip")
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Fall back to Downloads if no path provided
        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)
        out_path = downloads / filename

    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
        if DB_PATH.exists():
            zf.write(DB_PATH, "rivus.db")
        if PDF_DIR.exists():
            for f in PDF_DIR.iterdir():
                if f.is_file():
                    zf.write(f, f"pdfs/{f.name}")

    return {"ok": True, "path": str(out_path), "filename": out_path.name}


@app.post("/api/import-backup")
def import_backup(zip_path: str = Form(...)):
    """Restore knowledge base from a backup zip (replaces current database and PDFs)"""
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=400, detail=f"File not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise HTTPException(status_code=400, detail="Not a valid zip file")

    # Extract to temp dir for validation
    tmp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if "rivus.db" not in names:
                raise HTTPException(status_code=400, detail="rivus.db not found in backup. Please verify the file source.")
            zf.extractall(tmp_dir)

        # Back up current database before replacing
        db_src = Path(tmp_dir) / "rivus.db"
        if DB_PATH.exists():
            shutil.copy2(DB_PATH, str(DB_PATH) + ".bak")
        shutil.copy2(db_src, DB_PATH)

        # Restore PDF files
        pdf_src_dir = Path(tmp_dir) / "pdfs"
        if pdf_src_dir.exists():
            PDF_DIR.mkdir(exist_ok=True)
            for f in pdf_src_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, PDF_DIR / f.name)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"ok": True, "message": "Backup restored. Please restart the app to load the latest data."}


@app.post("/api/ingest/docx")
def api_ingest_docx(file: UploadFile = File(...),
                    custom_title: Optional[str] = Form(None),
                    folder_id: Optional[int] = Form(None)):
    """Browser file upload .docx; returns progress via SSE stream"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    shutil.copyfileobj(file.file, tmp)
    tmp.close()
    if os.path.getsize(tmp.name) == 0:
        os.unlink(tmp.name)
        raise HTTPException(status_code=400, detail="Uploaded file is empty. Please select a valid file.")
    stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.docx")
    return StreamingResponse(
        _stream_ingest(ingest_docx, dict(
            file_path=tmp.name, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=stored_path,
        ), cleanup_path=tmp.name),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/docx-path")
def api_ingest_docx_path(path: str = Form(...),
                         custom_title: Optional[str] = Form(None),
                         folder_id: Optional[int] = Form(None)):
    """pywebview native .docx picker; returns progress via SSE stream"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    return StreamingResponse(
        _stream_ingest(ingest_docx, dict(
            file_path=path, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=path,
        )),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/excel")
def api_ingest_excel(file: UploadFile = File(...),
                     custom_title: Optional[str] = Form(None),
                     folder_id: Optional[int] = Form(None)):
    """Browser file upload .xlsx; returns progress via SSE stream"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    shutil.copyfileobj(file.file, tmp)
    tmp.close()
    stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.xlsx")
    return StreamingResponse(
        _stream_ingest(ingest_excel, dict(
            file_path=tmp.name, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=stored_path,
        ), cleanup_path=tmp.name),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/excel-path")
def api_ingest_excel_path(path: str = Form(...),
                          custom_title: Optional[str] = Form(None),
                          folder_id: Optional[int] = Form(None)):
    """pywebview native .xlsx picker; returns progress via SSE stream"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    return StreamingResponse(
        _stream_ingest(ingest_excel, dict(
            file_path=path, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=path,
        )),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/md")
def api_ingest_md(file: UploadFile = File(...),
                  custom_title: Optional[str] = Form(None),
                  folder_id: Optional[int] = Form(None)):
    """Browser file upload .md; returns progress via SSE stream"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
    shutil.copyfileobj(file.file, tmp)
    tmp.close()
    stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.md")
    return StreamingResponse(
        _stream_ingest(ingest_md, dict(
            file_path=tmp.name, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=stored_path,
        ), cleanup_path=tmp.name),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.post("/api/ingest/md-path")
def api_ingest_md_path(path: str = Form(...),
                       custom_title: Optional[str] = Form(None),
                       folder_id: Optional[int] = Form(None)):
    """pywebview native .md picker; returns progress via SSE stream"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    return StreamingResponse(
        _stream_ingest(ingest_md, dict(
            file_path=path, custom_title=custom_title or None,
            folder_id=folder_id, stored_url=path,
        )),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.get("/api/query")
def api_query(q: str, model: str = DEFAULT_MODEL, doc_id: Optional[int] = None):
    def generate():
        try:
            stream, sources = answer_stream(q, model=model, pinned_doc_id=doc_id)
            for token in stream:
                data = json.dumps({"type": "token", "content": token}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            if sources:
                seen, src_list = set(), []
                for s in sources:
                    key = (s["title"], s["url"])
                    if key not in seen:
                        seen.add(key)
                        src_list.append({"doc_id": s.get("doc_id"), "title": s["title"], "url": s["url"]})
                data = json.dumps({"type": "sources", "sources": src_list}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
        except Exception as e:
            data = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

"""
server.py — FastAPI 后端
"""
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
import zipfile
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from db import (init_db, list_documents, delete_document, export_all, DB_PATH,
                list_folders, create_folder, rename_folder, delete_folder, move_document,
                get_document_by_id, search_documents, find_by_url, rename_document)
from ingest import ingest_url, ingest_text, ingest_pdf, ingest_docx
from query import answer_stream, list_ollama_models, DEFAULT_MODEL
from config import (get_cloud_keys, set_cloud_keys, get_enabled_cloud_models,
                    CLOUD_PROVIDERS, APP_VERSION, UPDATE_CHECK_URL,
                    get_ollama_options, set_ollama_options, OLLAMA_OPTIONS_DEFAULTS)

app = FastAPI()
init_db()

# ── Embedding 模型预热（后台线程，避免首次问答卡顿）────────────────────────────
_embed_ready = [False]

def _prewarm_embed():
    try:
        from ingest import get_embed_model
        get_embed_model()
        _embed_ready[0] = True
        print("[embed] ✓ 预热完成")
    except Exception as e:
        print(f"[embed] 预热失败: {e}")

threading.Thread(target=_prewarm_embed, daemon=True).start()

UI_DIR = Path(__file__).parent / "ui"
UI_PATH = UI_DIR / "index.html"
app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

# PDF 永久存储目录（和数据库同级）
PDF_DIR = DB_PATH.parent / "pdfs"
PDF_DIR.mkdir(exist_ok=True)


def _save_pdf_copy(src: str, original_name: str) -> str:
    """把 PDF 保存到数据目录，返回永久路径"""
    safe_name = f"{int(time.time())}_{Path(original_name).name}"
    dest = PDF_DIR / safe_name
    shutil.copy2(src, dest)
    return str(dest)


@app.get("/", response_class=HTMLResponse)
def index():
    return UI_PATH.read_text(encoding="utf-8")


@app.get("/api/docs")
def get_docs(q: Optional[str] = None):
    if q and q.strip():
        return search_documents(q.strip())
    return list_documents()


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: int):
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@app.delete("/api/docs/{doc_id}")
def del_doc(doc_id: int):
    delete_document(doc_id)
    return {"ok": True}


@app.post("/api/ingest/url")
def api_ingest_url(url: str = Form(...), custom_title: Optional[str] = Form(None), folder_id: Optional[int] = Form(None)):
    # 去重：同一 URL 不重复入库
    existing = find_by_url(url.strip())
    if existing:
        raise HTTPException(status_code=409, detail=f"__duplicate__{existing['id']}__{existing['title']}")
    try:
        return ingest_url(url, custom_title=custom_title or None, folder_id=folder_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/docs/{doc_id}/title")
def api_rename_doc(doc_id: int, title: str = Form(...)):
    if not title.strip():
        raise HTTPException(status_code=400, detail="标题不能为空")
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
    """浏览器文件上传：把 PDF 保存到数据目录再解析"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        if os.path.getsize(tmp.name) == 0:
            raise HTTPException(status_code=400, detail="上传的文件为空，请重新选择。")
        # 永久保存副本
        stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.pdf")
        return ingest_pdf(tmp.name, custom_title=custom_title or None,
                          folder_id=folder_id, stored_url=stored_path)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.post("/api/ingest/pdf-path")
def api_ingest_pdf_path(path: str = Form(...), custom_title: Optional[str] = Form(None), folder_id: Optional[int] = Form(None)):
    """pywebview 原生选择：直接用本地路径，url 存原始路径"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"文件不存在: {path}")
    try:
        return ingest_pdf(path, custom_title=custom_title or None,
                          folder_id=folder_id, stored_url=path)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


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
    """检测 Ollama 是否安装、是否运行"""
    import shutil, requests as req

    running = False
    try:
        r = req.get("http://localhost:11434/api/tags", timeout=2)
        running = r.ok
    except Exception:
        pass

    # 检测安装路径
    installed = bool(shutil.which("ollama"))
    if not installed:
        candidates = [
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            str(Path.home() / "Applications/Ollama.app/Contents/MacOS/Ollama"),
            "/Applications/Ollama.app/Contents/MacOS/Ollama",
        ]
        installed = any(os.path.exists(p) for p in candidates)

    # 检测 Ollama.app
    app_path = None
    for p in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(p):
            app_path = p
            break

    return {"running": running, "installed": installed, "app_path": app_path}


@app.post("/api/ollama-fix")
def ollama_fix():
    """修复 Ollama 并启动（macOS / Windows）"""
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
            return {"ok": False, "error": "找不到 Ollama，请先安装"}
        subprocess.Popen([str(ollama_exe)], creationflags=subprocess.CREATE_NO_WINDOW)
        return {"ok": True}

    # macOS
    app_path = None
    for p in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(p):
            app_path = p
            break
    if not app_path:
        return {"ok": False, "error": "找不到 Ollama.app"}

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
    """触发 Ollama 启动（不阻塞等待，由前端轮询状态）"""
    import platform, shutil, subprocess, requests as req

    # 已经在跑了
    try:
        if req.get("http://localhost:11434/api/tags", timeout=2).ok:
            return {"ok": True, "already_running": True}
    except Exception:
        pass

    if platform.system() == "Windows":
        # 优先用 "Ollama app.exe"（带托盘），其次 ollama.exe serve
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
        return {"ok": False, "error": "Ollama 未安装"}

    # macOS：找到 Ollama.app 就用 open 打开
    for app_dir in [str(Path.home() / "Applications/Ollama.app"), "/Applications/Ollama.app"]:
        if os.path.exists(app_dir):
            subprocess.Popen(["open", app_dir])
            return {"ok": True, "launched": True}

    # 没有 .app，尝试 ollama serve
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        subprocess.Popen([ollama_bin, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "launched": True}

    return {"ok": False, "error": "Ollama 未安装"}


@app.get("/api/ollama-install")
def ollama_install():
    """流式下载并安装 Ollama（支持 macOS 和 Windows）"""
    import platform, zipfile, tempfile, subprocess, requests as req, time

    def generate():
        try:
            system = platform.system()
            if system not in ("Darwin", "Windows"):
                yield f'data: {json.dumps({"status":"error","error":"仅支持 macOS 和 Windows","done":True})}\n\n'
                return

            # ── Windows：直接跳官网，用户自行安装 ───────────────────────────────
            if system == "Windows":
                import webbrowser
                webbrowser.open("https://ollama.com")
                yield f'data: {json.dumps({"status":"error","error":"请在浏览器中下载并安装 Ollama，完成后重启 Rivus 即可使用本地模型","done":True})}\n\n'
                return

            # ── macOS 安装路径 ────────────────────────────────────────────────
            url = "https://ollama.com/download/Ollama-darwin.zip"
            yield f'data: {json.dumps({"status":"正在下载 Ollama...","pct":0})}\n\n'

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
                                yield f'data: {json.dumps({"status":f"正在下载 Ollama... {mb} MB","pct":pct})}\n\n'

                yield f'data: {json.dumps({"status":"正在解压...","pct":72})}\n\n'

                extract_dir = tempfile.mkdtemp()
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(extract_dir)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            # 找 Ollama.app
            app_src = None
            for root, dirs, _ in os.walk(extract_dir):
                for d in dirs:
                    if d == "Ollama.app":
                        app_src = os.path.join(root, d)
                        break
                if app_src:
                    break

            if not app_src:
                raise FileNotFoundError("解压后未找到 Ollama.app")

            yield f'data: {json.dumps({"status":"正在安装...","pct":85})}\n\n'

            # 安装到 ~/Applications（不需要管理员权限）
            apps_dir = Path.home() / "Applications"
            apps_dir.mkdir(exist_ok=True)
            dest = apps_dir / "Ollama.app"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(app_src, str(dest))
            shutil.rmtree(extract_dir, ignore_errors=True)

            # 修复可执行权限（zipfile 解压不保留 +x）
            import stat as _stat
            for root, dirs, files in os.walk(str(dest)):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        cur = os.stat(fp).st_mode
                        os.chmod(fp, cur | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
                    except Exception:
                        pass
            # 移除隔离属性（防止 Gatekeeper 拦截）
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
    """返回系统内存（GB）+ 已安装 LLM 模型列表（过滤 embedding 模型）"""
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
    # 过滤 embedding 模型（nomic 系列等非对话模型）
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
    """流式拉取 Ollama 模型，返回进度 SSE"""
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
    """删除本地 Ollama 模型"""
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
    """返回当前云端配置（隐藏 key 中间部分）"""
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
    保存云端 API Keys。
    payload: { provider: { api_key?: str, enabled?: bool } }
    api_key 为空字符串表示不更新（保留原值）。
    """
    current = get_cloud_keys()
    for provider, data in payload.items():
        if provider not in CLOUD_PROVIDERS:
            continue
        entry = current.setdefault(provider, {})
        if data.get("api_key"):          # 非空才覆盖
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


@app.get("/api/models")
def get_models():
    # 本地 Ollama 模型
    local = list_ollama_models()
    chat_local = [m for m in local if not any(
        m.lower().startswith(p) for p in ("nomic", "all-minilm", "mxbai", "bge", "snowflake")
    )]
    # 云端模型（已配置 key）
    cloud = get_enabled_cloud_models()
    all_models = (
        [{"id": m, "label": m} for m in chat_local]
        + [{"id": m["id"], "label": m["label"]} for m in cloud]
    )
    default_id = (
        chat_local[0] if chat_local
        else (cloud[0]["id"] if cloud else DEFAULT_MODEL)
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
            r = req.get(UPDATE_CHECK_URL, timeout=4,
                        headers={"User-Agent": "Rivus-App"})
            data = r.json()
            latest = data.get("tag_name", "").lstrip("v")
            if latest and latest != APP_VERSION:
                result["latest"] = latest
                result["update_available"] = True
                result["url"] = data.get("html_url", "")
        except Exception:
            pass
    return result


@app.get("/api/export")
def export_backup(save_path: Optional[str] = None):
    """导出知识库：打包 zip 到用户指定路径"""
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"rivus-backup-{date_str}.zip"

    if save_path:
        out_path = Path(save_path)
        # 确保有 .zip 后缀
        if out_path.suffix.lower() != ".zip":
            out_path = out_path.with_suffix(".zip")
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # 未传路径时回退到 Downloads
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
    """从备份 zip 恢复知识库（替换现有数据库和 PDF）"""
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=400, detail=f"文件不存在: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise HTTPException(status_code=400, detail="不是有效的 zip 文件")

    # 解压到临时目录验证内容
    tmp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if "rivus.db" not in names:
                raise HTTPException(status_code=400, detail="备份文件中未找到 rivus.db，请确认文件来源")
            zf.extractall(tmp_dir)

        # 备份当前数据库
        db_src = Path(tmp_dir) / "rivus.db"
        if DB_PATH.exists():
            shutil.copy2(DB_PATH, str(DB_PATH) + ".bak")
        shutil.copy2(db_src, DB_PATH)

        # 恢复 PDF 文件
        pdf_src_dir = Path(tmp_dir) / "pdfs"
        if pdf_src_dir.exists():
            PDF_DIR.mkdir(exist_ok=True)
            for f in pdf_src_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, PDF_DIR / f.name)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"ok": True, "message": "备份已恢复，请重启应用以加载最新数据。"}


@app.post("/api/ingest/docx")
def api_ingest_docx(file: UploadFile = File(...),
                    custom_title: Optional[str] = Form(None),
                    folder_id: Optional[int] = Form(None)):
    """浏览器上传 .docx 文件"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        if os.path.getsize(tmp.name) == 0:
            raise HTTPException(status_code=400, detail="上传的文件为空，请重新选择。")
        stored_path = _save_pdf_copy(tmp.name, file.filename or "upload.docx")
        return ingest_docx(tmp.name, custom_title=custom_title or None,
                           folder_id=folder_id, stored_url=stored_path)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.post("/api/ingest/docx-path")
def api_ingest_docx_path(path: str = Form(...),
                         custom_title: Optional[str] = Form(None),
                         folder_id: Optional[int] = Form(None)):
    """pywebview 原生选择 .docx 文件"""
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"文件不存在: {path}")
    try:
        return ingest_docx(path, custom_title=custom_title or None,
                           folder_id=folder_id, stored_url=path)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


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
                        src_list.append({"title": s["title"], "url": s["url"]})
                data = json.dumps({"type": "sources", "sources": src_list}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
        except Exception as e:
            data = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

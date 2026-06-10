"""
ingest.py — 内容解析 + 切块 + Embedding + 入库
"""
import re
from pathlib import Path
from typing import Optional

import requests
from readability import Document  # readability-lxml
from sentence_transformers import SentenceTransformer

import threading
from db import init_db, insert_document, insert_chunk, update_summary

# ── 全局单例 embedding 模型 ───────────────────────────────────────────────────
_model: Optional[SentenceTransformer] = None

def get_embed_model() -> SentenceTransformer:
    global _model
    if _model is None:
        print("[ingest] 加载 embedding 模型 BAAI/bge-m3 ...")
        _model = SentenceTransformer("BAAI/bge-m3")
    return _model


# ── 文本切块（段落感知）────────────────────────────────────────────────────────
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

def chunk_text(text: str) -> list[str]:
    """
    按段落切块：尽量在段落边界断开，保持语义完整。
    每块不超过 CHUNK_SIZE 字符，相邻块重叠 CHUNK_OVERLAP 字符。
    """
    # 清理过多空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # 先按段落（双换行）分割
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]

    chunks = []
    current = ""

    for para in paragraphs:
        # 单段超长则强制按字符切
        if len(para) > CHUNK_SIZE:
            if current:
                chunks.append(current.strip())
                current = ""
            # 按句子边界尝试切（。！？.!?）
            sentences = re.split(r"(?<=[。！？.!?])\s*", para)
            buf = ""
            for sent in sentences:
                if len(buf) + len(sent) > CHUNK_SIZE and buf:
                    chunks.append(buf.strip())
                    # 重叠：保留最后 CHUNK_OVERLAP 字符
                    buf = buf[-CHUNK_OVERLAP:] + sent
                else:
                    buf += sent
            if buf.strip():
                chunks.append(buf.strip())
            continue

        # 加段落后超长 → 先保存当前块，再开新块（带重叠）
        if current and len(current) + len(para) + 2 > CHUNK_SIZE:
            chunks.append(current.strip())
            # 重叠：从上一块末尾取部分内容
            overlap = current[-CHUNK_OVERLAP:].strip()
            current = overlap + "\n\n" + para if overlap else para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


# ── URL 解析 ──────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def parse_url(url: str) -> tuple[str, str, str]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    doc = Document(resp.text)
    title = doc.title() or url
    html_content = doc.summary()
    plain = re.sub(r"<[^>]+>", " ", html_content)
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    from urllib.parse import urlparse
    source = urlparse(url).netloc or url
    return title, plain, source


# ── PDF 解析 ──────────────────────────────────────────────────────────────────

def _clean_pdf_text(text: str) -> str:
    """清理 PDF 提取的常见乱码/格式问题"""
    # 合并被断行的单词（英文连字符换行）
    text = re.sub(r"-\n([a-z])", r"\1", text)
    # 单个换行变空格（段内换行），双换行保留（段落）
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    # 多余空格
    text = re.sub(r"[ \t]+", " ", text)
    # 多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(file_path: str) -> tuple[str, str, str]:
    """
    解析 PDF，优先用 pymupdf（质量更好），退回 pypdf。
    返回 (title, text, source)
    """
    # ── 方案一：pymupdf ───────────────────────────────────────────────────────
    try:
        import fitz  # pymupdf
        doc = fitz.open(file_path)

        # 从元数据取标题，否则用文件名
        meta_title = (doc.metadata or {}).get("title", "").strip()
        title = meta_title or Path(file_path).stem

        pages = []
        for page in doc:
            # sort=True 按阅读顺序排列文字块，对多栏 PDF 效果好很多
            page_text = page.get_text("text", sort=True)
            if page_text.strip():
                pages.append(page_text)

        full_text = _clean_pdf_text("\n\n".join(pages))

        if len(full_text) < 50:
            raise ValueError("扫描版 PDF（图片），无法提取文字。请使用支持 OCR 的工具先转换。")

        return title, full_text, "PDF"

    except ImportError:
        pass  # pymupdf 未安装，退回 pypdf

    # ── 方案二：pypdf（备用）─────────────────────────────────────────────────
    try:
        import pypdf
        reader = pypdf.PdfReader(file_path)
        meta = reader.metadata or {}
        title = (meta.get("/Title") or "").strip() or Path(file_path).stem
        pages = [page.extract_text() or "" for page in reader.pages]
        full_text = _clean_pdf_text("\n\n".join(pages))
        if not full_text:
            raise ValueError("PDF 内容为空，可能是扫描版或加密文件。")
        return title, full_text, "PDF"
    except ImportError:
        raise ImportError("请安装 pymupdf: pip install pymupdf")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def ingest_url(url: str, custom_title: str = None, folder_id: int = None) -> dict:
    init_db()
    print(f"[ingest] 正在抓取: {url}")
    title, text, source = parse_url(url)
    if custom_title:
        title = custom_title
    return _ingest_text(url=url, title=title, text=text, source=source, folder_id=folder_id)


def ingest_text(title: str, text: str, source: str = "手动输入", folder_id: int = None) -> dict:
    init_db()
    return _ingest_text(url="", title=title, text=text, source=source, folder_id=folder_id)


def ingest_docx(file_path: str, custom_title: str = None, folder_id: int = None, stored_url: str = "") -> dict:
    """解析 Word (.docx) 文档"""
    init_db()
    try:
        from docx import Document as DocxDocument
    except ImportError:
        raise ImportError("python-docx 未安装，请运行: pip install python-docx")

    doc = DocxDocument(file_path)
    title = custom_title or Path(file_path).stem

    # 提取段落 + 表格文本
    parts = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            parts.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append("  ".join(cells))

    full_text = "\n\n".join(parts)
    if not full_text.strip():
        raise ValueError("Word 文档内容为空，请检查文件是否正常。")

    return _ingest_text(url=stored_url or file_path, title=title, text=full_text, source="Word", folder_id=folder_id)


def ingest_pdf(file_path: str, custom_title: str = None, folder_id: int = None, stored_url: str = "") -> dict:
    init_db()
    title, text, source = parse_pdf(file_path)
    if custom_title:
        title = custom_title
    # stored_url 是永久路径（用于"用默认应用打开"），空字符串表示无法定位原文件
    return _ingest_text(url=stored_url, title=title, text=text, source=source, folder_id=folder_id)


def _ingest_text(url: str, title: str, text: str, source: str, folder_id: int = None) -> dict:
    if not text.strip():
        raise ValueError("提取到的正文为空，请检查链接或内容。")

    chunks = chunk_text(text)
    print(f"[ingest] 《{title}》切成 {len(chunks)} 块")

    model = get_embed_model()
    embeddings = model.encode(chunks, show_progress_bar=False, normalize_embeddings=True).tolist()

    doc_id = insert_document(url=url, title=title, source=source, full_text=text, folder_id=folder_id)
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        insert_chunk(doc_id=doc_id, chunk_index=i, text=chunk, embedding=emb)

    print(f"[ingest] ✓ 已存入，doc_id={doc_id}")

    # 后台异步生成摘要（不阻塞用户操作）
    threading.Thread(
        target=_enrich_background,
        args=(doc_id, title, text),
        daemon=True
    ).start()

    return {
        "doc_id": doc_id,
        "title": title,
        "source": source,
        "chunks": len(chunks),
        "chars": len(text),
    }


def _enrich_background(doc_id: int, title: str, text: str):
    """后台调用 Ollama 提炼文档摘要和关键信息"""
    try:
        import requests as req
        from query import DEFAULT_MODEL

        # 取前 4000 字（节省推理时间，基本够覆盖核心内容）
        excerpt = text[:4000]
        prompt = f"""Analyze the following document and extract key information.

Document title: {title}

Document content:
{excerpt}

Output the following (reply in the same language as the document):
[Summary] 2-3 sentences summarizing the core content
[Key Points] 3-5 most important points (one per line, starting with "-")
[Keywords] 3-6 keywords, comma-separated"""

        resp = req.post(
            "http://localhost:11434/api/chat",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=120,
        )
        if resp.ok:
            summary = resp.json()["message"]["content"].strip()
            update_summary(doc_id, summary)
            print(f"[enrich] ✓ doc_id={doc_id} 摘要已生成")
        else:
            print(f"[enrich] Ollama 返回错误: {resp.status_code}")
    except Exception as e:
        print(f"[enrich] 摘要生成失败 doc_id={doc_id}: {e}")

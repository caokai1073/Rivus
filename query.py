"""
query.py — RAG 检索 + 本地 Ollama / 云端 LLM 问答
"""
import json
import re
import time
import requests
from typing import Iterator

from db import vector_search, fts_search, get_document_summaries, get_document_by_id, list_documents, list_folders
from ingest import get_embed_model, chunk_text
from config import get_ollama_options

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:9b"
TOP_K = 8


# ── Ollama ────────────────────────────────────────────────────────────────────

def list_ollama_models() -> list[str]:
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def ollama_chat_stream(model: str, prompt: str) -> Iterator[str]:
    # 先确认 Ollama 可用且模型已下载
    available = list_ollama_models()
    if available is None or (isinstance(available, list) and len(available) == 0):
        yield "[错误] Ollama 未检测到任何本地模型，请先在「设置 → 本地模型」中下载一个模型。"
        return
    base = model.split(":")[0]
    if not any(m == model or m.split(":")[0] == base for m in available):
        names = "、".join(available[:5])
        yield f"[错误] 模型「{model}」未找到。已下载的模型：{names}。请在顶部下拉框切换到已有模型，或前往设置下载该模型。"
        return

    ollama_opts = get_ollama_options()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "options": {k: v for k, v in ollama_opts.items() if v != -1 or k != "num_predict"},
    }
    for attempt in range(2):   # 最多重试一次
        try:
            resp = requests.post(
                f"{OLLAMA_BASE}/api/chat",
                json=payload, stream=True, timeout=180,
            )
        except requests.exceptions.ConnectionError:
            yield "[错误] 无法连接到 Ollama，请确认 Ollama 已启动。"
            return
        except requests.exceptions.Timeout:
            yield "[错误] Ollama 响应超时，模型可能正在加载，请稍后重试。"
            return

        if resp.status_code == 500 and attempt == 0:
            # query 扩写可能打断了上一次请求，等一下再试
            time.sleep(3)
            continue
        if not resp.ok:
            try:
                err_body = resp.json().get("error", resp.text[:300])
            except Exception:
                err_body = resp.text[:300]
            yield f"[错误] Ollama 返回 {resp.status_code}：{err_body}"
            return
        break
    else:
        yield "[错误] Ollama 连续返回错误，请检查模型是否正常加载。"
        return

    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in data:
                yield f"\n\n[错误] Ollama 生成中断：{data['error']}"
                return
            token = data.get("message", {}).get("content", "")
            if token:
                yield token
            if data.get("done"):
                break
    except requests.exceptions.ConnectionError:
        yield "[错误] 无法连接到 Ollama，请确认 Ollama 已启动。"
    except requests.exceptions.Timeout:
        yield "[错误] Ollama 响应超时，模型可能正在加载，请稍后重试。"


# ── 云端 LLM ──────────────────────────────────────────────────────────────────

def _openai_compat_stream(base_url: str, api_key: str, model: str, prompt: str) -> Iterator[str]:
    """OpenAI 兼容接口（DeepSeek / OpenAI）流式调用"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    with requests.post(
        f"{base_url}/chat/completions",
        headers=headers, json=payload, stream=True, timeout=120,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            if text.startswith("data:"):
                text = text[5:].strip()
            if text == "[DONE]":
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            token = (data.get("choices") or [{}])[0].get("delta", {}).get("content", "")
            if token:
                yield token


def _anthropic_stream(api_key: str, model: str, prompt: str) -> Iterator[str]:
    """Anthropic Claude 流式调用"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    with requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers, json=payload, stream=True, timeout=120,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            if text.startswith("data:"):
                text = text[5:].strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "content_block_delta":
                token = data.get("delta", {}).get("text", "")
                if token:
                    yield token


def cloud_chat_stream(provider: str, model: str, api_key: str, prompt: str) -> Iterator[str]:
    """统一云端入口，按 provider 路由"""
    from config import CLOUD_PROVIDERS
    if not api_key:
        yield f"[错误] {provider} 的 API Key 未配置。"
        return
    if provider in ("deepseek", "openai", "minimax", "glm"):
        base_url = CLOUD_PROVIDERS[provider]["base_url"]
        yield from _openai_compat_stream(base_url, api_key, model, prompt)
    elif provider == "anthropic":
        yield from _anthropic_stream(api_key, model, prompt)
    else:
        yield f"[错误] 未知云端提供商：{provider}"


# ── Query 扩写 ────────────────────────────────────────────────────────────────

def _expand_query(question: str, model: str) -> list[str]:
    """
    用本地 Ollama 生成 2 个等价问题变体，扩大检索覆盖面。
    失败时静默降级，只返回原问题。
    云端模型跳过（不需要本地调用）。
    """
    if model.startswith("cloud:"):
        return [question]
    try:
        prompt = (
            "Rephrase the following question in 2 different ways for document retrieval. "
            "Use different keywords but preserve the meaning. "
            "Return ONLY the 2 rephrased questions, one per line, no numbering.\n\n"
            f"Question: {question}\n\nRephrased:"
        )
        ollama_opts = get_ollama_options()
        resp = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False,
                  "think": False,
                  "options": {"num_ctx": ollama_opts.get("num_ctx", 4096)}},
            timeout=30,
        )
        if resp.ok:
            lines = [l.strip() for l in resp.json()["message"]["content"].split("\n")
                     if l.strip()][:2]
            return [question] + lines
    except Exception:
        pass
    return [question]


# ── RRF 融合 ──────────────────────────────────────────────────────────────────

def _rrf_merge(result_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """
    Reciprocal Rank Fusion：合并多个排名列表，score = Σ 1/(k+rank)。
    以 chunk_id 去重，保留每个 chunk 最早出现的完整数据。
    """
    scores: dict[int, float] = {}
    best: dict[int, dict] = {}
    for results in result_lists:
        for rank, r in enumerate(results):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in best:
                best[cid] = r
    return [best[cid] for cid in sorted(scores, key=lambda x: -scores[x])]


# ── RAG prompt 构建 ───────────────────────────────────────────────────────────

def _build_inventory() -> str:
    """生成知识库目录字符串，供 prompt 使用。"""
    try:
        docs    = list_documents()
        folders = {f["id"]: f["name"] for f in list_folders()}
        if not docs:
            return ""
        # 按文件夹分组
        groups: dict[str, list[str]] = {}
        for d in docs:
            folder_name = folders.get(d["folder_id"], "未分类") if d["folder_id"] else "未分类"
            groups.setdefault(folder_name, []).append(d["title"])
        lines = []
        for folder_name, titles in groups.items():
            lines.append(f"[{folder_name}] ({len(titles)} items)")
            for title in titles:
                lines.append(f"  - {title}")
        return "[Knowledge Base Inventory]\n" + "\n".join(lines)
    except Exception:
        return ""


def _question_lang(text: str) -> str:
    """粗略判断问题语言：CJK 字符占比 >15% 视为中文，否则为英文。"""
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    return 'zh' if cjk / max(len(text), 1) > 0.15 else 'en'


def build_prompt(
    question: str,
    chunks: list[dict],
    summaries: dict[int, str],
    pinned_title: str = None,
) -> str:
    doc_order: list[int] = []
    doc_meta: dict[int, dict] = {}
    doc_chunks: dict[int, list[str]] = {}

    for c in chunks:
        did = c["doc_id"]
        if did not in doc_meta:
            doc_order.append(did)
            doc_meta[did] = {"title": c.get("title", "未知"), "url": c.get("url", "")}
            doc_chunks[did] = []
        doc_chunks[did].append(c["chunk_text"])

    doc_num: dict[int, int] = {did: i + 1 for i, did in enumerate(doc_order)}

    summary_parts = []
    for did in doc_order:
        s = summaries.get(did)
        if s:
            summary_parts.append(f"[来源{doc_num[did]}]《{doc_meta[did]['title']}》\n{s}")

    summary_block = (
        "【文档摘要】\n" + "\n\n".join(summary_parts) + "\n\n"
        if summary_parts else ""
    )

    context_parts = []
    for did in doc_order:
        title = doc_meta[did]["title"]
        url   = doc_meta[did]["url"]
        ref   = f"[来源{doc_num[did]}]《{title}》" + (f" ({url})" if url else "")
        merged = "\n\n".join(doc_chunks[did])
        context_parts.append(f"{ref}\n{merged}")

    context = "\n\n---\n\n".join(context_parts)

    q_lang = _question_lang(question)
    if q_lang == 'zh':
        lang_rule = "用中文回答。"
    else:
        lang_rule = "Reply in English. Do NOT switch to Chinese even if the source documents are in Chinese."

    if pinned_title:
        instruction = f'You are a personal knowledge base assistant. The user is referencing the document "{pinned_title}". Focus on that document to answer the question.'
        rules = (
            f"Rules:\n"
            f"1. Answer only based on the provided document content. Do not fabricate.\n"
            f"2. {lang_rule}"
        )
    else:
        instruction = "You are a personal knowledge base assistant. Answer the user's question based on the content below."
        rules = (
            f"Rules:\n"
            f"1. Answer only using the provided content. Do not fabricate.\n"
            f"2. After each key piece of information, cite the source number, e.g. (Source 1).\n"
            f"3. If the content is insufficient to answer, say so briefly.\n"
            f"4. {lang_rule}"
        )

    inventory = _build_inventory()
    inventory_block = f"{inventory}\n\n" if inventory else ""

    return (
        f"{instruction}\n\n{rules}\n\n"
        f"{inventory_block}{summary_block}[Document Excerpts]\n{context}\n\n"
        f"[User Question]\n{question}\n\n[Your Answer]"
    )


# ── 主入口 ────────────────────────────────────────────────────────────────────

def answer_stream(
    question: str,
    model: str = DEFAULT_MODEL,
    pinned_doc_id: int = None,
) -> tuple[Iterator[str], list[dict]]:
    """
    执行完整 RAG 流程，返回 (token_stream, source_chips)。
    model 格式：
      - 本地 Ollama：  "qwen3.5:9b"
      - 云端：         "cloud:deepseek:deepseek-chat"
    """
    # ── 引用模式：强制读取指定文档全文 ──
    if pinned_doc_id is not None:
        doc = get_document_by_id(pinned_doc_id)
        if not doc:
            def _notfound():
                yield "找不到该文档，可能已被删除。"
            return _notfound(), []
        full_text = doc.get("full_text") or ""
        text_chunks = chunk_text(full_text) if full_text else [full_text]
        chunks = [
            {"doc_id": doc["id"], "title": doc["title"],
             "url": doc.get("url", ""), "chunk_text": c}
            for c in text_chunks
        ]
        summaries = get_document_summaries([doc["id"]])
        prompt = build_prompt(question, chunks, summaries, pinned_title=doc["title"])
        source_chips = [{"title": doc["title"], "url": doc.get("url", "")}]
        stream = _dispatch_stream(model, prompt)
        return stream, source_chips

    # ── 常规 RAG 模式（Query 扩写 + 混合检索 + RRF）──
    # 1. Query 扩写
    questions = _expand_query(question, model)

    # 2. 向量检索（每个变体各搜一次）
    emb_model = get_embed_model()
    vec_result_lists = []
    for q in questions:
        qvec = emb_model.encode([q], normalize_embeddings=True)[0].tolist()
        vec_result_lists.append(vector_search(qvec, top_k=TOP_K))

    # 3. BM25 关键词检索（用原始问题）
    fts_results = fts_search(question, top_k=TOP_K)

    # 4. RRF 融合，取前 TOP_K
    merged = _rrf_merge(vec_result_lists + [fts_results])[:TOP_K]
    chunks = merged if merged else (vec_result_lists[0] if vec_result_lists[0] else [])

    if not chunks:
        def _empty():
            yield "我的收藏中还没有任何内容，请先添加一些收藏。"
        return _empty(), []

    doc_ids = list({c["doc_id"] for c in chunks})
    summaries = get_document_summaries(doc_ids)
    prompt = build_prompt(question, chunks, summaries)
    return _dispatch_stream(model, prompt), chunks


def _dispatch_stream(model: str, prompt: str) -> Iterator[str]:
    """根据 model 字符串路由到本地或云端"""
    if model.startswith("cloud:"):
        parts = model.split(":", 2)          # ["cloud", "deepseek", "deepseek-chat"]
        if len(parts) != 3:
            yield f"[错误] 无效的云端模型格式：{model}"
            return
        _, provider, model_name = parts
        from config import get_cloud_keys
        api_key = get_cloud_keys().get(provider, {}).get("api_key", "")
        yield from cloud_chat_stream(provider, model_name, api_key, prompt)
    else:
        yield from ollama_chat_stream(model, prompt)

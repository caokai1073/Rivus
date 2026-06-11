"""
query.py — RAG retrieval + local Ollama / cloud LLM question answering

Unified Tool-Use Agent architecture:
  - Local Ollama  → Tool-Use Agent (requires Ollama >= 0.3)
  - Cloud models  → Tool-Use Agent (search / get_document / list_documents tool loop)
  - Pinned doc mode (pinned_doc_id) → Cloud: full-text prompt; Local: Agent with doc title prefix
"""
import json
import re
import time
import requests
from typing import Iterator

from db import vector_search, fts_search, get_document_summaries, get_document_by_id, list_documents, list_folders
from ingest import get_embed_model, chunk_text
from config import get_ollama_options

OLLAMA_BASE = "http://localhost:11434"   # local default; overridden by _ollama_base() when SSH tunnel is active
DEFAULT_MODEL = "qwen3.5:9b"
TOP_K = 8


def _ollama_base() -> str:
    """
    Returns the Ollama base URL to use.
    - When a remote SSH tunnel is active → tunnel local port (e.g. http://localhost:11435)
    - Otherwise → local Ollama (http://localhost:11434)
    """
    try:
        import remote
        b = remote.get_base()
        if b:
            return b
    except ImportError:
        pass
    return OLLAMA_BASE


# ── Agent tool definitions ────────────────────────────────────────────────────

AGENT_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search the personal knowledge base for relevant document chunks. "
                "Use this to find information related to a specific topic or question. "
                "You can call this multiple times with different queries to broaden coverage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query. Use specific keywords relevant to what you're looking for.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": "Retrieve the full content of a specific document by its title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The document title or a partial title to match.",
                    }
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List all documents in the knowledge base to understand what's available before searching.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

AGENT_TOOLS_ANTHROPIC = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the personal knowledge base for relevant document chunks. "
            "Use this to find information related to a specific topic or question. "
            "You can call this multiple times with different queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": "Retrieve the full content of a specific document by its title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title to match."}
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_documents",
        "description": "List all documents in the knowledge base.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ── Ollama ────────────────────────────────────────────────────────────────────

def list_ollama_models() -> list[str]:
    """List local Ollama models (always queries localhost, not affected by SSH tunnel)."""
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def _resolve_ollama_model(model: str) -> tuple[str, str]:
    """
    Resolve a model name, returning (actual_model_name, base_url).
    - "remote:qwen3.6:27b" → ("qwen3.6:27b", tunnel_url)
    - "qwen3.5:9b"         → ("qwen3.5:9b",  localhost:11434)
    """
    if model.startswith("remote:"):
        actual = model[len("remote:"):]
        base = _ollama_base()   # returns tunnel URL
        return actual, base
    return model, OLLAMA_BASE


def ollama_chat_stream(model: str, prompt: str) -> Iterator[str]:
    actual_model, base = _resolve_ollama_model(model)

    # Check model availability
    try:
        resp_tags = requests.get(f"{base}/api/tags", timeout=5)
        available = [m["name"] for m in resp_tags.json().get("models", [])] if resp_tags.ok else []
    except Exception:
        available = []

    if not available:
        source = "remote server" if model.startswith("remote:") else "local"
        yield f"[Error] No models detected on {source} Ollama."
        return
    name_base = actual_model.split(":")[0]
    if not any(m == actual_model or m.split(":")[0] == name_base for m in available):
        names = ", ".join(available[:5])
        yield f"[Error] Model '{actual_model}' not found. Available: {names}."
        return

    ollama_opts = get_ollama_options()
    payload = {
        "model": actual_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "options": ollama_opts,
    }
    for attempt in range(2):   # retry once on 500
        try:
            resp = requests.post(
                f"{base}/api/chat",
                json=payload, stream=True, timeout=180,
            )
        except requests.exceptions.ConnectionError:
            yield "[Error] Cannot connect to Ollama. Please make sure Ollama is running."
            return
        except requests.exceptions.Timeout:
            yield "[Error] Ollama timed out. The model may still be loading; please try again."
            return

        if resp.status_code == 500 and attempt == 0:
            # A previous interrupted request may have caused this; wait and retry
            time.sleep(3)
            continue
        if not resp.ok:
            try:
                err_body = resp.json().get("error", resp.text[:300])
            except Exception:
                err_body = resp.text[:300]
            yield f"[Error] Ollama returned {resp.status_code}: {err_body}"
            return
        break
    else:
        yield "[Error] Ollama returned consecutive errors. Please check if the model loaded correctly."
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
                yield f"\n\n[Error] Ollama generation interrupted: {data['error']}"
                return
            token = data.get("message", {}).get("content", "")
            if token:
                yield token
            if data.get("done"):
                break
    except requests.exceptions.ConnectionError:
        yield "[Error] Cannot connect to Ollama. Please make sure Ollama is running."
    except requests.exceptions.Timeout:
        yield "[Error] Ollama timed out. The model may still be loading; please try again."


# ── Cloud LLM ─────────────────────────────────────────────────────────────────

def _openai_compat_stream(base_url: str, api_key: str, model: str, prompt: str) -> Iterator[str]:
    """OpenAI-compatible streaming API (DeepSeek / OpenAI / GLM / MiniMax)"""
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
    """Anthropic Claude streaming API"""
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
    """Unified cloud entry point, routes by provider"""
    from config import CLOUD_PROVIDERS
    if not api_key:
        yield f"[Error] API key for {provider} is not configured."
        return
    if provider in ("deepseek", "openai", "minimax", "glm"):
        base_url = CLOUD_PROVIDERS[provider]["base_url"]
        yield from _openai_compat_stream(base_url, api_key, model, prompt)
    elif provider == "anthropic":
        yield from _anthropic_stream(api_key, model, prompt)
    else:
        yield f"[Error] Unknown cloud provider: {provider}"


# ── Tool execution layer ──────────────────────────────────────────────────────

def _run_tool_with_chips(name: str, args: dict) -> tuple[str, list[dict]]:
    """
    Execute an Agent tool, returns (result_text, source_chips).
    result_text is appended to messages for the next model turn;
    source_chips are used by the frontend to display citation badges.
    """
    if name == "search_knowledge_base":
        query = args.get("query", "").strip()
        if not query:
            return "No query provided.", []
        emb_model = get_embed_model()
        qvec = emb_model.encode([query], normalize_embeddings=True)[0].tolist()
        vec_results = vector_search(qvec, top_k=5)
        fts_results = fts_search(query, top_k=5)
        merged = _rrf_merge([vec_results, fts_results])[:5]
        if not merged:
            return "No results found for this query.", []
        parts, chips, seen = [], [], set()
        for r in merged:
            parts.append(f"[{r.get('title', 'Unknown')}]\n{r['chunk_text']}")
            key = r.get("title", "")
            if key not in seen:
                seen.add(key)
                chips.append({"doc_id": r.get("doc_id"), "title": r.get("title", ""), "url": r.get("url", "")})
        return "\n\n---\n\n".join(parts), chips

    elif name == "get_document":
        title = args.get("title", "").strip()
        if not title:
            return "No title provided.", []
        docs = list_documents()
        for doc in docs:
            if title.lower() in doc["title"].lower():
                full_doc = get_document_by_id(doc["id"])
                if full_doc:
                    text = (full_doc.get("full_text") or "")[:6000]
                    chip = {"doc_id": doc["id"], "title": doc["title"], "url": doc.get("url", "")}
                    return f"[{doc['title']}]\n{text}", [chip]
        return f"Document matching '{title}' not found.", []

    elif name == "list_documents":
        inventory = _build_inventory()
        return (inventory or "No documents in knowledge base."), []

    return f"Unknown tool: {name}", []


# ── RRF fusion ────────────────────────────────────────────────────────────────

def _rrf_merge(result_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """
    Reciprocal Rank Fusion: merges multiple ranked lists, score = Σ 1/(k+rank).
    Deduplicates by chunk_id, retaining the first-seen full record for each chunk.
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


# ── RAG prompt construction ───────────────────────────────────────────────────

def _build_inventory() -> str:
    """Build a knowledge base directory string for use in prompts."""
    try:
        docs    = list_documents()
        folders = {f["id"]: f["name"] for f in list_folders()}
        if not docs:
            return ""
        # Group by folder
        groups: dict[str, list[str]] = {}
        for d in docs:
            folder_name = folders.get(d["folder_id"], "Uncategorized") if d["folder_id"] else "Uncategorized"
            source = d.get("source") or "Unknown"
            groups.setdefault(folder_name, []).append(f"{d['title']} [{source}]")
        lines = []
        for folder_name, entries in groups.items():
            lines.append(f"[{folder_name}] ({len(entries)} items)")
            for entry in entries:
                lines.append(f"  - {entry}")
        return "[Knowledge Base Inventory]\n" + "\n".join(lines)
    except Exception:
        return ""


def _question_lang(text: str) -> str:
    """Rough language detection: CJK character ratio > 15% → Chinese, otherwise English."""
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
            doc_meta[did] = {"title": c.get("title", "Unknown"), "url": c.get("url", "")}
            doc_chunks[did] = []
        doc_chunks[did].append(c["chunk_text"])

    doc_num: dict[int, int] = {did: i + 1 for i, did in enumerate(doc_order)}

    summary_parts = []
    for did in doc_order:
        s = summaries.get(did)
        if s:
            summary_parts.append(f"[Source {doc_num[did]}] {doc_meta[did]['title']}\n{s}")

    summary_block = (
        "[Document Summaries]\n" + "\n\n".join(summary_parts) + "\n\n"
        if summary_parts else ""
    )

    context_parts = []
    for did in doc_order:
        title = doc_meta[did]["title"]
        url   = doc_meta[did]["url"]
        ref   = f"[Source {doc_num[did]}] {title}" + (f" ({url})" if url else "")
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


# ── Ollama Tool-Use Agent ─────────────────────────────────────────────────────

def _ollama_agent_stream(
    model: str,
    question: str,
    source_chips: list,
) -> Iterator[str]:
    """
    Ollama Tool-Use Agent loop (requires Ollama >= 0.3).
    Tool-call phase: non-streaming; final answer phase: streaming.
    """
    actual_model, base = _resolve_ollama_model(model)

    # Check model availability
    try:
        resp_tags = requests.get(f"{base}/api/tags", timeout=5)
        available = [m["name"] for m in resp_tags.json().get("models", [])] if resp_tags.ok else []
    except Exception:
        available = []

    if not available:
        source = "remote server" if model.startswith("remote:") else "local"
        yield f"[Error] No models detected on {source} Ollama."
        return
    name_base = actual_model.split(":")[0]
    if not any(m == actual_model or m.split(":")[0] == name_base for m in available):
        names = ", ".join(available[:5])
        yield f"[Error] Model '{actual_model}' not found. Available: {names}."
        return

    inventory = _build_inventory()
    system_content = (
        "You are a personal knowledge base assistant. "
        "Use the provided tools to search and retrieve relevant information from the user's documents, "
        "then answer the user's question based solely on what you find. "
        "Do not fabricate information.\n\n"
        + (inventory + "\n\n" if inventory else "")
    )

    ollama_opts = get_ollama_options()
    base_options = ollama_opts  # num_predict=-1 tells Ollama to not limit output length

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": question},
    ]

    for _iteration in range(8):
        # ── Non-streaming: detect whether the model wants to call a tool ──
        try:
            resp = requests.post(
                f"{base}/api/chat",
                json={
                    "model": actual_model,
                    "messages": messages,
                    "tools": AGENT_TOOLS_OPENAI,   # Ollama uses OpenAI tool format
                    "stream": False,
                    "think": False,
                    "options": base_options,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectionError:
            yield "[Error] Cannot connect to Ollama. Please make sure Ollama is running."
            return
        except Exception as e:
            yield f"[Error] Ollama Agent call failed: {e}"
            return

        message    = data.get("message", {})
        tool_calls = message.get("tool_calls")

        if tool_calls:
            messages.append(message)
            for i, tc in enumerate(tool_calls):
                fn      = tc.get("function", {})
                fn_name = fn.get("name", "")
                fn_args = fn.get("arguments", {})
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except json.JSONDecodeError:
                        fn_args = {}

                # Status indicator
                label = fn_args.get("query") or fn_args.get("title") or ""
                if fn_name == "search_knowledge_base":
                    yield f'\n\n*🔍 Searching: "{label}"*\n\n'
                elif fn_name == "get_document":
                    yield f'\n\n*📄 Reading: "{label}"*\n\n'
                elif fn_name == "list_documents":
                    yield "\n\n*📋 Listing documents...*\n\n"

                result, chips = _run_tool_with_chips(fn_name, fn_args)
                source_chips.extend(chips)

                # Ollama follows OpenAI format: tool results must include tool_call_id
                tc_id = tc.get("id") or f"call_{_iteration}_{i}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })

        else:
            # ── Final answer: always stream to avoid non-streaming num_predict truncation ──
            try:
                sresp = requests.post(
                    f"{base}/api/chat",
                    json={
                        "model": actual_model,
                        "messages": messages,
                        "stream": True,
                        "think": False,
                        "options": base_options,
                    },
                    stream=True,
                    timeout=180,
                )
                sresp.raise_for_status()
                for line in sresp.iter_lines():
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in d:
                        yield f"\n\n[Error] Ollama generation interrupted: {d['error']}"
                        return
                    token = d.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if d.get("done"):
                        break
            except Exception as e:
                yield f"[Error] Streaming generation failed: {e}"
            return

    yield "\n\n*[Agent reached maximum tool call iterations]*\n\n"


# ── Cloud Tool-Use Agent ──────────────────────────────────────────────────────

def _openai_agent_stream(
    base_url: str,
    api_key: str,
    model: str,
    question: str,
    source_chips: list,   # mutable list; populated during stream consumption
) -> Iterator[str]:
    """
    OpenAI-compatible Tool-Use Agent loop (DeepSeek / OpenAI / GLM / MiniMax).
    Tool-call phase: non-streaming (need full response to determine which tool to call).
    Final answer phase: streaming.
    """
    inventory = _build_inventory()
    system_content = (
        "You are a personal knowledge base assistant. "
        "Use the provided tools to search and retrieve relevant information from the user's documents, "
        "then answer the user's question based solely on what you find. "
        "Do not fabricate information.\n\n"
        + (inventory + "\n\n" if inventory else "")
    )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": question},
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for _iteration in range(8):
        # ── Non-streaming call to detect tool requests ──
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "tools": AGENT_TOOLS_OPENAI,
                    "tool_choice": "auto",
                    "stream": False,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            yield f"[Error] Agent call failed: {e}"
            return

        choice  = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls")

        if tool_calls:
            # Add the assistant message (with tool_calls) to history
            messages.append(message)
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    fn_args = {}

                # Status indicator
                label = fn_args.get("query") or fn_args.get("title") or ""
                if fn_name == "search_knowledge_base":
                    yield f'\n\n*🔍 Searching: "{label}"*\n\n'
                elif fn_name == "get_document":
                    yield f'\n\n*📄 Reading: "{label}"*\n\n'
                elif fn_name == "list_documents":
                    yield "\n\n*📋 Listing documents...*\n\n"

                # Execute tool
                result, chips = _run_tool_with_chips(fn_name, fn_args)
                source_chips.extend(chips)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        else:
            # ── Final answer: always stream to avoid provider non-streaming token limits ──
            # Filter out empty assistant messages (no tool_calls and no content) before streaming
            messages_for_stream = [
                m for m in messages
                if not (m.get("role") == "assistant" and not m.get("tool_calls") and not m.get("content"))
            ]
            try:
                with requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages_for_stream,
                        "stream": True,
                        "max_tokens": 4096,
                    },
                    stream=True,
                    timeout=120,
                ) as sresp:
                    sresp.raise_for_status()
                    for line in sresp.iter_lines():
                        if not line:
                            continue
                        text = line.decode("utf-8") if isinstance(line, bytes) else line
                        if text.startswith("data:"):
                            text = text[5:].strip()
                        if text == "[DONE]":
                            break
                        try:
                            d = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        token = (d.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                        if token:
                            yield token
            except Exception as e:
                yield f"[Error] Streaming generation failed: {e}"
            return

    yield "\n\n*[Agent reached maximum tool call iterations]*\n\n"


def _anthropic_agent_stream(
    api_key: str,
    model: str,
    question: str,
    source_chips: list,
) -> Iterator[str]:
    """
    Anthropic Claude Tool-Use Agent loop.
    Anthropic's tool format differs from OpenAI:
      - Model returns tool_use blocks inside content
      - Tool results are sent back as user role + tool_result blocks
    """
    inventory = _build_inventory()
    system_content = (
        "You are a personal knowledge base assistant. "
        "Use the provided tools to search and retrieve relevant information from the user's documents, "
        "then answer the user's question based solely on what you find. "
        "Do not fabricate information.\n\n"
        + (inventory + "\n\n" if inventory else "")
    )

    messages = [{"role": "user", "content": question}]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for _iteration in range(8):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "system": system_content,
                    "tools": AGENT_TOOLS_ANTHROPIC,
                    "messages": messages,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            yield f"[Error] Anthropic Agent call failed: {e}"
            return

        stop_reason     = data.get("stop_reason", "")
        content_blocks  = data.get("content", [])

        # Record this assistant turn
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "tool_use":
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    fn_name = block["name"]
                    fn_args = block.get("input", {})
                    tool_use_id = block["id"]

                    # Status indicator
                    label = fn_args.get("query") or fn_args.get("title") or ""
                    if fn_name == "search_knowledge_base":
                        yield f'\n\n*🔍 Searching: "{label}"*\n\n'
                    elif fn_name == "get_document":
                        yield f'\n\n*📄 Reading: "{label}"*\n\n'
                    elif fn_name == "list_documents":
                        yield "\n\n*📋 Listing documents...*\n\n"

                    result, chips = _run_tool_with_chips(fn_name, fn_args)
                    source_chips.extend(chips)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            # end_turn or other stop reason: yield text blocks
            for block in content_blocks:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        yield text
            return

    yield "\n\n*[Agent reached maximum tool call iterations]*\n\n"


def _cloud_agent_stream(
    provider: str,
    model_id: str,
    api_key: str,
    question: str,
) -> tuple[Iterator[str], list[dict]]:
    """
    Unified cloud Tool-Use Agent entry point.
    Returns (stream, source_chips); source_chips is populated as the stream is consumed.
    """
    from config import CLOUD_PROVIDERS

    # source_chips is a mutable list; the stream appends to it during iteration
    source_chips: list[dict] = []

    if provider == "anthropic":
        stream = _anthropic_agent_stream(api_key, model_id, question, source_chips)
    elif provider in CLOUD_PROVIDERS:
        base_url = CLOUD_PROVIDERS[provider]["base_url"]
        stream = _openai_agent_stream(base_url, api_key, model_id, question, source_chips)
    else:
        def _unknown():
            yield f"[Error] Unknown cloud provider: {provider}"
        return _unknown(), []

    return stream, source_chips


# ── Main entry point ──────────────────────────────────────────────────────────

def answer_stream(
    question: str,
    model: str = DEFAULT_MODEL,
    pinned_doc_id: int = None,
) -> tuple[Iterator[str], list[dict]]:
    """
    Execute the full RAG pipeline, returns (token_stream, source_chips).

    Model format:
      - Local Ollama:   "qwen3.5:9b"          → Ollama Tool-Use Agent
      - Remote Ollama:  "remote:qwen3.5:9b"   → Ollama Tool-Use Agent (SSH tunnel)
      - Cloud:          "cloud:deepseek:..."   → Cloud Tool-Use Agent
    """
    # ── Pinned document mode ──
    if pinned_doc_id is not None:
        doc = get_document_by_id(pinned_doc_id)
        if not doc:
            def _notfound():
                yield "Document not found; it may have been deleted."
            return _notfound(), []

        # Cloud models: large context window, full-text prompt is fine
        if model.startswith("cloud:"):
            full_text = doc.get("full_text") or ""
            text_chunks = chunk_text(full_text) if full_text else [full_text]
            chunks = [
                {"doc_id": doc["id"], "title": doc["title"],
                 "url": doc.get("url", ""), "chunk_text": c}
                for c in text_chunks
            ]
            summaries = get_document_summaries([doc["id"]])
            prompt = build_prompt(question, chunks, summaries, pinned_title=doc["title"])
            source_chips = [{"doc_id": doc["id"], "title": doc["title"], "url": doc.get("url", "")}]
            parts = model.split(":", 2)
            if len(parts) != 3:
                def _fmt_err():
                    yield f"[Error] Invalid cloud model format: {model}"
                return _fmt_err(), []
            _, provider, model_id = parts
            from config import get_cloud_keys
            api_key = get_cloud_keys().get(provider, {}).get("api_key", "")
            if not api_key:
                def _no_key():
                    yield f"[Error] API key for {provider} is not configured."
                return _no_key(), []
            return _dispatch_stream(model, prompt), source_chips

        # Local / remote Ollama: limited context window, use Agent with doc title prefix
        pinned_question = (
            f'[Regarding the document titled "{doc["title"]}"] {question}'
        )
        source_chips: list[dict] = []
        return _ollama_agent_stream(model, pinned_question, source_chips), source_chips

    # ── Cloud model → Cloud Tool-Use Agent ──
    if model.startswith("cloud:"):
        parts = model.split(":", 2)
        if len(parts) != 3:
            def _fmt_err():
                yield f"[Error] Invalid cloud model format: {model}"
            return _fmt_err(), []
        _, provider, model_id = parts
        from config import get_cloud_keys
        api_key = get_cloud_keys().get(provider, {}).get("api_key", "")
        if not api_key:
            def _no_key():
                yield f"[Error] API key for {provider} is not configured."
            return _no_key(), []
        return _cloud_agent_stream(provider, model_id, api_key, question)

    # ── Local / remote Ollama → Ollama Tool-Use Agent ──
    source_chips: list[dict] = []
    return _ollama_agent_stream(model, question, source_chips), source_chips


def _dispatch_stream(model: str, prompt: str) -> Iterator[str]:
    """
    Pinned document mode only: send prompt directly to model, bypassing Agent.
    """
    if model.startswith("cloud:"):
        parts = model.split(":", 2)
        if len(parts) != 3:
            yield f"[Error] Invalid cloud model format: {model}"
            return
        _, provider, model_name = parts
        from config import get_cloud_keys
        api_key = get_cloud_keys().get(provider, {}).get("api_key", "")
        yield from cloud_chat_stream(provider, model_name, api_key, prompt)
    else:
        yield from ollama_chat_stream(model, prompt)

<div align="center">

<img src="image.png" alt="Rivus Logo" width="96" />

# Rivus · 问渠

**Your personal knowledge base that thinks with you — fully offline, forever private.**

*问渠那得清如许，为有源头活水来*
*"Why is the canal so clear? Because living water flows from the source."*
— Zhu Xi, Song Dynasty

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey)](https://github.com/caokai1073/Rivus/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Stars](https://img.shields.io/github/stars/caokai1073/Rivus?style=social)](https://github.com/caokai1073/Rivus)

[**Download**](https://github.com/caokai1073/Rivus/releases) · [**Report Bug**](https://github.com/caokai1073/Rivus/issues) · [**Request Feature**](https://github.com/caokai1073/Rivus/issues) · [**中文文档**](#中文简介)

---

<!-- Replace with a real screenshot or demo GIF -->
![Rivus Demo](docs/demo.gif)

</div>

---

## Why Rivus?

Most AI chat tools either send your data to the cloud, or stop at basic search. Rivus does neither.

It's a **desktop app** that lets you build a personal knowledge base from your documents, web pages, PDFs, and notes — then ask questions about them using AI that runs **entirely on your own machine**. No subscriptions. No vendor lock-in.

If you prefer cloud models (DeepSeek, OpenAI, Claude), Rivus supports those too. All documents and embeddings are stored locally on your machine. When using cloud models, your question and the relevant document excerpts retrieved by RAG are sent to the API — your full knowledge base stays on your device.

---

## Features

### 📥 Ingest Anything
- **Web URLs** — paste a link, Rivus fetches and cleans the article automatically
- **PDFs** — drag-and-drop or file picker, full text extracted
- **Word documents** (.docx)
- **Plain text** — paste directly into the editor
- **Folder organization** — group documents into named collections

### 🧠 Smart Retrieval (Hybrid RAG)
Rivus doesn't just do keyword search. Every query goes through a multi-stage pipeline:

1. **Query expansion** — the LLM rephrases your question 2 ways to widen the net
2. **Vector search** — semantic similarity via `BAAI/bge-m3` (1024-dim embeddings, stored in SQLite)
3. **Full-text search** — BM25-ranked keyword match
4. **RRF fusion** — Reciprocal Rank Fusion merges both result lists into one ranked set
5. **Document-pinning** — reference a specific document directly in your question

### 🤖 Local AI (Zero Cloud Required)
- One-click Ollama integration — download and run models without touching the terminal
- Supported models: **Qwen3**, **Llama 3**, **Gemma 3**, **Phi-4**, and any model on [ollama.com/library](https://ollama.com/library)
- Configurable inference parameters: context window, temperature, top-p, repeat penalty, max tokens
- Automatic `<think>` block filtering for reasoning models (Qwen3, DeepSeek-R1)

### ☁️ Cloud AI (Optional)
| Provider | Models |
|---|---|
| DeepSeek | V4 Flash, V4 Pro |
| OpenAI | GPT-5.5, GPT-4.1, o3 |
| Anthropic | Claude Opus 4, Sonnet 4, Haiku 4 |
| MiniMax | M1, Text-01 |
| 智谱 GLM | GLM-4-Plus, GLM-Z1-Plus (Reasoning) |

### 🔒 Privacy First
- All documents stored in a local SQLite database
- Embeddings computed locally (no external embedding API needed)
- Works 100% offline with local models
- Data directory is yours — back it up, move it, version-control it

### 🌍 Bilingual
Full English and Chinese interface — switches instantly without restart.

### 🖥️ Native Desktop Feel
- macOS and Windows native app (pywebview)
- macOS: hide to Dock, reopen with click — stays out of your way
- Windows: single-instance enforcement, no stray console windows
- Import/export your entire knowledge base as a `.zip` backup

---

## Quick Start

### Option A: Download the App (Recommended)

| Platform | Download |
|---|---|
| macOS (Apple Silicon + Intel) | [Rivus-1.0.0.dmg](https://github.com/caokai1073/Rivus/releases) |
| Windows 11/10 | [Rivus-1.0.0-setup.exe](https://github.com/caokai1073/Rivus/releases) |

**macOS note:** After opening the DMG, drag Rivus to Applications. On first launch macOS may block it — go to **System Settings → Privacy & Security** and click "Open Anyway". This is standard for unsigned apps.

**Windows note:** Windows Smart App Control may flag the installer. Click "More info → Run anyway".

---

### Option B: Run from Source

**Requirements:** Python 3.11+, [Ollama](https://ollama.com) (optional, for local models)

If you don't have Python 3.11, install it via [python.org](https://www.python.org/downloads/) or with conda:

```bash
# With conda
conda create -n rivus python=3.11
conda activate rivus

# With pyenv (macOS/Linux)
pyenv install 3.11
pyenv local 3.11
```

Then clone and run:

```bash
git clone https://github.com/caokai1073/Rivus.git
cd Rivus

pip install -r requirements.txt

python app.py
```

The app window opens automatically. On first run, the embedding model (`BAAI/bge-m3`, ~570MB) is downloaded once.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  pywebview window                   │
│              (HTML/CSS/JS frontend)                 │
└────────────────────┬────────────────────────────────┘
                     │  JS ↔ Python API bridge
┌────────────────────▼────────────────────────────────┐
│              FastAPI (local HTTP server)            │
│   /api/query   /api/ingest/*   /api/ollama-*  ...   │
└──────┬──────────────────┬───────────────────────────┘
       │                  │
┌──────▼──────┐    ┌──────▼──────────────────────────┐
│  query.py   │    │           ingest.py              │
│             │    │                                  │
│  1. Expand  │    │  URL → readability               │
│  2. Embed   │    │  PDF → PyMuPDF                   │
│  3. Vec     │    │  DOCX → python-docx              │
│     search  │    │  Text → chunker                  │
│  4. FTS     │    │  Chunks → BAAI/bge-m3 → vectors  │
│  5. RRF     │    └──────────────────────────────────┘
│  6. Prompt  │
│  7. Stream  │    ┌──────────────────────────────────┐
└──────┬──────┘    │           db.py                  │
       │           │                                  │
       │           │  SQLite + sqlite-vec              │
       │           │  • documents table               │
       │           │  • chunks table (full text)      │
       │           │  • chunk_embeddings (vec index)  │
       └───────────│  • folders table                 │
                   └──────────────────────────────────┘
                   
       ┌──────────────────────────────────────────────┐
       │         LLM layer (config.py + query.py)     │
       │                                              │
       │  Local:  Ollama  →  localhost:11434          │
       │  Cloud:  DeepSeek / OpenAI / Anthropic / ... │
       └──────────────────────────────────────────────┘
```

**Key design choices:**
- **SQLite + sqlite-vec** — zero-dependency vector store; the entire knowledge base is a single `.db` file you can copy anywhere
- **BAAI/bge-m3** — multilingual embedding model, handles Chinese and English in the same index without separate pipelines
- **RRF over re-ranking** — faster than a cross-encoder re-ranker, robust to vocabulary mismatch
- **pywebview over Electron** — orders of magnitude smaller bundle size; no Node.js runtime

---

## Tech Stack

| Layer | Technology |
|---|---|
| Desktop shell | [pywebview](https://pywebview.flowrl.com/) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| Database | [SQLite](https://sqlite.org/) + [sqlite-vec](https://github.com/asg017/sqlite-vec) |
| Embeddings | [sentence-transformers](https://www.sbert.net/) / BAAI/bge-m3 |
| Local LLM | [Ollama](https://ollama.com/) |
| PDF parsing | [PyMuPDF](https://pymupdf.readthedocs.io/) |
| Web parsing | [readability-lxml](https://github.com/buriy/python-readability) |
| DOCX parsing | [python-docx](https://python-docx.readthedocs.io/) |
| Frontend | Vanilla HTML/CSS/JS (no framework, no build step) |

---

## Roadmap

These are features actively being considered. PRs welcome.

- [ ] **Browser extension** — clip web pages directly from Chrome/Safari
- [ ] **PDF annotation** — highlight and save excerpts as notes
- [ ] **Batch URL import** — paste a list of links and import all at once
- [ ] **Scheduled re-fetch** — keep web articles fresh with periodic re-crawl
- [ ] **Markdown / Obsidian vault import** — ingest `.md` files and directory trees
- [ ] **OCR support** — extract text from scanned PDFs and images
- [ ] **Graph view** — visualize connections between documents
- [ ] **MCP server** — expose your knowledge base as a Model Context Protocol server, usable from Claude Desktop or any MCP-compatible client
- [ ] **Linux support** — AppImage packaging
- [ ] **Sharing** — export a read-only snapshot to share with others
- [ ] **Multi-language embedding** — user-selectable embedding models

Have something else in mind? [Open a feature request](https://github.com/caokai1073/Rivus/issues/new?template=feature_request.md).

---

## Contributing

Rivus is actively developed and warmly welcomes contributions. The codebase is intentionally small (~2,400 lines of Python + ~3,500 lines of HTML/JS) and has no build system — you can be productive in minutes.

### Good First Issues

Look for issues tagged [`good first issue`](https://github.com/caokai1073/Rivus/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) — these are self-contained tasks that don't require deep knowledge of the codebase:

- Adding a new cloud provider (just extend `CLOUD_PROVIDERS` in `config.py`)
- Improving the chunking strategy in `ingest.py`
- Adding keyboard shortcuts to the frontend
- Writing tests (there are currently none — huge opportunity!)
- Improving error messages and edge case handling

### Development Setup

```bash
git clone https://github.com/caokai1073/Rivus.git
cd Rivus
pip install -r requirements.txt
python app.py        # the app hot-reloads HTML/JS changes on refresh
```

The frontend is plain HTML/JS in `ui/index.html` — no build step, just edit and refresh the webview.

### Project Layout

```
version1.0.0/
├── app.py          # pywebview window setup, macOS Dock integration
├── server.py       # FastAPI routes (~50 endpoints)
├── query.py        # RAG pipeline: expand → embed → search → fuse → prompt → stream
├── ingest.py       # Document parsing and chunking
├── db.py           # SQLite schema, vector search, FTS
├── config.py       # Settings persistence, cloud provider definitions
├── launcher.py     # Windows launcher (hides console, handles PATH)
├── ui/
│   └── index.html  # Entire frontend (~3,500 lines, self-contained)
├── build_app.sh    # macOS DMG build script
└── build_windows.bat # Windows exe build (PyInstaller)
```

### Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feature/your-feature`
2. Make your changes
3. Test on macOS or Windows (or both if you can)
4. Open a PR with a short description of what changed and why

There's no formal PR template right now — just be clear about what the change does. All contributions are reviewed within a few days.

---

## FAQ

**Does it work without internet?**
Yes, completely. With a local Ollama model, Rivus has zero network dependencies after the initial model download.

**How large can my knowledge base get?**
SQLite handles millions of rows easily. The main constraint is the embedding index in memory during search — expect comfortable performance up to tens of thousands of documents on typical hardware.

**Can I use my own embedding model?**
Not yet via the UI, but it's a one-line change in `ingest.py` (`SentenceTransformer("your-model")`). This is on the roadmap as a UI option.

**Why not use a dedicated vector database like Chroma or Qdrant?**
Simplicity and portability. sqlite-vec gives us vector search without a separate process, and the entire knowledge base lives in a single file. For a personal tool, this is a better trade-off.

**Can I run it on a server / headless?**
The FastAPI backend can run standalone (just `uvicorn server:app`), but the frontend assumes pywebview. A proper web UI is a potential future direction.

**The macOS app says it's from an unidentified developer.**
This is expected for unsigned apps. See the installation instructions in the DMG, or run `xattr -cr Rivus.app` in Terminal before launching.

---

## License

Apache 2.0 © [Kai Cao](https://github.com/caokai1073)

---

<div align="center">

If Rivus is useful to you, a ⭐ on GitHub goes a long way.

</div>

---

## 中文简介

**Rivus · 问渠** 是一个完全本地运行的个人知识库 AI 问答工具。

名字取自朱熹《观书有感》：*"问渠那得清如许，为有源头活水来"*。

### 核心特点

- **完全本地**：文档、向量、对话历史全部存在本机 SQLite 数据库，无需任何云服务
- **混合 RAG**：向量检索 + 全文检索 + RRF 融合 + Query 扩写，检索质量远超单纯关键词搜索
- **支持本地模型**：通过 Ollama 一键下载运行 Qwen3、Llama 3、Gemma 3 等模型
- **支持云端模型**：DeepSeek、OpenAI、Claude、智谱 GLM、MiniMax
- **跨平台**：macOS + Windows 原生应用
- **中英双语**：界面支持中英文随时切换

### 快速运行

```bash
git clone https://github.com/caokai1073/Rivus.git
cd Rivus
pip install -r requirements.txt
python app.py
```

欢迎提 Issue、PR，或者直接 ⭐ 支持一下 :)

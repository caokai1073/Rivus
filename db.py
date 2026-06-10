"""
db.py — SQLite + sqlite-vec 向量数据库层
"""
import os
import sqlite3
import sqlite_vec
import struct
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# 打包成 .app 后数据存 ~/Library/Application Support/MemoryVault/
# 开发时存在项目目录下
_data_dir = os.environ.get("MEMORYVAULT_DATA_DIR")
DB_PATH = Path(_data_dir) / "memoryvault.db" if _data_dir else Path(__file__).parent / "memoryvault.db"
EMBEDDING_DIM = 1024  # BAAI/bge-m3 输出维度


def _serialize_vector(v: list[float]) -> bytes:
    """把 float list 序列化成 sqlite-vec 需要的 binary 格式"""
    return struct.pack(f"{len(v)}f", *v)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    return conn


def _current_vec_dim() -> int | None:
    """读取 chunk_embeddings 虚表当前维度，表不存在返回 None"""
    import re
    conn = get_conn()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_embeddings'"
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    m = re.search(r'FLOAT\[(\d+)\]', row[0])
    return int(m.group(1)) if m else None


def init_db():
    # ── 维度变更时自动清库（重新入库所有文档）────────────────────────────────
    old_dim = _current_vec_dim()
    if old_dim is not None and old_dim != EMBEDDING_DIM:
        print(f"[db] embedding 维度从 {old_dim} 变更为 {EMBEDDING_DIM}，清空旧数据…")
        conn = get_conn()
        with conn:
            conn.execute("DROP TABLE IF EXISTS chunk_embeddings")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM documents")
        conn.close()
        print("[db] 旧数据已清除，请重新添加文档。")

    conn = get_conn()
    with conn:
        # 文件夹表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # 文档元数据表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT,
                title     TEXT,
                source    TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                full_text TEXT,
                folder_id  INTEGER REFERENCES folders(id) ON DELETE SET NULL
            )
        """)

        # 兼容旧库：按需添加缺失列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
        if "folder_id" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")
        if "summary" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN summary TEXT")

        # chunk 表（每篇文档切块后的片段）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id      INTEGER NOT NULL REFERENCES documents(id),
                chunk_index INTEGER NOT NULL,
                text        TEXT NOT NULL
            )
        """)

        # 向量表（sqlite-vec virtual table）
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings
            USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            )
        """)

        # FTS5 全文索引（BM25 关键词检索）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(text, tokenize='unicode61 remove_diacritics 1')
        """)
    conn.close()

    # ── 迁移：把已有 chunk 补充进 FTS 索引 ───────────────────────────────────
    conn = get_conn()
    with conn:
        existing = {r[0] for r in conn.execute("SELECT rowid FROM chunks_fts").fetchall()}
        rows = conn.execute("SELECT id, text FROM chunks").fetchall()
        missing = [(r["id"], r["text"]) for r in rows if r["id"] not in existing]
        if missing:
            conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", missing)
            print(f"[db] FTS 索引补充 {len(missing)} 条")
    conn.close()


def insert_document(url: str, title: str, source: str, full_text: str, folder_id: int = None) -> int:
    """插入一篇文档，返回 doc_id"""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO documents (url, title, source, full_text, folder_id) VALUES (?,?,?,?,?)",
            (url, title, source, full_text, folder_id)
        )
        doc_id = cur.lastrowid
    conn.close()
    return doc_id


def insert_chunk(doc_id: int, chunk_index: int, text: str, embedding: list[float]) -> int:
    """插入一个 chunk 及其 embedding，返回 chunk_id"""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO chunks (doc_id, chunk_index, text) VALUES (?,?,?)",
            (doc_id, chunk_index, text)
        )
        chunk_id = cur.lastrowid
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedding) VALUES (?,?)",
            (chunk_id, _serialize_vector(embedding))
        )
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
            (chunk_id, text)
        )
    conn.close()
    return chunk_id


def vector_search(query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """向量近邻搜索，返回最相关的 top_k 个 chunk（含文档信息）"""
    conn = get_conn()
    results = conn.execute(
        f"""
        SELECT
            c.id        AS chunk_id,
            c.doc_id,
            c.text      AS chunk_text,
            d.title,
            d.url,
            d.source,
            ce.distance
        FROM chunk_embeddings ce
        JOIN chunks c ON c.id = ce.chunk_id
        JOIN documents d ON d.id = c.doc_id
        WHERE embedding MATCH ?
          AND k = ?
        ORDER BY distance
        """,
        (_serialize_vector(query_embedding), top_k)
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


def fts_search(query: str, top_k: int = 8) -> list[dict]:
    """BM25 关键词搜索，返回最相关的 top_k 个 chunk"""
    import re
    try:
        # 提取词元（中文字符 + 英文单词），最多取前 10 个，OR 连接
        tokens = list(dict.fromkeys(
            re.findall(r'[一-鿿]+|\b[a-zA-Z0-9]{2,}\b', query)
        ))[:10]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in tokens)
        conn = get_conn()
        rows = conn.execute(
            """
            SELECT cf.rowid  AS chunk_id,
                   c.doc_id,
                   c.text    AS chunk_text,
                   d.title,
                   d.url,
                   d.source,
                   bm25(chunks_fts) AS distance
            FROM chunks_fts cf
            JOIN chunks    c ON c.id  = cf.rowid
            JOIN documents d ON d.id  = c.doc_id
            WHERE chunks_fts MATCH ?
            ORDER BY distance          -- bm25 值越小（越负）越相关
            LIMIT ?
            """,
            (fts_query, top_k)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[fts_search] 错误: {e}")
        return []


def get_document_by_id(doc_id: int) -> Optional[dict]:
    """按 id 获取单篇文档（含正文）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, url, title, source, created_at, folder_id, summary, full_text FROM documents WHERE id=?",
        (doc_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents() -> list[dict]:
    """列出所有已收藏的文档（含 folder_id、summary）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, url, title, source, created_at, folder_id, summary FROM documents ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_documents(query: str) -> list[dict]:
    """全文搜索：匹配标题、正文、摘要，返回匹配文档列表（不含 full_text）"""
    conn = get_conn()
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id, url, title, source, created_at, folder_id, summary,
               -- 标记命中位置方便前端高亮：title命中=2，正文命中=1
               CASE
                   WHEN title LIKE ? THEN 2
                   ELSE 1
               END AS match_rank
        FROM documents
        WHERE title LIKE ?
           OR full_text LIKE ?
           OR summary LIKE ?
        ORDER BY match_rank DESC, id DESC
        """,
        (pattern, pattern, pattern, pattern)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_summary(doc_id: int, summary: str):
    """更新文档的 LLM 摘要"""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE documents SET summary=? WHERE id=?", (summary, doc_id))
    conn.close()


def get_document_summaries(doc_ids: list[int]) -> dict[int, str]:
    """批量获取文档摘要，返回 {doc_id: summary}"""
    if not doc_ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"SELECT id, summary FROM documents WHERE id IN ({placeholders})", doc_ids
    ).fetchall()
    conn.close()
    return {r["id"]: r["summary"] for r in rows if r["summary"]}


def find_by_url(url: str) -> dict | None:
    """按 URL 查找已存在的文档，用于去重"""
    if not url:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT id, title FROM documents WHERE url=? LIMIT 1", (url,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def rename_document(doc_id: int, new_title: str):
    """重命名文档"""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE documents SET title=? WHERE id=?", (new_title, doc_id))
    conn.close()


# ── 文件夹操作 ────────────────────────────────────────────────────────────────

def list_folders() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, name, created_at FROM folders ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_folder(name: str) -> int:
    conn = get_conn()
    with conn:
        cur = conn.execute("INSERT INTO folders (name) VALUES (?)", (name,))
        fid = cur.lastrowid
    conn.close()
    return fid


def rename_folder(folder_id: int, name: str):
    conn = get_conn()
    with conn:
        conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    conn.close()


def delete_folder(folder_id: int):
    """删除文件夹，其中的文档变为未分类"""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE documents SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.close()


def move_document(doc_id: int, folder_id: Optional[int]):
    """把文档移入文件夹（folder_id=None 表示移出到未分类）"""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE documents SET folder_id=? WHERE id=?", (folder_id, doc_id))
    conn.close()


def export_all() -> list[dict]:
    """导出所有文档（含正文），用于备份/迁移"""
    conn = get_conn()
    docs = conn.execute(
        "SELECT id, url, title, source, created_at, full_text FROM documents ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(d) for d in docs]


def delete_document(doc_id: int):
    """删除一篇文档及其所有 chunk 和 embedding"""
    conn = get_conn()
    with conn:
        chunk_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chunks WHERE doc_id=?", (doc_id,)
        ).fetchall()]
        if chunk_ids:
            placeholders = ",".join("?" * len(chunk_ids))
            conn.execute(f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", chunk_ids)
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", chunk_ids)
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.close()

"""
위키 페이지 저장소 (app_data.db) + 대표 질문 임베딩 관리 (#58)

- wiki_pages: 페이지 본문·팩트·상태 (draft → approved / disabled)
- ChromaDB: 대표 질문별 1문서 임베딩 (is_wiki=True) — Hub 질문 임베딩 패턴 재사용
  disabled 전환 시 임베딩 제거로 매칭 대상에서 즉시 제외.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_local = threading.local()

STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_DISABLED = "disabled"
_VALID_STATUS = {STATUS_DRAFT, STATUS_APPROVED, STATUS_DISABLED}


def _conn() -> sqlite3.Connection:
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(Path(settings.APP_DATA_DB_PATH), "Wiki", _local, "con")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_wiki_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                topic               TEXT    NOT NULL UNIQUE,
                title               TEXT    NOT NULL,
                content             TEXT    NOT NULL,
                questions_json      TEXT    NOT NULL DEFAULT '[]',
                facts_json          TEXT    DEFAULT '[]',
                source_doc_ids_json TEXT    DEFAULT '[]',
                source_hash         TEXT    DEFAULT '',
                status              TEXT    NOT NULL DEFAULT 'draft',
                model               TEXT    DEFAULT '',
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_wiki_status ON wiki_pages(status)")
        con.commit()


def compute_source_hash(content_hashes: List[str]) -> str:
    """소스 문서 content_hash 집합의 해시 — 변경 감지용(Phase 2 freshness)."""
    joined = "|".join(sorted(h for h in content_hashes if h))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


# ── CRUD ─────────────────────────────────────────────────────────────────────

def upsert_page(
    topic: str,
    title: str,
    content: str,
    questions: List[str],
    facts: List[Dict],
    source_doc_ids: List[str],
    source_hash: str,
    model: str,
) -> Dict:
    """페이지 생성/재생성. 재생성 시 status는 draft로 강등(재검수)."""
    now = _now()
    with _conn() as con:
        row = con.execute("SELECT id FROM wiki_pages WHERE topic = ?", (topic,)).fetchone()
        if row:
            con.execute(
                "UPDATE wiki_pages SET title=?, content=?, questions_json=?, facts_json=?, "
                "source_doc_ids_json=?, source_hash=?, status='draft', model=?, updated_at=? "
                "WHERE topic=?",
                (title, content, json.dumps(questions, ensure_ascii=False),
                 json.dumps(facts, ensure_ascii=False),
                 json.dumps(source_doc_ids), source_hash, model, now, topic),
            )
            page_id = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO wiki_pages (topic, title, content, questions_json, facts_json, "
                "source_doc_ids_json, source_hash, status, model, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (topic, title, content, json.dumps(questions, ensure_ascii=False),
                 json.dumps(facts, ensure_ascii=False),
                 json.dumps(source_doc_ids), source_hash, model, now, now),
            )
            page_id = cur.lastrowid
        con.commit()
    # 대표 질문 임베딩 갱신 (재생성 포함 — 기존 것 제거 후 재등록)
    _reindex_questions(page_id, topic, title, questions)
    logger.info(f"[Wiki] 페이지 저장: {topic} (id={page_id}, 질문 {len(questions)}개)")
    return get_page(page_id)


def get_page(page_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
    return _row_to_dict(row)


def get_page_by_topic(topic: str) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM wiki_pages WHERE topic = ?", (topic,)).fetchone()
    return _row_to_dict(row)


def get_page_by_wiki_id(wiki_id: int) -> Optional[Dict]:
    """검색 주입용 — disabled 페이지는 반환하지 않음."""
    page = get_page(wiki_id)
    if page and page["status"] != STATUS_DISABLED:
        return page
    return None


def list_pages() -> List[Dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def set_status(page_id: int, status: str) -> bool:
    if status not in _VALID_STATUS:
        raise ValueError(f"invalid status: {status}")
    page = get_page(page_id)
    if not page:
        return False
    with _conn() as con:
        con.execute("UPDATE wiki_pages SET status=?, updated_at=? WHERE id=?",
                    (status, _now(), page_id))
        con.commit()
    # disabled → 임베딩 제거(매칭 제외), 재활성 → 재등록
    if status == STATUS_DISABLED:
        _remove_question_embeddings(page_id)
    elif page["status"] == STATUS_DISABLED:
        _reindex_questions(page_id, page["topic"], page["title"], page["questions"])
    logger.info(f"[Wiki] 상태 변경: id={page_id} {page['status']} → {status}")
    return True


def _row_to_dict(row) -> Optional[Dict]:
    if row is None:
        return None
    d = dict(row)
    d["questions"] = json.loads(d.pop("questions_json") or "[]")
    d["facts"] = json.loads(d.pop("facts_json") or "[]")
    d["source_doc_ids"] = json.loads(d.pop("source_doc_ids_json") or "[]")
    return d


# ── 대표 질문 임베딩 (ChromaDB) ──────────────────────────────────────────────

def _embedding_ids(page_id: int, n: int) -> List[str]:
    return [f"wiki-{page_id}-q{i}" for i in range(n)]


def _remove_question_embeddings(page_id: int) -> None:
    from company_llm_rag.database import db_manager
    collection = db_manager.get_collection()
    try:
        existing = collection.get(where={"wiki_id": page_id}, include=[])
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
    except Exception as e:
        logger.warning(f"[Wiki] 임베딩 제거 실패 (id={page_id}): {e}")


def _reindex_questions(page_id: int, topic: str, title: str, questions: List[str]) -> None:
    from company_llm_rag.database import db_manager
    collection = db_manager.get_collection()
    _remove_question_embeddings(page_id)
    if not questions:
        return
    ids = _embedding_ids(page_id, len(questions))
    collection.add(
        ids=ids,
        documents=questions,  # 질문 텍스트만 임베딩 (Hub 패턴)
        metadatas=[{
            "source": "wiki",
            "is_wiki": True,
            "wiki_id": page_id,
            "title": title,
            "topic": topic,
            "original_doc_id": f"wiki-{page_id}",
        } for _ in questions],
    )

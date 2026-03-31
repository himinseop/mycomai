"""
FTS5 검색 인덱스 저장소 (search_index.db)

ChromaDB 문서 전문 검색(Full-Text Search) 인덱스를 별도 SQLite DB로 관리합니다.
웹 운영 이력(app_data.db)과 분리되어 대량 upsert/rebuild 작업이 독립적으로 수행됩니다.
"""

import sqlite3
import threading
from pathlib import Path
from typing import List

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.SEARCH_INDEX_DB_PATH)

_local = threading.local()  # 스레드별 연결 캐시


def _conn() -> sqlite3.Connection:
    """스레드별 SQLite 연결을 캐싱하여 반환합니다."""
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(_DB_PATH, "FTS", _local, "fts_con")


def init_fts_db() -> None:
    """FTS DB 초기화 — doc_fts 가상 테이블 생성 (idempotent)."""
    with _conn() as con:
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
                chunk_id UNINDEXED,
                content,
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
        con.commit()


def fts_upsert(chunk_id: str, content: str) -> None:
    """단일 청크를 FTS 인덱스에 추가/갱신합니다."""
    with _conn() as con:
        con.execute("DELETE FROM doc_fts WHERE chunk_id = ?", (chunk_id,))
        con.execute("INSERT INTO doc_fts (chunk_id, content) VALUES (?, ?)", (chunk_id, content))
        con.commit()


def fts_bulk_upsert(docs: List[tuple]) -> None:
    """여러 청크를 FTS 인덱스에 일괄 추가합니다. docs: [(chunk_id, content), ...]"""
    if not docs:
        return
    with _conn() as con:
        con.executemany("DELETE FROM doc_fts WHERE chunk_id = ?", [(d[0],) for d in docs])
        con.executemany("INSERT INTO doc_fts (chunk_id, content) VALUES (?, ?)", docs)
        con.commit()


def fts_search(keywords: List[str], limit: int = 21) -> List[str]:
    """
    키워드 prefix 검색으로 매칭 chunk_id 리스트를 반환합니다.
    FTS5 BM25 점수 순(관련도 높은 순)으로 정렬됩니다.
    """
    if not keywords:
        return []
    parts = ['"' + kw.replace('"', '') + '"*' for kw in keywords]
    fts_query = " OR ".join(parts)
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT chunk_id FROM doc_fts WHERE doc_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        logger.debug(f"FTS 검색 실패 (query={fts_query!r}): {e}")
        return []


def fts_exists() -> bool:
    """FTS 인덱스에 데이터가 있으면 True를 반환합니다."""
    try:
        with _conn() as con:
            row = con.execute("SELECT 1 FROM doc_fts LIMIT 1").fetchone()
            return row is not None
    except Exception:
        return False


def fts_count() -> int:
    """FTS 인덱스의 청크 수를 반환합니다. (모니터링/관리 목적)"""
    try:
        with _conn() as con:
            return con.execute("SELECT COUNT(*) FROM doc_fts").fetchone()[0]
    except Exception:
        return 0

"""
플랫폼 문서 S3 수집 상태 저장 (app_data.db) (#62)

마지막 성공 sourceCommit과, 그 시점의 문서 스냅샷(catalog_id → doc_id, content_hash)을
보관합니다. digest_store.py와 동일한 패턴(sqlite_utils.create_connection, threading.local)을
사용합니다.

스냅샷은 s3_docs_ingest가 신규/변경/삭제 문서를 정확히 판별하는 데 쓰이며,
모든 처리가 성공한 뒤에만 commit_snapshot()으로 원자적으로(단일 트랜잭션) 교체됩니다 —
색인 도중 실패하면 이 테이블은 이전 성공 상태 그대로 남습니다.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.APP_DATA_DB_PATH)
_local = threading.local()

# catalog_id -> (doc_id, content_hash)
DocSnapshot = Dict[str, Tuple[str, str]]


def _conn() -> sqlite3.Connection:
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(_DB_PATH, "PlatformDocs", _local, "con")


def init_db() -> None:
    """platform_docs_state / platform_docs_docs 테이블을 생성합니다 (존재하면 무시)."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS platform_docs_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                source_commit TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS platform_docs_docs (
                catalog_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
        """)


def get_last_source_commit() -> Optional[str]:
    """마지막으로 성공한 수집의 sourceCommit을 반환합니다 (없으면 None)."""
    init_db()
    with _conn() as con:
        row = con.execute("SELECT source_commit FROM platform_docs_state WHERE id = 1").fetchone()
    return row["source_commit"] if row else None


def get_doc_snapshot() -> DocSnapshot:
    """마지막 성공 수집 시점의 문서 스냅샷을 반환합니다. {catalog_id: (doc_id, content_hash)}"""
    init_db()
    with _conn() as con:
        rows = con.execute("SELECT catalog_id, doc_id, content_hash FROM platform_docs_docs").fetchall()
    return {r["catalog_id"]: (r["doc_id"], r["content_hash"]) for r in rows}


def commit_snapshot(source_commit: str, docs: DocSnapshot) -> None:
    """이번 수집의 sourceCommit과 문서 스냅샷으로 전체 교체합니다 (단일 트랜잭션, 원자적).

    모든 색인·삭제 처리가 성공한 뒤에만 호출해야 합니다 — 중간에 실패하면 이 함수는
    호출되지 않아 상태가 이전 성공 버전 그대로 유지됩니다.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("DELETE FROM platform_docs_docs")
        if docs:
            con.executemany(
                "INSERT INTO platform_docs_docs (catalog_id, doc_id, content_hash) VALUES (?, ?, ?)",
                [(cid, doc_id, content_hash) for cid, (doc_id, content_hash) in docs.items()],
            )
        con.execute(
            "INSERT INTO platform_docs_state (id, source_commit, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET source_commit = excluded.source_commit, updated_at = excluded.updated_at",
            (source_commit, now),
        )
    logger.info(f"[PlatformDocsS3] 상태 스냅샷 갱신: source_commit={source_commit} docs={len(docs)}")

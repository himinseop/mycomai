"""
SQLite 공통 연결 팩토리

history_store, fts_store, rebuild_fts에서 사용하는
SQLite 연결 생성, PRAGMA 설정, 스레드별 캐싱을 한 곳에서 관리합니다.
"""

import sqlite3
import threading
from pathlib import Path

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def create_connection(db_path: Path, label: str, thread_local: threading.local, cache_attr: str) -> sqlite3.Connection:
    """스레드별 SQLite 연결을 캐싱하여 반환합니다.

    Args:
        db_path: DB 파일 경로
        label: 로그 식별자 (예: "History", "FTS")
        thread_local: 스레드별 캐시 객체
        cache_attr: thread_local에 저장할 속성명
    """
    con = getattr(thread_local, cache_attr, None)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except Exception:
            setattr(thread_local, cache_attr, None)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    journal_mode = settings.SQLITE_JOURNAL_MODE
    actual = con.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
    con.execute("PRAGMA synchronous=NORMAL")
    logger.info(f"[{label}] SQLite journal_mode={actual}")
    setattr(thread_local, cache_attr, con)
    return con

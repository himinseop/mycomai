"""
다이제스트 sp_guid 집합 저장 (#61 11-A)

data_loader가 매 적재 시 다이제스트 문서의 sp_guid 전체 스냅샷으로 이 테이블을 교체한다.
retrieval_module은 sharepoint 원본 문서의 URL에서 GUID를 추출해 이 집합에 있으면
distance를 다운랭크한다 (다이제스트가 원본을 대체 — 제거는 아님).
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Set

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.APP_DATA_DB_PATH)
_local = threading.local()

_cache: Set[str] = None
_cache_at: float = 0.0
_CACHE_TTL = 300.0  # 5분


def _conn() -> sqlite3.Connection:
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(_DB_PATH, "DigestGuids", _local, "con")


def init_db() -> None:
    """digest_guids 테이블을 생성합니다 (존재하면 무시)."""
    with _conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS digest_guids (guid TEXT PRIMARY KEY)")


def replace_guids(guids: Iterable[str]) -> int:
    """이번 적재에서 수집된 다이제스트 sp_guid 전체 집합으로 테이블을 교체합니다."""
    global _cache, _cache_at
    init_db()
    clean = sorted({g.strip().upper() for g in guids if g and g.strip()})
    with _conn() as con:
        con.execute("DELETE FROM digest_guids")
        con.executemany("INSERT OR IGNORE INTO digest_guids (guid) VALUES (?)", [(g,) for g in clean])
    _cache, _cache_at = None, 0.0
    logger.info(f"[Digest] sp_guid 집합 갱신: {len(clean)}개")
    return len(clean)


def get_guid_set() -> Set[str]:
    """다이제스트 sp_guid 집합을 반환합니다 (5분 캐시)."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and now - _cache_at < _CACHE_TTL:
        return _cache
    init_db()
    with _conn() as con:
        rows = con.execute("SELECT guid FROM digest_guids").fetchall()
    _cache = {r["guid"] for r in rows}
    _cache_at = now
    return _cache

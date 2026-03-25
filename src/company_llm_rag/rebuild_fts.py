"""
SQLite FTS5 인덱스 초기 구축 / 재구축 스크립트

ChromaDB에 이미 적재된 모든 문서를 FTS5 인덱스에 등록합니다.
data_loader.py 이후 실행하거나, FTS 인덱스가 비어있을 때 한 번 실행합니다.

사용법:
    PYTHONPATH=src python3 -m company_llm_rag.rebuild_fts
    또는 Docker:
    docker compose run --rm web python -m company_llm_rag.rebuild_fts
"""

import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager
from company_llm_rag.fts_store import init_fts_db, fts_count
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_BATCH_SIZE = 5000   # ChromaDB get() 한 번에 가져올 청크 수
_DB_PATH = Path(settings.SEARCH_INDEX_DB_PATH)


def rebuild_fts() -> None:
    init_fts_db()  # doc_fts 테이블 보장

    collection = db_manager.get_collection()
    total = collection.count()

    if total == 0:
        logger.warning("ChromaDB가 비어있습니다. 데이터를 먼저 수집해주세요.")
        return

    existing_fts = fts_count()
    logger.info(f"FTS 재구축 시작 | ChromaDB: {total:,}개 청크 | FTS 현재: {existing_fts:,}개")

    # SQLite에 직접 연결해 속도 최적화 pragma 적용
    # (durability 불필요 — 재실행 가능한 rebuild 작업)
    con = sqlite3.connect(str(_DB_PATH), timeout=30)
    con.execute("PRAGMA synchronous = OFF")        # fsync 생략
    con.execute("PRAGMA cache_size = -65536")      # 64 MB 캐시
    con.execute("PRAGMA temp_store = MEMORY")      # 임시 데이터 메모리 사용

    # 기존 FTS 데이터 전체 삭제 (재구축이므로 초기화)
    con.execute("DELETE FROM doc_fts")
    con.commit()

    start = time.monotonic()
    indexed = 0
    offset = 0

    try:
        con.execute("BEGIN")
        while offset < total:
            batch = collection.get(
                limit=_BATCH_SIZE,
                offset=offset,
                include=['documents'],
            )
            if not batch['ids']:
                break

            docs = list(zip(batch['ids'], batch['documents']))
            con.executemany(
                "INSERT INTO doc_fts (chunk_id, content) VALUES (?, ?)",
                docs,
            )
            indexed += len(docs)
            offset += len(docs)

            # 10만 건마다 중간 커밋 (메모리 안전)
            if indexed % 100_000 == 0:
                con.execute("COMMIT")
                con.execute("BEGIN")

            elapsed = time.monotonic() - start
            pct = int(indexed / total * 100)
            speed = indexed / elapsed if elapsed > 0 else 0
            eta = (total - indexed) / speed if speed > 0 else 0
            logger.info(
                f"  {indexed:,}/{total:,} ({pct}%) | "
                f"속도: {speed:.0f}개/s | 남은시간: {timedelta(seconds=int(eta))}"
            )

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        raise
    finally:
        con.execute("PRAGMA synchronous = NORMAL")
        con.close()

    elapsed = time.monotonic() - start
    logger.info(
        f"FTS 재구축 완료 | {indexed:,}개 색인 | "
        f"소요: {timedelta(seconds=int(elapsed))} | "
        f"FTS 총: {fts_count():,}개"
    )


if __name__ == "__main__":
    rebuild_fts()

"""
SQLite FTS5 인덱스 초기 구축 / 재구축 스크립트

ChromaDB에 이미 적재된 모든 문서를 FTS5 인덱스에 등록합니다.
data_loader.py 이후 실행하거나, FTS 인덱스가 비어있을 때 한 번 실행합니다.

사용법:
    PYTHONPATH=src python3 -m company_llm_rag.rebuild_fts
    또는 Docker:
    docker compose run --rm web python -m company_llm_rag.rebuild_fts
"""

import time
from datetime import timedelta

from company_llm_rag.database import db_manager
from company_llm_rag.history_store import init_db, fts_bulk_upsert, fts_count
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_BATCH_SIZE = 1000  # ChromaDB get() 한 번에 가져올 청크 수


def rebuild_fts() -> None:
    init_db()  # doc_fts 테이블 보장

    collection = db_manager.get_collection()
    total = collection.count()

    if total == 0:
        logger.warning("ChromaDB가 비어있습니다. 데이터를 먼저 수집해주세요.")
        return

    existing_fts = fts_count()
    logger.info(f"FTS 재구축 시작 | ChromaDB: {total:,}개 청크 | FTS 현재: {existing_fts:,}개")

    start = time.monotonic()
    indexed = 0
    offset = 0

    while offset < total:
        batch = collection.get(
            limit=_BATCH_SIZE,
            offset=offset,
            include=['documents'],
        )
        if not batch['ids']:
            break

        docs = list(zip(batch['ids'], batch['documents']))
        fts_bulk_upsert(docs)
        indexed += len(docs)
        offset += len(docs)

        elapsed = time.monotonic() - start
        pct = int(indexed / total * 100)
        speed = indexed / elapsed if elapsed > 0 else 0
        eta = (total - indexed) / speed if speed > 0 else 0
        logger.info(
            f"  {indexed:,}/{total:,} ({pct}%) | "
            f"속도: {speed:.0f}개/s | 남은시간: {timedelta(seconds=int(eta))}"
        )

    elapsed = time.monotonic() - start
    logger.info(
        f"FTS 재구축 완료 | {indexed:,}개 색인 | "
        f"소요: {timedelta(seconds=int(elapsed))} | "
        f"FTS 총: {fts_count():,}개"
    )


if __name__ == "__main__":
    rebuild_fts()

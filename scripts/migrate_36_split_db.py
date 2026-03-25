"""
Issue #36 마이그레이션 스크립트: query_history.db → app_data.db + search_index.db

기존 query_history.db 데이터를 새 DB 구조로 이전합니다.
- chat_history (구 query_history) → app_data.db
- app_settings → app_data.db
- doc_fts → search_index.db (rebuild_fts.py로 재구축 권장)

사용법:
    PYTHONPATH=src python3 scripts/migrate_36_split_db.py
    또는 Docker:
    docker compose run --rm web python scripts/migrate_36_split_db.py
"""

import sqlite3
import sys
from pathlib import Path

# PYTHONPATH=src 없이도 동작하도록 sys.path 보정
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

OLD_DB_PATH = Path(settings.CHROMA_DB_PATH).parent / "query_history.db"
APP_DATA_DB_PATH = Path(settings.APP_DATA_DB_PATH)
SEARCH_INDEX_DB_PATH = Path(settings.SEARCH_INDEX_DB_PATH)


def migrate() -> None:
    if not OLD_DB_PATH.exists():
        logger.info(f"기존 DB 없음 ({OLD_DB_PATH}) — 마이그레이션 불필요.")
        return

    if APP_DATA_DB_PATH.exists():
        logger.warning(f"app_data.db가 이미 존재합니다 ({APP_DATA_DB_PATH}). 마이그레이션을 건너뜁니다.")
        logger.warning("강제 재실행하려면 app_data.db를 삭제 후 다시 실행하세요.")
        return

    logger.info(f"마이그레이션 시작: {OLD_DB_PATH} → {APP_DATA_DB_PATH}")

    APP_DATA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    old = sqlite3.connect(str(OLD_DB_PATH))
    old.row_factory = sqlite3.Row
    new = sqlite3.connect(str(APP_DATA_DB_PATH))

    try:
        # ── app_settings 이전 ────────────────────────────────────────
        new.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        settings_rows = old.execute("SELECT key, value FROM app_settings").fetchall()
        new.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            [(r["key"], r["value"]) for r in settings_rows],
        )
        logger.info(f"  app_settings: {len(settings_rows)}건 이전 완료")

        # ── chat_history (구 query_history) 이전 ────────────────────
        old_tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        src_table = "query_history" if "query_history" in old_tables else "chat_history"

        if src_table not in old_tables:
            logger.warning("  query_history / chat_history 테이블 없음 — 이력 이전 건너뜀")
        else:
            # 컬럼 목록 동적 확인
            cols = [row[1] for row in old.execute(f"PRAGMA table_info({src_table})")]

            new.execute(f"""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id        TEXT    NOT NULL,
                    created_at        TEXT    NOT NULL,
                    question          TEXT    NOT NULL,
                    answer            TEXT    NOT NULL,
                    references_json   TEXT    DEFAULT '[]',
                    teams_sent        INTEGER DEFAULT 0,
                    response_time_ms  INTEGER DEFAULT NULL,
                    is_no_answer      INTEGER DEFAULT 0,
                    ref_count         INTEGER DEFAULT 0,
                    ref_sources_json  TEXT    DEFAULT '[]',
                    feedback          INTEGER DEFAULT 0,
                    no_answer_analysis TEXT   DEFAULT NULL,
                    analysis_status   TEXT    DEFAULT NULL,
                    perf_json         TEXT    DEFAULT NULL
                )
            """)

            rows = old.execute(f"SELECT * FROM {src_table}").fetchall()
            if rows:
                col_str = ", ".join(cols)
                placeholders = ", ".join(["?"] * len(cols))
                new.executemany(
                    f"INSERT INTO chat_history ({col_str}) VALUES ({placeholders})",
                    [tuple(r) for r in rows],
                )
            logger.info(f"  chat_history: {len(rows)}건 이전 완료 (원본: {src_table})")

        new.execute("CREATE INDEX IF NOT EXISTS idx_session ON chat_history(session_id)")
        new.execute("CREATE INDEX IF NOT EXISTS idx_created ON chat_history(created_at)")
        new.execute("CREATE INDEX IF NOT EXISTS idx_no_answer ON chat_history(is_no_answer)")
        new.commit()

        logger.info(f"마이그레이션 완료 → {APP_DATA_DB_PATH}")
        logger.info("")
        logger.info("다음 단계:")
        logger.info("  1. 앱을 재시작하여 app_data.db 정상 동작 확인")
        logger.info("  2. FTS 재구축: PYTHONPATH=src python3 -m company_llm_rag.rebuild_fts")
        logger.info(f"  3. 안정화 후 기존 파일 삭제: {OLD_DB_PATH}")

    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        new.close()


if __name__ == "__main__":
    migrate()

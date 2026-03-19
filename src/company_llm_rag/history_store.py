"""
질문 이력 저장소 (SQLite)

세션별 Q&A 이력을 저장하고 조회합니다.
TTL: 히스토리 14일, 세션 만료 기준 7일
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.CHROMA_DB_PATH).parent / "query_history.db"

HISTORY_TTL_DAYS = 14   # 이력 보관 기간
SESSION_TTL_DAYS = 7    # 세션 유효 기간


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """DB 초기화 및 만료 레코드 정리."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS query_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT    NOT NULL,
                created_at   TEXT    NOT NULL,
                question     TEXT    NOT NULL,
                answer       TEXT    NOT NULL,
                references_json TEXT DEFAULT '[]',
                teams_sent   INTEGER DEFAULT 0
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_session ON query_history(session_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_created ON query_history(created_at)")
        con.commit()

    _purge_expired()


def _purge_expired() -> None:
    """14일 초과 레코드 삭제."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    with _conn() as con:
        cur = con.execute("DELETE FROM query_history WHERE created_at < ?", (cutoff,))
        if cur.rowcount:
            logger.info(f"[History] 만료 레코드 {cur.rowcount}건 삭제 (>{HISTORY_TTL_DAYS}일)")
        con.commit()


def save(session_id: str, question: str, answer: str,
         references: List[Dict] = None, teams_sent: bool = False) -> None:
    """Q&A 한 건을 저장합니다."""
    with _conn() as con:
        con.execute(
            """INSERT INTO query_history
               (session_id, created_at, question, answer, references_json, teams_sent)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                datetime.now(timezone.utc).isoformat(),
                question,
                answer,
                json.dumps(references or [], ensure_ascii=False),
                int(teams_sent),
            ),
        )
        con.commit()


def get_session_history(session_id: str) -> List[Dict]:
    """세션의 전체 이력을 반환합니다."""
    with _conn() as con:
        rows = con.execute(
            """SELECT created_at, question, answer, references_json, teams_sent
               FROM query_history
               WHERE session_id = ?
               ORDER BY created_at ASC""",
            (session_id,),
        ).fetchall()

    return [
        {
            "created_at": row["created_at"],
            "question": row["question"],
            "answer": row["answer"],
            "references": json.loads(row["references_json"] or "[]"),
            "teams_sent": bool(row["teams_sent"]),
        }
        for row in rows
    ]

"""
질문 이력 저장소 (SQLite)

세션별 Q&A 이력을 저장하고 조회합니다.
TTL: 히스토리 14일, 세션 만료 기준 7일
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional

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


def _migrate_add_columns(con: sqlite3.Connection) -> None:
    """기존 DB에 신규 컬럼을 추가합니다 (idempotent)."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(query_history)")}
    migrations = [
        ("response_time_ms",   "INTEGER DEFAULT NULL"),
        ("is_no_answer",       "INTEGER DEFAULT 0"),
        ("ref_count",          "INTEGER DEFAULT 0"),
        ("ref_sources_json",   "TEXT    DEFAULT '[]'"),
        ("feedback",           "INTEGER DEFAULT 0"),
        ("no_answer_analysis", "TEXT    DEFAULT NULL"),
        ("analysis_status",    "TEXT    DEFAULT NULL"),
    ]
    for col, definition in migrations:
        if col not in existing:
            con.execute(f"ALTER TABLE query_history ADD COLUMN {col} {definition}")
            logger.info(f"[History] 컬럼 추가: {col}")


def init_db() -> None:
    """DB 초기화 및 만료 레코드 정리."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS query_history (
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
                analysis_status   TEXT    DEFAULT NULL
            )
        """)
        _migrate_add_columns(con)
        con.execute("CREATE INDEX IF NOT EXISTS idx_session ON query_history(session_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_created ON query_history(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_no_answer ON query_history(is_no_answer)")
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


def save(
    session_id: str,
    question: str,
    answer: str,
    references: List[Dict] = None,
    teams_sent: bool = False,
    response_time_ms: Optional[int] = None,
    is_no_answer: bool = False,
) -> int:
    """Q&A 한 건을 저장하고 record_id를 반환합니다."""
    refs = references or []
    sources = list(dict.fromkeys(r.get("source", "unknown") for r in refs))

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO query_history
               (session_id, created_at, question, answer, references_json, teams_sent,
                response_time_ms, is_no_answer, ref_count, ref_sources_json, feedback)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                session_id,
                datetime.now(timezone.utc).isoformat(),
                question,
                answer,
                json.dumps(refs, ensure_ascii=False),
                int(teams_sent),
                response_time_ms,
                int(is_no_answer),
                len(refs),
                json.dumps(sources, ensure_ascii=False),
            ),
        )
        con.commit()
        return cur.lastrowid


def save_feedback(record_id: int, rating: int) -> bool:
    """
    피드백을 저장합니다.

    Args:
        record_id: save()가 반환한 레코드 ID
        rating: 1(👍) 또는 -1(👎)

    Returns:
        저장 성공 여부 (존재하지 않는 record_id면 False)
    """
    with _conn() as con:
        cur = con.execute(
            "UPDATE query_history SET feedback = ? WHERE id = ?",
            (rating, record_id),
        )
        con.commit()
        return cur.rowcount > 0


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


def get_stats(days: int = 14) -> Dict:
    """어드민 대시보드용 통계를 반환합니다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _conn() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM query_history WHERE created_at >= ?", (cutoff,)
        ).fetchone()[0]

        avg_ms = con.execute(
            "SELECT AVG(response_time_ms) FROM query_history WHERE created_at >= ? AND response_time_ms IS NOT NULL",
            (cutoff,)
        ).fetchone()[0]

        no_answer_count = con.execute(
            "SELECT COUNT(*) FROM query_history WHERE created_at >= ? AND is_no_answer = 1",
            (cutoff,)
        ).fetchone()[0]

        thumbs_up = con.execute(
            "SELECT COUNT(*) FROM query_history WHERE created_at >= ? AND feedback = 1",
            (cutoff,)
        ).fetchone()[0]

        thumbs_down = con.execute(
            "SELECT COUNT(*) FROM query_history WHERE created_at >= ? AND feedback = -1",
            (cutoff,)
        ).fetchone()[0]

        source_rows = con.execute(
            "SELECT ref_sources_json FROM query_history WHERE created_at >= ? AND ref_count > 0",
            (cutoff,)
        ).fetchall()

        # 시간대별 분포 (UTC 기준 → JS에서 KST 변환)
        hourly_rows = con.execute(
            """SELECT substr(created_at, 12, 2) as hour, COUNT(*)
               FROM query_history WHERE created_at >= ?
               GROUP BY hour ORDER BY hour""",
            (cutoff,)
        ).fetchall()

        # 최근 👎 질문 (최대 20건)
        bad_rows = con.execute(
            """SELECT id, created_at, question, answer
               FROM query_history
               WHERE feedback = -1
               ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()

    # 소스별 카운트 집계
    source_counts: Dict[str, int] = {}
    for row in source_rows:
        for src in json.loads(row[0] or "[]"):
            source_counts[src] = source_counts.get(src, 0) + 1

    return {
        "period_days": days,
        "total": total,
        "avg_response_ms": round(avg_ms or 0),
        "no_answer_count": no_answer_count,
        "success_rate": round((total - no_answer_count) / total * 100, 1) if total else 0,
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
        "satisfaction_rate": (
            round(thumbs_up / (thumbs_up + thumbs_down) * 100, 1)
            if (thumbs_up + thumbs_down) else None
        ),
        "source_counts": source_counts,
        "hourly": [{"hour": row[0], "count": row[1]} for row in hourly_rows],
        "recent_thumbs_down": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "question": r["question"],
                "answer": r["answer"][:200],
            }
            for r in bad_rows
        ],
    }


# ── 앱 설정 ─────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """앱 설정값을 반환합니다."""
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """앱 설정값을 저장합니다."""
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        con.commit()


# ── 이력 페이지네이션 조회 ────────────────────────────────────────────────────

def get_history_page(
    page: int = 1,
    page_size: int = 20,
    is_no_answer: Optional[int] = None,
    feedback: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
) -> Dict:
    """필터 + 페이지네이션으로 query_history를 조회합니다."""
    conditions = []
    params: list = []

    if is_no_answer is not None:
        conditions.append("is_no_answer = ?")
        params.append(is_no_answer)
    if feedback is not None:
        conditions.append("feedback = ?")
        params.append(feedback)
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        # date_to는 날짜만 오므로 해당 날 끝까지 포함
        conditions.append("created_at < ?")
        params.append(date_to + "T23:59:59.999999")
    if q:
        conditions.append("question LIKE ?")
        params.append(f"%{q}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    with _conn() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM query_history {where}", params
        ).fetchone()[0]

        rows = con.execute(
            f"""SELECT id, session_id, created_at, question, answer,
                       ref_count, ref_sources_json, response_time_ms,
                       is_no_answer, feedback, analysis_status
                FROM query_history {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

    items = [
        {
            "id": r["id"],
            "session_id": r["session_id"],
            "created_at": r["created_at"],
            "question": r["question"],
            "answer_preview": r["answer"][:80] if r["answer"] else "",
            "ref_count": r["ref_count"],
            "ref_sources": json.loads(r["ref_sources_json"] or "[]"),
            "response_time_ms": r["response_time_ms"],
            "is_no_answer": bool(r["is_no_answer"]),
            "feedback": r["feedback"],
            "analysis_status": r["analysis_status"],
        }
        for r in rows
    ]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": items,
    }


def get_record_detail(record_id: int) -> Optional[Dict]:
    """단건 전체 내용 (모달용)."""
    with _conn() as con:
        row = con.execute(
            """SELECT id, session_id, created_at, question, answer,
                      references_json, ref_count, ref_sources_json,
                      response_time_ms, is_no_answer, feedback,
                      no_answer_analysis, analysis_status
               FROM query_history WHERE id = ?""",
            (record_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "created_at": row["created_at"],
        "question": row["question"],
        "answer": row["answer"],
        "references": json.loads(row["references_json"] or "[]"),
        "ref_count": row["ref_count"],
        "ref_sources": json.loads(row["ref_sources_json"] or "[]"),
        "response_time_ms": row["response_time_ms"],
        "is_no_answer": bool(row["is_no_answer"]),
        "feedback": row["feedback"],
        "no_answer_analysis": row["no_answer_analysis"],
        "analysis_status": row["analysis_status"],
    }


def save_analysis(record_id: int, analysis: str, status: str = "done") -> None:
    """답변없음 조사 결과를 저장합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE query_history SET no_answer_analysis = ?, analysis_status = ? WHERE id = ?",
            (analysis, status, record_id),
        )
        con.commit()


def set_analysis_pending(record_id: int) -> None:
    """조사 시작을 표시합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE query_history SET analysis_status = 'pending' WHERE id = ?",
            (record_id,),
        )
        con.commit()

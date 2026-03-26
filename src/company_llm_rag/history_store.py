"""
애플리케이션 운영 데이터 저장소 (app_data.db)

웹 서비스 운영에 직접 연결된 데이터를 저장하고 조회합니다.
- chat_history: 사용자 질문/답변 이력
- app_settings: 웹/어드민 운영 설정

검색 인덱스(doc_fts)는 fts_store.py (search_index.db)에서 별도 관리합니다.

TTL: 이력 14일, 세션 만료 기준 7일
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.APP_DATA_DB_PATH)

HISTORY_TTL_DAYS = 14   # 이력 보관 기간
SESSION_TTL_DAYS = 7    # 세션 유효 기간

_local = threading.local()  # 스레드별 연결 캐시

# get_stats 영구 캐시 (수집 완료 또는 명시적 무효화 시까지 유지)
_stats_cache: Dict[int, Dict] = {}
_stats_cache_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    """스레드별 SQLite 연결을 캐싱하여 반환합니다. PRAGMA는 최초 1회만 실행됩니다."""
    con = getattr(_local, 'con', None)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except Exception:
            _local.con = None

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    journal_mode = settings.SQLITE_JOURNAL_MODE
    actual_journal_mode = con.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
    con.execute("PRAGMA synchronous=NORMAL")
    logger.info(f"[History] SQLite journal_mode={actual_journal_mode}")
    _local.con = con
    return con


def _migrate_add_columns(con: sqlite3.Connection) -> None:
    """기존 DB에 신규 컬럼을 추가합니다 (idempotent)."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(chat_history)")}
    migrations = [
        ("response_time_ms",   "INTEGER DEFAULT NULL"),
        ("is_no_answer",       "INTEGER DEFAULT 0"),
        ("ref_count",          "INTEGER DEFAULT 0"),
        ("ref_sources_json",   "TEXT    DEFAULT '[]'"),
        ("feedback",           "INTEGER DEFAULT 0"),
        ("no_answer_analysis", "TEXT    DEFAULT NULL"),
        ("analysis_status",    "TEXT    DEFAULT NULL"),
        ("perf_json",          "TEXT    DEFAULT NULL"),
        # Issue #37 — 질문 그룹 단위 세션
        ("parent_record_id",   "INTEGER DEFAULT NULL"),
        ("turn_index",         "INTEGER DEFAULT 1"),
        ("group_feedback",     "INTEGER DEFAULT 0"),
        ("group_feedback_at",  "TEXT    DEFAULT NULL"),
        ("retrieved_docs_json", "TEXT    DEFAULT NULL"),
    ]
    for col, definition in migrations:
        if col not in existing:
            con.execute(f"ALTER TABLE chat_history ADD COLUMN {col} {definition}")
            logger.info(f"[History] 컬럼 추가: {col}")


def _migrate_group_fields(con: sqlite3.Connection) -> None:
    """기존 레코드에 turn_index / parent_record_id / group_feedback을 채웁니다 (idempotent).

    같은 session_id 내에서 created_at 오름차순으로 turn_index를 매기고
    직전 레코드를 parent_record_id로 연결합니다.
    group_feedback은 기존 feedback 값이 있는 마지막 턴 기준으로 채웁니다.
    이미 turn_index가 설정된 세션은 건너뜁니다.
    """
    # turn_index가 아직 1로만 남아있는 session만 처리 (신규 컬럼 추가 직후 상태)
    sessions = [
        row[0]
        for row in con.execute(
            """SELECT DISTINCT session_id FROM chat_history
               WHERE turn_index = 1
               GROUP BY session_id HAVING COUNT(*) > 1"""
        )
    ]
    if not sessions:
        return

    updated = 0
    for sid in sessions:
        rows = con.execute(
            "SELECT id, feedback FROM chat_history WHERE session_id = ? ORDER BY created_at ASC",
            (sid,),
        ).fetchall()
        # group_feedback: 마지막으로 피드백이 있는 턴의 값
        group_fb = 0
        for r in rows:
            if r[1] != 0:
                group_fb = r[1]

        prev_id = None
        for idx, row in enumerate(rows, 1):
            con.execute(
                """UPDATE chat_history
                   SET turn_index = ?, parent_record_id = ?, group_feedback = ?
                   WHERE id = ?""",
                (idx, prev_id, group_fb, row[0]),
            )
            prev_id = row[0]
            updated += 1

    if updated:
        logger.info(f"[History] 그룹 필드 마이그레이션: {len(sessions)}개 세션 / {updated}개 레코드")


def init_db() -> None:
    """DB 초기화 및 만료 레코드 정리."""
    from company_llm_rag.fts_store import init_fts_db
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # legacy: query_history → chat_history 마이그레이션
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "query_history" in tables and "chat_history" not in tables:
            con.execute("ALTER TABLE query_history RENAME TO chat_history")
            logger.info("[History] query_history → chat_history 테이블 마이그레이션 완료")
        con.execute("""
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
                perf_json         TEXT    DEFAULT NULL,
                parent_record_id  INTEGER DEFAULT NULL,
                turn_index        INTEGER DEFAULT 1,
                group_feedback    INTEGER DEFAULT 0,
                group_feedback_at TEXT    DEFAULT NULL,
                retrieved_docs_json TEXT    DEFAULT NULL
            )
        """)
        _migrate_add_columns(con)
        con.execute("CREATE INDEX IF NOT EXISTS idx_session ON chat_history(session_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_created ON chat_history(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_no_answer ON chat_history(is_no_answer)")
        _migrate_group_fields(con)
        con.commit()

    init_fts_db()
    _purge_expired()


def _purge_expired() -> None:
    """14일 초과 레코드 삭제."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    with _conn() as con:
        cur = con.execute("DELETE FROM chat_history WHERE created_at < ?", (cutoff,))
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
    perf: Optional[Dict] = None,
    turn_index: int = 1,
    parent_record_id: Optional[int] = None,
    retrieved_docs: Optional[List[Dict]] = None,
) -> int:
    """Q&A 한 건을 저장하고 record_id를 반환합니다."""
    refs = references or []
    sources = list(dict.fromkeys(r.get("source", "unknown") for r in refs))

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO chat_history
               (session_id, created_at, question, answer, references_json, teams_sent,
                response_time_ms, is_no_answer, ref_count, ref_sources_json, feedback,
                perf_json, turn_index, parent_record_id, retrieved_docs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
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
                json.dumps(perf, ensure_ascii=False) if perf else None,
                turn_index,
                parent_record_id,
                json.dumps(retrieved_docs, ensure_ascii=False) if retrieved_docs else None,
            ),
        )
        con.commit()
        return cur.lastrowid


def save_feedback(record_id: int, rating: int) -> bool:
    """턴 단위 피드백을 저장합니다 (하위 호환 유지)."""
    return save_record_feedback(record_id, rating)


def save_record_feedback(record_id: int, rating: int) -> bool:
    """단건 턴 피드백을 저장합니다.

    Args:
        record_id: save()가 반환한 레코드 ID
        rating: 1(👍) 또는 -1(👎)

    Returns:
        저장 성공 여부 (존재하지 않는 record_id면 False)
    """
    with _conn() as con:
        cur = con.execute(
            "UPDATE chat_history SET feedback = ? WHERE id = ?",
            (rating, record_id),
        )
        con.commit()
        return cur.rowcount > 0


def save_group_feedback(session_id: str, rating: int) -> bool:
    """질문 그룹 전체(같은 session_id)에 그룹 피드백을 저장합니다.

    Args:
        session_id: 질문 그룹 ID
        rating: 1(👍) 또는 -1(👎)

    Returns:
        저장 성공 여부 (해당 session_id 레코드가 없으면 False)
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE chat_history SET group_feedback = ?, group_feedback_at = ? WHERE session_id = ?",
            (rating, now, session_id),
        )
        con.commit()
        return cur.rowcount > 0


def get_session_history(session_id: str) -> List[Dict]:
    """세션의 전체 이력을 반환합니다."""
    with _conn() as con:
        rows = con.execute(
            """SELECT created_at, question, answer, references_json, teams_sent
               FROM chat_history
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


def get_session_detail(session_id: str) -> Optional[Dict]:
    """질문 그룹 상세 정보를 반환합니다 (분석·관리자 상세용).

    Returns:
        {session_id, group_feedback, turns: [{id, turn_index, question, answer, ...}]}
        또는 해당 세션이 없으면 None
    """
    with _conn() as con:
        rows = con.execute(
            """SELECT id, turn_index, parent_record_id, created_at,
                      question, answer, references_json, is_no_answer,
                      feedback, group_feedback, group_feedback_at,
                      analysis_status, no_answer_analysis, response_time_ms,
                      perf_json, retrieved_docs_json
               FROM chat_history
               WHERE session_id = ?
               ORDER BY turn_index ASC, created_at ASC""",
            (session_id,),
        ).fetchall()

    if not rows:
        return None

    turns = [
        {
            "id": r["id"],
            "turn_index": r["turn_index"],
            "parent_record_id": r["parent_record_id"],
            "created_at": r["created_at"],
            "question": r["question"],
            "answer": r["answer"],
            "references": json.loads(r["references_json"] or "[]"),
            "is_no_answer": bool(r["is_no_answer"]),
            "feedback": r["feedback"],
            "response_time_ms": r["response_time_ms"],
            "perf": json.loads(r["perf_json"]) if r["perf_json"] else None,
            "analysis_status": r["analysis_status"],
            "no_answer_analysis": r["no_answer_analysis"],
            "retrieved_docs": json.loads(r["retrieved_docs_json"]) if r["retrieved_docs_json"] else [],
        }
        for r in rows
    ]
    return {
        "session_id": session_id,
        "group_feedback": rows[0]["group_feedback"],
        "group_feedback_at": rows[0]["group_feedback_at"],
        "turns": turns,
    }


def get_last_turn_in_session(session_id: str) -> Optional[Dict]:
    """세션의 마지막 턴 record_id와 turn_index를 반환합니다."""
    with _conn() as con:
        row = con.execute(
            """SELECT id, turn_index FROM chat_history
               WHERE session_id = ?
               ORDER BY turn_index DESC, created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "turn_index": row["turn_index"]}


def get_session_groups(
    page: int = 1,
    page_size: int = 20,
    group_feedback: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
) -> Dict:
    """관리자 그룹 뷰: session_id 단위 집계 목록을 반환합니다."""
    conditions = []
    params: list = []

    if group_feedback is not None:
        conditions.append("group_feedback = ?")
        params.append(group_feedback)
    if date_from:
        conditions.append("MIN(created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("MIN(created_at) < ?")
        params.append(date_to + "T23:59:59.999999")
    if q:
        conditions.append("root_question LIKE ?")
        params.append(f"%{q}%")

    having = ("HAVING " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    base_query = f"""
        SELECT session_id,
               MIN(CASE WHEN turn_index = 1 THEN question END) AS root_question,
               COUNT(*) AS turn_count,
               MAX(created_at) AS latest_turn_at,
               MAX(group_feedback) AS group_feedback,
               MAX(CASE WHEN analysis_status IS NOT NULL THEN analysis_status END) AS analysis_status,
               MAX(CASE WHEN teams_sent = 1 THEN 1 ELSE 0 END) AS teams_sent
        FROM chat_history
        GROUP BY session_id
        {having}
    """

    with _conn() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM ({base_query})", params
        ).fetchone()[0]

        rows = con.execute(
            f"{base_query} ORDER BY latest_turn_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

    items = [
        {
            "session_id": r["session_id"],
            "root_question": r["root_question"] or "",
            "turn_count": r["turn_count"],
            "latest_turn_at": r["latest_turn_at"],
            "group_feedback": r["group_feedback"],
            "analysis_status": r["analysis_status"],
            "teams_sent": bool(r["teams_sent"]),
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


def invalidate_stats_cache() -> None:
    """통계 캐시를 무효화합니다. 수집 완료 후 호출하면 다음 조회 시 재계산됩니다."""
    with _stats_cache_lock:
        _stats_cache.clear()
    logger.info("[History] 통계 캐시 무효화")


def get_stats(days: int = 14) -> Dict:
    """어드민 대시보드용 통계를 반환합니다. 영구 캐싱 (invalidate_stats_cache() 호출 전까지)."""
    with _stats_cache_lock:
        if days in _stats_cache:
            return _stats_cache[days]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # 오늘(KST) 기준 시작 시간 계산
    _kst = timezone(timedelta(hours=9))
    _now_kst = datetime.now(_kst)
    _today_kst_start = _now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_cutoff = _today_kst_start.astimezone(timezone.utc).isoformat()

    with _conn() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ?", (cutoff,)
        ).fetchone()[0]

        avg_ms = con.execute(
            "SELECT AVG(response_time_ms) FROM chat_history WHERE created_at >= ? AND response_time_ms IS NOT NULL",
            (cutoff,)
        ).fetchone()[0]

        no_answer_count = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND is_no_answer = 1",
            (cutoff,)
        ).fetchone()[0]

        thumbs_up = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND feedback = 1",
            (cutoff,)
        ).fetchone()[0]

        thumbs_down = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND feedback = -1",
            (cutoff,)
        ).fetchone()[0]

        source_rows = con.execute(
            "SELECT ref_sources_json FROM chat_history WHERE created_at >= ? AND ref_count > 0",
            (cutoff,)
        ).fetchall()

        # 시간대별 분포 (UTC 기준 → JS에서 KST 변환)
        hourly_rows = con.execute(
            """SELECT substr(created_at, 12, 2) as hour,
                      COUNT(*) as total,
                      SUM(CASE WHEN is_no_answer = 0 THEN 1 ELSE 0 END) as answered
               FROM chat_history WHERE created_at >= ?
               GROUP BY hour ORDER BY hour""",
            (cutoff,)
        ).fetchall()

        # 일자별 분포 (UTC 날짜 기준)
        daily_rows = con.execute(
            """SELECT substr(created_at, 1, 10) as day,
                      COUNT(*) as total,
                      SUM(CASE WHEN is_no_answer = 0 THEN 1 ELSE 0 END) as answered
               FROM chat_history WHERE created_at >= ?
               GROUP BY day ORDER BY day""",
            (cutoff,)
        ).fetchall()

        # 오늘 통계 (KST 기준)
        today_total = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ?", (today_cutoff,)
        ).fetchone()[0]

        today_avg_ms = con.execute(
            "SELECT AVG(response_time_ms) FROM chat_history WHERE created_at >= ? AND response_time_ms IS NOT NULL",
            (today_cutoff,)
        ).fetchone()[0]

        today_no_answer = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND is_no_answer = 1",
            (today_cutoff,)
        ).fetchone()[0]

        today_up = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND feedback = 1",
            (today_cutoff,)
        ).fetchone()[0]

        today_down = con.execute(
            "SELECT COUNT(*) FROM chat_history WHERE created_at >= ? AND feedback = -1",
            (today_cutoff,)
        ).fetchone()[0]

        # 최근 👎 질문 (최대 20건)
        bad_rows = con.execute(
            """SELECT id, created_at, question, answer
               FROM chat_history
               WHERE feedback = -1
               ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()

    # 소스별 카운트 집계
    source_counts: Dict[str, int] = {}
    for row in source_rows:
        for src in json.loads(row[0] or "[]"):
            source_counts[src] = source_counts.get(src, 0) + 1

    result = {
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
        "hourly": [{"hour": row[0], "total": row[1], "answered": row[2]} for row in hourly_rows],
        "daily": [{"day": row[0], "total": row[1], "answered": row[2]} for row in daily_rows],
        "today_total": today_total,
        "today_success_rate": round((today_total - today_no_answer) / today_total * 100, 1) if today_total else 0,
        "today_avg_response_ms": round(today_avg_ms or 0),
        "today_thumbs_up": today_up,
        "today_thumbs_down": today_down,
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
    with _stats_cache_lock:
        _stats_cache[days] = result
    return result


# ── 앱 설정 ─────────────────────────────────────────────────────────────────

def set_collection_date(source: str, collected_at: Optional[str] = None) -> None:
    """소스별 최근 수집일자를 app_settings에 저장합니다."""
    value = collected_at or datetime.now(timezone.utc).isoformat()
    set_setting(f"collection_date_{source}", value)


def get_collection_dates() -> dict:
    """소스별 최근 수집일자를 반환합니다."""
    sources = ["jira", "confluence", "sharepoint", "teams", "local"]
    with _conn() as con:
        rows = con.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'collection_date_%'"
        ).fetchall()
    date_map = {row["key"].replace("collection_date_", ""): row["value"] for row in rows}
    return {src: date_map.get(src) for src in sources}


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
    """필터 + 페이지네이션으로 chat_history를 조회합니다."""
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
            f"SELECT COUNT(*) FROM chat_history {where}", params
        ).fetchone()[0]

        rows = con.execute(
            f"""SELECT id, session_id, created_at, question, answer,
                       ref_count, ref_sources_json, response_time_ms,
                       is_no_answer, feedback, group_feedback, analysis_status, perf_json
                FROM chat_history {where}
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
            "group_feedback": r["group_feedback"],
            "analysis_status": r["analysis_status"],
            "perf": json.loads(r["perf_json"]) if r["perf_json"] else None,
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
                      group_feedback, group_feedback_at, turn_index,
                      no_answer_analysis, analysis_status, perf_json
               FROM chat_history WHERE id = ?""",
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
        "group_feedback": row["group_feedback"],
        "group_feedback_at": row["group_feedback_at"],
        "turn_index": row["turn_index"],
        "no_answer_analysis": row["no_answer_analysis"],
        "analysis_status": row["analysis_status"],
        "perf": json.loads(row["perf_json"]) if row["perf_json"] else None,
    }


def save_analysis(record_id: int, analysis: str, status: str = "done") -> None:
    """답변없음 조사 결과를 저장합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE chat_history SET no_answer_analysis = ?, analysis_status = ? WHERE id = ?",
            (analysis, status, record_id),
        )
        con.commit()


def save_group_analysis(session_id: str, analysis: str, status: str = "done") -> None:
    """질문 그룹 전체에 분석 결과를 저장합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE chat_history SET no_answer_analysis = ?, analysis_status = ? WHERE session_id = ?",
            (analysis, status, session_id),
        )
        con.commit()


def set_analysis_pending(record_id: int) -> None:
    """조사 시작을 표시합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE chat_history SET analysis_status = 'pending' WHERE id = ?",
            (record_id,),
        )
        con.commit()


def set_group_analysis_pending(session_id: str) -> None:
    """그룹 조사 시작을 표시합니다."""
    with _conn() as con:
        con.execute(
            "UPDATE chat_history SET analysis_status = 'pending' WHERE session_id = ?",
            (session_id,),
        )
        con.commit()

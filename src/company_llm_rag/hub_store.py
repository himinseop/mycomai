"""
Knowledge Hub 답변 저장소

hub_replies 테이블의 CRUD를 담당합니다.
history_store.py의 _conn()과 init_db()에서 테이블 생성/마이그레이션을 수행합니다.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _conn():
    """history_store와 동일한 SQLite 연결을 공유합니다."""
    from company_llm_rag.history_store import _conn as _shared_conn
    return _shared_conn()


def hub_upsert(doc_id: str, reply_content: str, question: str = "") -> None:
    """Knowledge Hub 답변을 추가합니다. 기존 답변은 비활성화(is_active=0)하고 새 답변을 활성화."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("UPDATE hub_replies SET is_active = 0 WHERE doc_id = ? AND is_active = 1", (doc_id,))
        con.execute(
            "INSERT INTO hub_replies (doc_id, question, reply_content, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (doc_id, question, reply_content, now),
        )
        con.commit()


def hub_get_reply(doc_id: str) -> Optional[str]:
    """Knowledge Hub 현재 활성 답변을 조회합니다."""
    row = _conn().execute(
        "SELECT reply_content FROM hub_replies WHERE doc_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()
    return row["reply_content"] if row else None


def hub_find_duplicate(question: str) -> Optional[str]:
    """동일한 질문이 이미 존재하는지 확인합니다. 존재하면 기존 doc_id 반환."""
    row = _conn().execute(
        "SELECT doc_id FROM hub_replies WHERE question = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (question,),
    ).fetchone()
    return row["doc_id"] if row else None


def hub_get_reply_history(doc_id: str) -> List[Dict]:
    """Knowledge Hub 답변 이력을 조회합니다 (최신순)."""
    rows = _conn().execute(
        "SELECT id, question, reply_content, created_at, is_active FROM hub_replies WHERE doc_id = ? ORDER BY id DESC",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]

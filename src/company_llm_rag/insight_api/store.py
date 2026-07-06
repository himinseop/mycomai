"""
인사이트 API 저장소 (app_data.db)

- api_clients: API Key 클라이언트 (키는 SHA-256 해시로만 저장)
- api_call_history: 호출 이력 (요청 요약만 저장 — 원본 도메인 데이터 미저장)
"""

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import sqlite3

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_local = threading.local()

KEY_PREFIX = "mci_"  # mycomai insight


def _conn() -> sqlite3.Connection:
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(
        Path(settings.APP_DATA_DB_PATH), "InsightAPI", _local, "con"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_insight_db() -> None:
    """인사이트 API 테이블 초기화 (idempotent)."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS api_clients (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                name               TEXT    NOT NULL,
                key_hash           TEXT    NOT NULL UNIQUE,
                scopes             TEXT    NOT NULL DEFAULT '',
                rate_limit_per_min INTEGER DEFAULT NULL,
                is_active          INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS api_call_history (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id         TEXT    NOT NULL,
                client_id          INTEGER NOT NULL,
                domain             TEXT    NOT NULL,
                status             INTEGER NOT NULL,
                request_summary    TEXT    DEFAULT NULL,
                response_summary   TEXT    DEFAULT NULL,
                model              TEXT    DEFAULT NULL,
                prompt_tokens      INTEGER DEFAULT NULL,
                completion_tokens  INTEGER DEFAULT NULL,
                latency_ms         INTEGER DEFAULT NULL,
                error              TEXT    DEFAULT NULL,
                created_at         TEXT    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_client ON api_call_history(client_id, created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_api_calls_domain ON api_call_history(domain, created_at)")
        con.commit()


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# ── 클라이언트 관리 ──────────────────────────────────────────────────────────

def create_client(
    name: str,
    scopes: List[str],
    rate_limit_per_min: Optional[int] = None,
) -> Dict:
    """클라이언트를 생성하고 원문 키를 반환합니다 (원문은 이때 1회만 노출)."""
    raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO api_clients (name, key_hash, scopes, rate_limit_per_min, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, _hash_key(raw_key), ",".join(scopes), rate_limit_per_min, _now()),
        )
        con.commit()
        client_id = cur.lastrowid
    logger.info(f"[InsightAPI] 클라이언트 발급: id={client_id} name={name} scopes={scopes}")
    return {"id": client_id, "name": name, "api_key": raw_key, "scopes": scopes}


def set_client_active(client_id: int, is_active: bool) -> bool:
    """클라이언트 활성/비활성 전환 (비활성 = 즉시 차단, 이력 보존)."""
    with _conn() as con:
        cur = con.execute(
            "UPDATE api_clients SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, client_id),
        )
        con.commit()
    return cur.rowcount > 0


def list_clients() -> List[Dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name, scopes, rate_limit_per_min, is_active, created_at "
            "FROM api_clients ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def find_client_by_key(raw_key: str) -> Optional[Dict]:
    """활성 클라이언트를 키로 조회합니다. 없으면 None."""
    if not raw_key:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT id, name, scopes, rate_limit_per_min, is_active FROM api_clients "
            "WHERE key_hash = ? AND is_active = 1",
            (_hash_key(raw_key),),
        ).fetchone()
    if row is None:
        return None
    client = dict(row)
    client["scopes"] = [s.strip() for s in (client["scopes"] or "").split(",") if s.strip()]
    return client


# ── 호출 이력 ────────────────────────────────────────────────────────────────

def log_call(
    request_id: str,
    client_id: int,
    domain: str,
    status: int,
    request_summary: Optional[Dict] = None,
    response_summary: Optional[Dict] = None,
    model: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
) -> None:
    """호출 이력을 기록합니다. 원본 도메인 데이터는 받지 않습니다(요약만)."""
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO api_call_history "
                "(request_id, client_id, domain, status, request_summary, response_summary, "
                " model, prompt_tokens, completion_tokens, latency_ms, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id, client_id, domain, status,
                    json.dumps(request_summary, ensure_ascii=False) if request_summary else None,
                    json.dumps(response_summary, ensure_ascii=False) if response_summary else None,
                    model, prompt_tokens, completion_tokens, latency_ms,
                    (error or None) and str(error)[:2000],
                    _now(),
                ),
            )
            con.commit()
    except Exception as e:  # 이력 기록 실패가 API 응답을 막으면 안 됨
        logger.error(f"[InsightAPI] 호출 이력 기록 실패: {e}")


def get_call_history(
    limit: int = 50,
    offset: int = 0,
    client_id: Optional[int] = None,
    domain: Optional[str] = None,
) -> Dict:
    """호출 이력 페이지 조회 (관리자용)."""
    where, params = [], []
    if client_id is not None:
        where.append("h.client_id = ?")
        params.append(client_id)
    if domain:
        where.append("h.domain = ?")
        params.append(domain)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with _conn() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM api_call_history h {where_sql}", params
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT h.*, c.name AS client_name FROM api_call_history h "
            f"LEFT JOIN api_clients c ON c.id = h.client_id {where_sql} "
            f"ORDER BY h.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}

"""
그래프 저장소 (#59 Phase 1)

Jira 이슈·담당자·프로젝트·라벨·이슈링크를 SQLite(app_data.db)의
nodes/edges 테이블로 관리합니다. 수만 노드 규모라 그래프 DB는 쓰지 않습니다.

안전 원칙: 질의는 사전 정의된 템플릿 + 파라미터 바인딩만 사용합니다.
LLM이 SQL을 생성하지 않습니다.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(settings.APP_DATA_DB_PATH)
_local = threading.local()

_KST = timezone(timedelta(hours=9))


def _conn() -> sqlite3.Connection:
    from company_llm_rag.sqlite_utils import create_connection
    return create_connection(_DB_PATH, "Graph", _local, "con")


def init_db() -> None:
    """그래프 테이블을 생성합니다 (존재하면 무시)."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                id        TEXT PRIMARY KEY,   -- 예: issue:WMPO-123, person:홍길동
                type      TEXT NOT NULL,      -- issue / person / project / label
                label     TEXT NOT NULL,      -- 표시명 (이슈는 제목)
                meta_json TEXT DEFAULT '{}'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                src_id    TEXT NOT NULL,
                dst_id    TEXT NOT NULL,
                rel       TEXT NOT NULL,      -- HAS_ISSUE / ASSIGNED_TO / REPORTED_BY / HAS_LABEL / LINKS_TO
                meta_json TEXT DEFAULT '{}',
                PRIMARY KEY (src_id, dst_id, rel)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_gnodes_type ON graph_nodes(type)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gedges_src ON graph_edges(src_id, rel)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gedges_dst ON graph_edges(dst_id, rel)")


def rebuild(nodes: Iterable[tuple], edges: Iterable[tuple], node_types: List[str]) -> Dict[str, int]:
    """
    지정 타입의 노드와 그 노드들이 걸린 엣지를 전량 교체합니다 (전체 재구축).

    Args:
        nodes: (id, type, label, meta_json) 튜플 iterable
        edges: (src_id, dst_id, rel, meta_json) 튜플 iterable
        node_types: 교체 대상 노드 타입 목록 (다른 도메인 노드는 보존)
    """
    init_db()
    with _conn() as con:
        placeholders = ",".join("?" * len(node_types))
        con.execute(f"""
            DELETE FROM graph_edges WHERE src_id IN
                (SELECT id FROM graph_nodes WHERE type IN ({placeholders}))
        """, node_types)
        con.execute(f"DELETE FROM graph_nodes WHERE type IN ({placeholders})", node_types)
        node_rows = list(nodes)
        edge_rows = list(edges)
        con.executemany(
            "INSERT OR REPLACE INTO graph_nodes (id, type, label, meta_json) VALUES (?,?,?,?)",
            node_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO graph_edges (src_id, dst_id, rel, meta_json) VALUES (?,?,?,?)",
            edge_rows,
        )
    stats = {"nodes": len(node_rows), "edges": len(edge_rows)}
    logger.info(f"[Graph] 재구축 완료: 노드 {stats['nodes']:,} | 엣지 {stats['edges']:,}")
    return stats


def get_stats() -> Dict:
    """노드/엣지 수를 타입별로 반환합니다."""
    init_db()
    with _conn() as con:
        nodes = dict(con.execute(
            "SELECT type, COUNT(*) FROM graph_nodes GROUP BY type").fetchall())
        edges = dict(con.execute(
            "SELECT rel, COUNT(*) FROM graph_edges GROUP BY rel").fetchall())
    return {"nodes": nodes, "edges": edges}


# ── 조회 템플릿 (파라미터 바인딩만, 자유 SQL 금지) ──────────────────────────

def query_issues(
    project: str = "",
    assignee: str = "",
    label: str = "",
    status: str = "",
    statuses: Optional[List[str]] = None,
    days: int = 0,
    keywords: Optional[List[str]] = None,
    limit: int = 50,
) -> Dict:
    """
    조건에 맞는 Jira 이슈를 전수 조회합니다 (top-K 검색이 아님).

    Returns:
        {"total": 전체 건수, "rows": [{key,title,status,type,priority,assignee,created_at,url}, ...]}
    """
    init_db()
    where = ["n.type = 'issue'"]
    params: List = []

    if project:
        where.append("json_extract(n.meta_json, '$.project') = ?")
        params.append(project.upper())
    if statuses:
        ph = ",".join("?" * len(statuses))
        where.append(f"json_extract(n.meta_json, '$.status') IN ({ph})")
        params.extend(statuses)
    elif status:
        where.append("json_extract(n.meta_json, '$.status') LIKE ?")
        params.append(f"%{status}%")
    if days and days > 0:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        where.append("json_extract(n.meta_json, '$.created_at') >= ?")
        params.append(cutoff)
    if assignee:
        where.append("""n.id IN (
            SELECT src_id FROM graph_edges
            WHERE rel IN ('ASSIGNED_TO','REPORTED_BY') AND dst_id = ?)""")
        params.append(f"person:{assignee}")
    if label:
        where.append("n.id IN (SELECT src_id FROM graph_edges WHERE rel='HAS_LABEL' AND dst_id = ?)")
        params.append(f"label:{label}")
    for kw in (keywords or [])[:5]:
        kw = (kw or "").strip()
        if kw:
            where.append("(n.label LIKE ? OR json_extract(n.meta_json, '$.summary_text') LIKE ?)")
            params.extend([f"%{kw}%", f"%{kw}%"])

    where_sql = " AND ".join(where)
    with _conn() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM graph_nodes n WHERE {where_sql}", params
        ).fetchone()[0]
        rows = con.execute(f"""
            SELECT n.id, n.label, n.meta_json FROM graph_nodes n
            WHERE {where_sql}
            ORDER BY json_extract(n.meta_json, '$.created_at') DESC
            LIMIT ?
        """, params + [limit]).fetchall()

    results = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        results.append({
            "key": r["id"].split(":", 1)[1],
            "title": r["label"],
            "status": meta.get("status", ""),
            "type": meta.get("issue_type", ""),
            "priority": meta.get("priority", ""),
            "assignee": meta.get("assignee", ""),
            "created_at": (meta.get("created_at") or "")[:10],
            "url": meta.get("url", ""),
        })
    return {"total": total, "rows": results}


def linked_issues(issue_key: str) -> List[Dict]:
    """이슈와 링크로 연결된 이웃 이슈 목록을 반환합니다."""
    init_db()
    node_id = f"issue:{issue_key.upper()}"
    with _conn() as con:
        rows = con.execute("""
            SELECT e.dst_id AS other, e.meta_json AS emeta, n.label, n.meta_json
            FROM graph_edges e JOIN graph_nodes n ON n.id = e.dst_id
            WHERE e.rel = 'LINKS_TO' AND e.src_id = ?
            UNION
            SELECT e.src_id AS other, e.meta_json AS emeta, n.label, n.meta_json
            FROM graph_edges e JOIN graph_nodes n ON n.id = e.src_id
            WHERE e.rel = 'LINKS_TO' AND e.dst_id = ?
        """, (node_id, node_id)).fetchall()
    results = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        emeta = json.loads(r["emeta"] or "{}")
        results.append({
            "key": r["other"].split(":", 1)[1],
            "title": r["label"],
            "link_type": emeta.get("link_type", ""),
            "status": meta.get("status", ""),
            "url": meta.get("url", ""),
        })
    return results

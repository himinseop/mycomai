"""
플랫폼매뉴얼 백본 엔티티 링크 (#59 Phase 2)

플랫폼매뉴얼(source=docs) 문서를 엔티티의 백본으로 삼아
Jira 이슈·Confluence 페이지를 MENTIONS 엣지로 연결합니다.

- 엔티티 사전: 매뉴얼 문서 단위의 시드 사전 (표준 용어 + 사내 동의어).
  LLM 추출 없이 문자열 매칭만 사용 — 비용 0, 재현 가능.
- 서빙: 질문에서 엔티티를 감지하면 매뉴얼 청크 + 최근 관련 Jira/Confluence
  문서를 검색 후보에 주입 → "정책 현황(매뉴얼) + 변경 이력(Jira)" 멀티홉.
"""

import json
from typing import Dict, List, Optional

from company_llm_rag.graph import graph_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_MANUAL_DIR = "guides/documents/platform/features"

# 시드 엔티티 사전 — 매뉴얼 문서 1개 = 엔티티 1개.
# aliases는 Jira/Confluence 제목·본문 및 사용자 질문에서 매칭할 사내 용어.
# 주의: 1글자·과도하게 일반적인 단어는 넣지 않는다 (과잉 링크 방지).
SEED_ENTITIES: List[Dict] = [
    {"name": "QR 테이블 주문", "manual": "qr-table-order.md",
     "aliases": ["테이블오더", "테이블 오더", "QR주문", "QR 주문", "스마트오더", "전자메뉴판", "테이블주문", "테이블 주문", "직원호출", "직원 호출"]},
    {"name": "배달·픽업 주문", "manual": "delivery-pickup-order.md",
     "aliases": ["배달주문", "배달 주문", "픽업주문", "픽업 주문", "배달접수", "픽업대기"]},
    {"name": "키오스크 주문", "manual": "kiosk-order.md",
     "aliases": ["키오스크"]},
    {"name": "회원", "manual": "member.md",
     "aliases": ["회원가입", "회원 가입", "본인인증", "본인 인증", "회원탈퇴", "회원 탈퇴", "휴면회원", "휴면 회원"]},
    {"name": "알림", "manual": "notification.md",
     "aliases": ["알림톡", "푸시알림", "푸시 알림", "웹푸시", "앱푸시", "카카오 알림"]},
    {"name": "POS 실시간 통신", "manual": "pos-socket.md",
     "aliases": ["POS 소켓", "소켓 통신", "주문접수 프로그램", "주문접수 POS", "주문 알림", "자동주문접수", "자동 접수"]},
    {"name": "매출·정산", "manual": "sales-settlement.md",
     "aliases": ["정산", "정산주기", "정산 주기", "지급대상", "정산내역", "세금계산서", "부가세 신고"]},
    {"name": "정기구독", "manual": "subscription.md",
     "aliases": ["구독", "정기결제", "정기 결제", "구독권", "구독 혜택"]},
    {"name": "업체·매장", "manual": "company-store.md",
     "aliases": ["입점", "업체 등록", "매장 등록", "폐점 처리", "매장 관리"]},
]


def _entity_id(name: str) -> str:
    return f"entity:{name}"


def rebuild_entities(confluence_docs: Optional[List[Dict]] = None) -> Dict[str, int]:
    """
    엔티티 노드 + MENTIONS 엣지를 재구축합니다.

    - Confluence 문서는 doc 노드로 적재 (title/url/doc_id)
    - MENTIONS: (issue|doc) → entity, 제목·본문 앞부분의 별칭 문자열 매칭
    - Jira 이슈 노드는 이미 그래프에 있어야 함 (jira_graph.rebuild 이후 호출)
    """
    graph_store.init_db()
    nodes: Dict[str, tuple] = {}
    edges: Dict[tuple, tuple] = {}

    for ent in SEED_ENTITIES:
        eid = _entity_id(ent["name"])
        nodes[eid] = (eid, "entity", ent["name"], json.dumps({
            "aliases": ent["aliases"],
            "manual_relpath": f"{_MANUAL_DIR}/{ent['manual']}",
        }, ensure_ascii=False))

    # Confluence 페이지 → doc 노드
    for d in (confluence_docs or []):
        doc_id = d.get("id") or ""
        title = d.get("title") or ""
        if not doc_id or not title:
            continue
        nid = f"doc:{doc_id}"
        nodes[nid] = (nid, "doc", title, json.dumps({
            "source": "confluence",
            "url": d.get("url", ""),
            "original_doc_id": doc_id,
            "updated_at": d.get("updated_at", ""),
            "summary_text": (d.get("content") or "")[:300],
        }, ensure_ascii=False))

    # doc(Confluence) → entity MENTIONS (메모리 매칭 — 문서 수백 건 수준)
    # 제목 매칭만 사용 — 본문 매칭은 스치듯 언급된 문서까지 연결해 노이즈가 큼
    for d in (confluence_docs or []):
        doc_id = d.get("id") or ""
        if not doc_id:
            continue
        text = d.get("title") or ""
        for ent in SEED_ENTITIES:
            terms = [ent["name"]] + ent["aliases"]
            if any(t in text for t in terms):
                edges[(f"doc:{doc_id}", _entity_id(ent["name"]), "MENTIONS")] = (
                    f"doc:{doc_id}", _entity_id(ent["name"]), "MENTIONS", "{}")

    # 기존 entity/doc 노드 교체 (issue 노드는 보존)
    graph_store.rebuild(nodes.values(), edges.values(), ["entity", "doc"])

    # issue → entity MENTIONS (SQL LIKE — 이슈 노드는 DB에 있음)
    import sqlite3
    from company_llm_rag.config import settings
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    try:
        # 별칭 변경 시 낡은 링크가 남지 않도록 issue→entity MENTIONS 전량 재생성
        con.execute("""
            DELETE FROM graph_edges WHERE rel='MENTIONS' AND src_id IN
                (SELECT id FROM graph_nodes WHERE type='issue')
        """)
        edge_count = 0
        for ent in SEED_ENTITIES:
            eid = _entity_id(ent["name"])
            for term in [ent["name"]] + ent["aliases"]:
                # 제목 매칭만 사용 — 본문 매칭은 스치듯 언급된 이슈까지 끌어와 노이즈가 큼
                rows = con.execute("""
                    SELECT id FROM graph_nodes
                    WHERE type='issue' AND label LIKE ?
                """, (f"%{term}%",)).fetchall()
                if rows:
                    con.executemany(
                        "INSERT OR IGNORE INTO graph_edges (src_id, dst_id, rel, meta_json) VALUES (?,?,'MENTIONS','{}')",
                        [(r[0], eid) for r in rows])
                    edge_count += len(rows)
        con.commit()
    finally:
        con.close()

    stats = {"entities": len(SEED_ENTITIES), "conf_docs": len(confluence_docs or []),
             "issue_mentions": edge_count}
    logger.info(f"[Graph] 엔티티 링크 재구축: {stats}")
    return stats


# ── 서빙: 질문 → 엔티티 감지 → 연결 문서 주입 ──────────────────────────────

_INJECT_MAX_ENTITIES = 2   # 질문에서 사용할 엔티티 수 상한
_INJECT_ISSUES = 3         # 엔티티당 주입할 최근 Jira 이슈 수
_INJECT_CONF_DOCS = 2      # 엔티티당 주입할 Confluence 문서 수
_INJECT_MANUAL_CHUNKS = 2  # 매뉴얼 문서당 주입할 청크 수


def detect_entities(query: str) -> List[Dict]:
    """질문 문자열에서 시드 엔티티를 감지합니다 (별칭 부분 문자열 매칭)."""
    q = (query or "").strip()
    if not q:
        return []
    found = []
    for ent in SEED_ENTITIES:
        terms = [ent["name"]] + ent["aliases"]
        matched = next((t for t in terms if t in q), None)
        if matched:
            found.append({**ent, "matched": matched})
    # 매칭 문자열이 긴 순서 = 더 구체적인 엔티티 우선
    found.sort(key=lambda e: len(e["matched"]), reverse=True)
    return found[:_INJECT_MAX_ENTITIES]


def _mentioned_nodes(entity_name: str, node_type: str, limit: int) -> List[Dict]:
    """엔티티를 언급한 노드를 최근 순으로 반환합니다."""
    import sqlite3
    from company_llm_rag.config import settings
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        date_key = "$.created_at" if node_type == "issue" else "$.updated_at"
        rows = con.execute(f"""
            SELECT n.id, n.label, n.meta_json FROM graph_edges e
            JOIN graph_nodes n ON n.id = e.src_id
            WHERE e.rel='MENTIONS' AND e.dst_id = ? AND n.type = ?
            ORDER BY json_extract(n.meta_json, '{date_key}') DESC
            LIMIT ?
        """, (_entity_id(entity_name), node_type, limit)).fetchall()
        return [{"id": r["id"], "label": r["label"],
                 "meta": json.loads(r["meta_json"] or "{}")} for r in rows]
    finally:
        con.close()


def inject_entity_docs(query: str, retrieved_docs: List[Dict]) -> List[Dict]:
    """
    질문에서 엔티티가 감지되면 매뉴얼 청크 + 최근 관련 Jira/Confluence 문서를
    검색 결과에 주입합니다 (_injected 플래그, 이미 있으면 스킵).
    """
    from company_llm_rag.config import settings
    if not settings.GRAPH_ENTITY_INJECT_ENABLED:
        return retrieved_docs
    try:
        entities = detect_entities(query)
        if not entities:
            return retrieved_docs

        from company_llm_rag.database import db_manager
        collection = db_manager.get_collection()

        present_docs = {d["metadata"].get("original_doc_id", "") for d in retrieved_docs}
        present_keys = {d["metadata"].get("jira_issue_key", "") for d in retrieved_docs}
        injected: List[Dict] = []

        def _fetch(where: dict, limit: int) -> List[Dict]:
            res = collection.get(where=where, include=["documents", "metadatas"], limit=limit)
            return [{"content": res["documents"][i], "metadata": res["metadatas"][i],
                     "_distance": 0.0, "_injected": True}
                    for i in range(len(res.get("ids", [])))]

        for ent in entities:
            # 1) 매뉴얼 청크 (해당 문서가 검색 결과에 없을 때만)
            relpath = f"{_MANUAL_DIR}/{ent['manual']}"
            if f"docs-{relpath}" not in present_docs:
                injected.extend(_fetch({"docs_relpath": {"$eq": relpath}}, _INJECT_MANUAL_CHUNKS))

            # 2) 최근 관련 Jira 이슈
            for node in _mentioned_nodes(ent["name"], "issue", _INJECT_ISSUES):
                key = node["id"].split(":", 1)[1]
                if key in present_keys or node["meta"].get("placeholder"):
                    continue
                injected.extend(_fetch({"jira_issue_key": {"$eq": key}}, 1))

            # 3) 관련 Confluence 문서
            for node in _mentioned_nodes(ent["name"], "doc", _INJECT_CONF_DOCS):
                doc_id = node["meta"].get("original_doc_id", "")
                if not doc_id or doc_id in present_docs:
                    continue
                injected.extend(_fetch({"original_doc_id": {"$eq": doc_id}}, 1))

        if injected:
            logger.info(
                f"[Graph] 엔티티 주입: {[e['name'] for e in entities]} → {len(injected)}개 청크")
        return injected + retrieved_docs
    except Exception as e:
        logger.error(f"[Graph] 엔티티 주입 실패 (원본 결과 유지): {e}", exc_info=True)
        return retrieved_docs

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

_MANUAL_DIR = "platform"

# 시드 엔티티 사전 — 매뉴얼 문서 1개 = 엔티티 1개 (manual은 platform/ 기준 상대 경로).
# aliases는 Jira/Confluence 제목 및 사용자 질문에서 매칭할 사내 용어.
# 주의: 1글자·과도하게 일반적인 단어는 넣지 않는다 (과잉 링크 방지).
SEED_ENTITIES: List[Dict] = [
    # ── 기능 가이드 (features) ──
    {"name": "QR 테이블 주문", "manual": "features/qr-table-order.md",
     "aliases": ["테이블오더", "테이블 오더", "QR주문", "QR 주문", "스마트오더", "전자메뉴판", "테이블주문", "테이블 주문", "직원호출", "직원 호출"]},
    {"name": "배달·픽업 주문", "manual": "features/delivery-pickup-order.md",
     "aliases": ["배달주문", "배달 주문", "픽업주문", "픽업 주문", "배달접수", "픽업대기"]},
    {"name": "배송 주문", "manual": "features/shipping.md",
     "aliases": ["배송주문", "택배", "송장번호", "송장 번호"]},
    {"name": "키오스크 주문", "manual": "features/kiosk-order.md",
     "aliases": ["키오스크"]},
    {"name": "회원", "manual": "features/member.md",
     "aliases": ["회원가입", "회원 가입", "본인인증", "본인 인증", "회원탈퇴", "회원 탈퇴", "휴면회원", "휴면 회원"]},
    {"name": "알림", "manual": "features/notification.md",
     "aliases": ["알림톡", "푸시알림", "푸시 알림", "웹푸시", "앱푸시", "카카오 알림"]},
    {"name": "POS 실시간 통신", "manual": "features/pos-socket.md",
     "aliases": ["POS 소켓", "소켓 통신", "주문접수 프로그램", "주문접수 POS", "주문 알림", "자동주문접수", "자동 접수"]},
    {"name": "매출·정산", "manual": "features/sales-settlement.md",
     "aliases": ["정산", "정산주기", "정산 주기", "지급대상", "정산내역", "세금계산서", "부가세 신고"]},
    {"name": "정기구독", "manual": "features/subscription.md",
     "aliases": ["구독", "정기결제", "정기 결제", "구독권", "구독 혜택"]},
    {"name": "업체·매장", "manual": "features/company-store.md",
     "aliases": ["입점", "업체 등록", "매장 등록", "폐점 처리", "매장 관리"]},
    {"name": "쿠폰", "manual": "features/coupon.md",
     "aliases": ["할인쿠폰", "할인 쿠폰", "배달비 쿠폰", "메뉴 쿠폰", "쿠폰 발급", "프로모션 코드", "쿠폰 수기지급"]},
    {"name": "E쿠폰", "manual": "features/ecoupon.md",
     "aliases": ["이쿠폰", "메뉴교환권", "메뉴 교환권", "교환권", "금액권", "선물하기"]},
    {"name": "포인트", "manual": "features/point.md",
     "aliases": ["통합포인트", "통합 포인트", "포인트 적립", "포인트 사용", "적립금"]},
    {"name": "매장 포인트", "manual": "features/store-point.md",
     "aliases": ["매장포인트"]},
    {"name": "상품·메뉴", "manual": "features/product-menu.md",
     "aliases": ["상품 관리", "메뉴 관리", "옵션그룹", "옵션 그룹", "메뉴판", "추천메뉴", "원산지"]},
    {"name": "후기", "manual": "features/review.md",
     "aliases": ["리뷰", "별점", "후기 신고", "후기 답글"]},
    {"name": "티켓", "manual": "features/ticket.md",
     "aliases": ["티켓형 상품", "딜 상품", "입장권", "사용처"]},
    {"name": "위메프오플러스", "manual": "features/wmpoplus.md",
     "aliases": ["플러스 서비스", "브랜드관", "프랜차이즈 본사"]},
    # ── 사이트 가이드 (sites) ──
    {"name": "위메프오 통합 어드민", "manual": "sites/admin.md",
     "aliases": ["통합 어드민", "통합어드민", "어드민 사이트", "관리자 사이트"]},
    {"name": "위메프오 파트너스", "manual": "sites/partners.md",
     "aliases": ["파트너스", "사장님 사이트", "사장님 페이지"]},
    {"name": "위메프오 주문접수 사이트", "manual": "sites/pos.md",
     "aliases": ["주문접수 사이트", "주문접수 화면", "주문 목록 화면"]},
    {"name": "위메프오 고객 사이트", "manual": "sites/wmpo.md",
     "aliases": ["위메프오 앱", "위메프오 웹", "고객 앱"]},
    {"name": "위메프오플러스 어드민", "manual": "sites/wmpoplus-admin.md",
     "aliases": ["플러스 어드민", "브랜드 어드민", "플러스어드민"]},
    {"name": "위메프오플러스 고객 사이트", "manual": "sites/wmpoplus.md",
     "aliases": ["플러스 앱", "플러스 웹"]},
]


def _entity_id(name: str) -> str:
    return f"entity:{name}"


# ── 엔티티 사전 (DB 기반, 관리자 편집 가능 — SEED_ENTITIES는 최초 시드) ─────

import time as _time

_ent_cache: Optional[List[Dict]] = None
_ent_cache_at: float = 0.0
_ENT_CACHE_TTL = 60.0


def init_entities_table() -> None:
    """entities 테이블을 생성하고, 비어 있으면 시드 사전으로 채웁니다."""
    import sqlite3
    from company_llm_rag.config import settings
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                manual     TEXT NOT NULL DEFAULT '',  -- platform/ 기준 상대 경로
                aliases    TEXT NOT NULL DEFAULT '',  -- 콤마 구분
                is_active  INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT
            )
        """)
        if con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            con.executemany(
                "INSERT INTO entities (name, manual, aliases, is_active, updated_at) VALUES (?,?,?,1,?)",
                [(e["name"], e["manual"], ",".join(e["aliases"]), now) for e in SEED_ENTITIES])
            logger.info(f"[Graph] 엔티티 사전 시드 적재: {len(SEED_ENTITIES)}개")
        con.commit()
    finally:
        con.close()


def get_entities(active_only: bool = True, use_cache: bool = True) -> List[Dict]:
    """엔티티 사전을 반환합니다 (60초 캐시)."""
    global _ent_cache, _ent_cache_at
    if use_cache and active_only and _ent_cache is not None \
            and _time.monotonic() - _ent_cache_at < _ENT_CACHE_TTL:
        return _ent_cache
    import sqlite3
    from company_llm_rag.config import settings
    init_entities_table()
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        where = "WHERE is_active = 1" if active_only else ""
        rows = con.execute(f"SELECT * FROM entities {where} ORDER BY id").fetchall()
    finally:
        con.close()
    result = [{
        "id": r["id"], "name": r["name"], "manual": r["manual"],
        "aliases": [a.strip() for a in (r["aliases"] or "").split(",") if a.strip()],
        "is_active": bool(r["is_active"]),
    } for r in rows]
    if active_only:
        _ent_cache, _ent_cache_at = result, _time.monotonic()
    return result


def save_entities(entries: List[Dict]) -> Dict:
    """엔티티 사전을 전체 교체합니다 (관리자 편집 저장)."""
    global _ent_cache
    cleaned = []
    seen_names = set()
    for e in entries:
        name = (e.get("name") or "").strip()
        manual = (e.get("manual") or "").strip()
        aliases = e.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",")]
        aliases = [a for a in (a.strip() for a in aliases) if len(a) >= 2]
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        cleaned.append((name, manual, ",".join(aliases),
                        1 if e.get("is_active", True) else 0))
    if not cleaned:
        raise ValueError("엔티티가 1개 이상 필요합니다")

    import sqlite3
    from company_llm_rag.config import settings
    from datetime import datetime, timezone
    init_entities_table()
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    try:
        con.execute("DELETE FROM entities")
        con.executemany(
            "INSERT INTO entities (name, manual, aliases, is_active, updated_at) VALUES (?,?,?,?,?)",
            [(n, m, a, act, now) for n, m, a, act in cleaned])
        con.commit()
    finally:
        con.close()
    _ent_cache = None
    logger.info(f"[Graph] 엔티티 사전 저장: {len(cleaned)}개")
    return {"saved": len(cleaned)}


def rebuild_entities(confluence_docs: Optional[List[Dict]] = None) -> Dict[str, int]:
    """
    엔티티 노드 + MENTIONS 엣지를 재구축합니다.

    - 엔티티 정의는 DB 사전(get_entities) 사용
    - confluence_docs가 주어지면 doc 노드를 교체, None이면 기존 doc 노드 보존
    - MENTIONS: (issue|doc) → entity, **제목 매칭만** (본문 매칭은 노이즈가 컸음)
    - Jira 이슈 노드는 이미 그래프에 있어야 함 (jira_graph.rebuild 이후 호출)
    """
    graph_store.init_db()
    entities = get_entities(use_cache=False)
    nodes: Dict[str, tuple] = {}

    for ent in entities:
        eid = _entity_id(ent["name"])
        nodes[eid] = (eid, "entity", ent["name"], json.dumps({
            "aliases": ent["aliases"],
            "manual_relpath": f"{_MANUAL_DIR}/{ent['manual']}" if ent["manual"] else "",
        }, ensure_ascii=False))

    # Confluence 페이지 → doc 노드 (미제공 시 기존 노드 보존)
    replace_types = ["entity"]
    if confluence_docs is not None:
        replace_types.append("doc")
        for d in confluence_docs:
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
            }, ensure_ascii=False))

    graph_store.rebuild(nodes.values(), [], replace_types)

    # MENTIONS 전량 재생성 (별칭 변경 시 낡은 링크 제거)
    import sqlite3
    from company_llm_rag.config import settings
    con = sqlite3.connect(settings.APP_DATA_DB_PATH)
    try:
        con.execute("DELETE FROM graph_edges WHERE rel='MENTIONS'")
        edge_count = 0
        for ent in entities:
            eid = _entity_id(ent["name"])
            for term in [ent["name"]] + ent["aliases"]:
                rows = con.execute("""
                    SELECT id FROM graph_nodes
                    WHERE type IN ('issue', 'doc') AND label LIKE ?
                """, (f"%{term}%",)).fetchall()
                if rows:
                    con.executemany(
                        "INSERT OR IGNORE INTO graph_edges (src_id, dst_id, rel, meta_json) VALUES (?,?,'MENTIONS','{}')",
                        [(r[0], eid) for r in rows])
                    edge_count += len(rows)
        con.commit()
        doc_count = con.execute("SELECT COUNT(*) FROM graph_nodes WHERE type='doc'").fetchone()[0]
    finally:
        con.close()

    stats = {"entities": len(entities), "conf_docs": doc_count, "mentions": edge_count}
    logger.info(f"[Graph] 엔티티 링크 재구축: {stats}")
    return stats


# ── 서빙: 질문 → 엔티티 감지 → 연결 문서 주입 ──────────────────────────────

_INJECT_MAX_ENTITIES = 2   # 질문에서 사용할 엔티티 수 상한
_INJECT_ISSUES = 3         # 엔티티당 주입할 최근 Jira 이슈 수
_INJECT_CONF_DOCS = 2      # 엔티티당 주입할 Confluence 문서 수
_INJECT_MANUAL_CHUNKS = 2  # 매뉴얼 문서당 주입할 청크 수


def detect_entities(query: str) -> List[Dict]:
    """질문 문자열에서 엔티티를 감지합니다 (별칭 부분 문자열 매칭, DB 사전)."""
    q = (query or "").strip()
    if not q:
        return []
    found = []
    for ent in get_entities():
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
            if ent.get("manual"):
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

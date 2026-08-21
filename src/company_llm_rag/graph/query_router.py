"""
그래프 질의 라우팅 (#59 Phase 1)

query_rewriter가 intent=aggregate로 분류한 질문(목록·집계·"최근 ~들")을
top-K 벡터 검색 대신 그래프 전수 조회로 응답합니다.

- 조회는 graph_store의 사전 정의 템플릿만 사용 (자유 SQL 금지)
- 결과 0건이면 None을 반환해 기존 RAG 파이프라인으로 폴백
"""

from typing import Dict, List, Optional, Tuple

from company_llm_rag.config import settings
from company_llm_rag.graph import graph_store
from company_llm_rag.llm.factory import resolve_llm, current_model_name
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_MAX_LIST_ROWS = 50      # 답변에 나열할 최대 행 수 (총 건수는 별도 표기)
_MAX_REF_ROWS = 5        # 참고문서로 노출할 최대 이슈 수

# 수치·목록은 서버가 확정 조립 — LLM은 경향 요약만 (insight API와 동일 원칙)
_TREND_PROMPT = """당신은 사내 Jira 이슈 목록을 검토하는 어시스턴트입니다.
아래 이슈 목록에서 눈에 띄는 경향을 한국어 1~2문장으로 요약하세요
(반복되는 주제, 상태 분포, 특정 담당자/기간 집중 등).

- 건수·통계 수치를 직접 세지 마세요. 수치는 이미 사용자에게 표시되어 있습니다.
- 목록에 없는 내용을 지어내지 마세요. 특별한 경향이 없으면 "NONE"만 출력하세요."""


# 한글 상태 표현 → Jira 상태값 그룹 (실데이터 분포 기준)
_OPEN_STATUSES = ["Open", "To Do", "In Progress", "In Review",
                  "Ready for Development", "Reopened", "Frozen"]
_CLOSED_STATUSES = ["Closed", "Done", "Resolved"]


def _resolve_status(raw: str) -> Dict:
    """상태 필터 단어를 statuses(IN) 또는 status(LIKE) 파라미터로 변환합니다."""
    s = (raw or "").strip()
    if not s:
        return {"status": "", "statuses": None}
    low = s.lower()
    if any(t in low for t in ("진행", "미완료", "오픈", "열려", "open", "todo", "to do")):
        return {"status": "", "statuses": _OPEN_STATUSES}
    if any(t in low for t in ("완료", "닫힌", "종료", "해결", "close", "done", "resolve")):
        return {"status": "", "statuses": _CLOSED_STATUSES}
    return {"status": s, "statuses": None}


def _build_filter(rewrite: Dict) -> Dict:
    """rewriter가 구조화한 aggregate_filter를 조회 파라미터로 정리합니다."""
    f = rewrite.get("aggregate_filter") or {}
    days = f.get("days") or 0
    try:
        days = max(0, int(days))
    except (TypeError, ValueError):
        days = 0
    return {
        "project": (f.get("project") or "").strip(),
        "assignee": (f.get("assignee") or "").strip(),
        "label": (f.get("label") or "").strip(),
        "days": days,
        "keywords": [k for k in (f.get("keywords") or []) if isinstance(k, str) and k.strip()],
        **_resolve_status(f.get("status") or ""),
    }


def try_aggregate_answer(
    question: str,
    rewrite: Dict,
) -> Optional[Tuple[str, List[Dict], Dict]]:
    """
    그래프 집계 경로로 답변을 시도합니다.

    Returns:
        (answer, references, info) — 그래프 경로 성공 시
        None — 결과 빈약/오류 시 (호출측은 기존 파이프라인으로 폴백)
    """
    if not settings.GRAPH_AGGREGATE_ENABLED:
        return None
    try:
        filters = _build_filter(rewrite)
        # 조건이 하나도 없으면 전체 덤프가 되므로 그래프 경로를 쓰지 않음
        if not any([filters["project"], filters["assignee"], filters["label"],
                    filters["status"], filters["statuses"], filters["days"], filters["keywords"]]):
            logger.info("[Graph] aggregate 조건 없음 → 기존 파이프라인 폴백")
            return None

        result = graph_store.query_issues(limit=_MAX_LIST_ROWS, **filters)
        if result["total"] == 0:
            logger.info(f"[Graph] 조회 0건 (filter={filters}) → 기존 파이프라인 폴백")
            return None

        rows = result["rows"]
        total = result["total"]
        logger.info(f"[Graph] aggregate 조회: total={total} 표시={len(rows)} filter={filters}")

        # ── 서버가 확정 조립: 기준·총 건수·목록 (LLM 수치 오류 원천 차단) ──
        cond_parts = []
        if filters["project"]:
            cond_parts.append(f"{filters['project']} 프로젝트")
        if filters["assignee"]:
            cond_parts.append(f"담당자 {filters['assignee']}")
        if filters["days"]:
            cond_parts.append(f"최근 {filters['days']}일")
        if filters["statuses"]:
            cond_parts.append("미완료 상태" if filters["statuses"][0] in ("Open", "To Do") else "완료 상태")
        elif filters["status"]:
            cond_parts.append(f"상태 '{filters['status']}'")
        if filters["keywords"]:
            cond_parts.append("·".join(f"'{k}'" for k in filters["keywords"]) + " 관련")
        cond_text = " ".join(cond_parts) if cond_parts else "조건에 해당하는"
        intro = f"{cond_text} 이슈는 총 **{total}건**입니다."

        def _link(r):
            key_md = f"[{r['key']}]({r['url']})" if r.get("url") else f"[{r['key']}]"
            return (f"- **{key_md} {r['title']}** | 상태: {r['status'] or '-'} "
                    f"| 담당자: {r['assignee'] or '-'} | 생성: {r['created_at'] or '-'}")
        list_md = "\n".join(_link(r) for r in rows)

        answer = f"{intro}\n\n{list_md}"
        if total > len(rows):
            answer += f"\n\n_전체 {total}건 중 최신 {len(rows)}건만 표시했습니다._"

        # ── LLM은 경향 요약만 (실패해도 목록 답변은 유지) ──
        try:
            llm, model = resolve_llm("summarize")
            trend = llm.chat(
                [{"role": "system", "content": _TREND_PROMPT},
                 {"role": "user", "content": f"질문: {question}\n이슈 목록:\n" + "\n".join(
                     f"[{r['key']}] {r['title']} | {r['status'] or '-'} | {r['assignee'] or '-'} | {r['created_at'] or '-'}"
                     for r in rows)}],
                model=model,
                temperature=0.1,
                max_tokens=200,
            ).strip()
            if trend and trend.upper() != "NONE":
                answer += f"\n\n{trend}"
        except Exception as e:
            logger.warning(f"[Graph] 경향 요약 실패 (목록만 반환): {e}")

        references = [{
            "title": f"[{r['key']}] {r['title']}",
            "url": r["url"],
            "source": "jira",
            "issue_key": r["key"],
            "project_key": r["key"].split("-")[0],
            "content_type": "issue",
        } for r in rows[:_MAX_REF_ROWS] if r.get("url")]

        info = {
            "graph_total": result["total"],
            "graph_filter": filters,
            "model": current_model_name("summarize"),
        }
        return answer, references, info
    except Exception as e:
        logger.error(f"[Graph] aggregate 경로 실패, 기존 파이프라인 폴백: {e}", exc_info=True)
        return None

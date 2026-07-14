"""
토픽 마이너 (#58 Phase 1) — 질문 로그를 LLM 클러스터링해 위키 토픽 후보 도출

관리자 위키 탭의 [토픽 후보 분석] 버튼에서 온디맨드 실행 (결과 미저장).
"""

import json
from typing import Dict, List

from company_llm_rag.history_store import _conn
from company_llm_rag.llm.factory import resolve_llm
from company_llm_rag.wiki.wiki_store import list_pages
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_MAX_QUESTIONS = 200   # LLM에 넘길 질문 상한
_RECENT_DAYS = 90

_PROMPT = """당신은 사내 지식베이스 큐레이터입니다. 아래 사용자 질문 목록을 주제별로 클러스터링하세요.

규칙:
- 비슷한 질문끼리 묶어 주제(topic)를 만들고, 질문이 2개 이상인 주제만 포함하세요.
- slug: 영문 소문자-하이픈 (예: point-accrual)
- title: 한국어 페이지 제목 (예: "포인트 적립 정책·절차")
- questions: 해당 클러스터의 대표 질문 3~8개 (원문 그대로)
- count: 클러스터에 속한 질문 수
- 클러스터 크기(count) 내림차순으로 정렬하세요. 최대 15개 주제.

반드시 JSON 배열만 출력:
[{"slug": "...", "title": "...", "count": n, "questions": ["...", "..."]}]

[질문 목록]
{questions}"""


def load_recent_questions(days: int = _RECENT_DAYS) -> List[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT question FROM chat_history "
            f"WHERE created_at >= datetime('now', '-{int(days)} days') "
            "AND length(question) > 8 ORDER BY id DESC LIMIT 1000"
        ).fetchall()
    out = []
    for r in rows:
        q = r["question"].strip()
        if q.startswith("http") or "sess-" in q:
            continue
        out.append(q)
        if len(out) >= _MAX_QUESTIONS:
            break
    return out


def mine_topics() -> List[Dict]:
    """질문 로그 → 토픽 후보. 이미 위키가 있는 토픽은 has_page 표시."""
    questions = load_recent_questions()
    if len(questions) < 5:
        return []
    llm, model = resolve_llm("summarize")
    raw = llm.chat(
        [{"role": "user",
          "content": _PROMPT.replace("{questions}", "\n".join(f"- {q}" for q in questions))}],
        model=model, temperature=0.0, max_tokens=1500,
    )
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        logger.warning(f"[Wiki] 토픽 클러스터링 파싱 실패: {raw[:100]}")
        return []
    try:
        topics = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("[Wiki] 토픽 JSON 파싱 실패")
        return []

    existing = {p["topic"] for p in list_pages()}
    out = []
    for t in topics:
        slug = (t.get("slug") or "").strip()
        if not slug or not t.get("questions"):
            continue
        out.append({
            "slug": slug,
            "title": (t.get("title") or slug).strip(),
            "count": int(t.get("count") or len(t["questions"])),
            "questions": [q.strip() for q in t["questions"] if q.strip()][:8],
            "has_page": slug in existing,
        })
    logger.info(f"[Wiki] 토픽 후보 {len(out)}개 (질문 {len(questions)}건 기반)")
    return out

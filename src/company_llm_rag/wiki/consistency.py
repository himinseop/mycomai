"""
위키 팩트 모순 검증 (#58 Phase 2)

페이지 생성 시 함께 추출된 구조화 팩트를 페이지 간 교차 검사합니다.
같은 항목(key)에 서로 다른 값이 있으면 상충으로 보고 — 담당자 확인 유도.
(온톨로지 커리큘럼 분석에서 채택한 '실용적 추론' 요소 — 풀 Datalog 없이 결정적 검사)
"""

import re
from typing import Dict, List

from company_llm_rag.wiki import wiki_store


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def find_conflicts() -> List[Dict]:
    """페이지 간(및 페이지 내) 동일 key·상이 value 팩트 상충 목록."""
    pages = [p for p in wiki_store.list_pages()
             if p["status"] != wiki_store.STATUS_DISABLED]
    by_key: Dict[str, List[Dict]] = {}
    for p in pages:
        for f in p.get("facts", []):
            key = _norm(f.get("key", ""))
            if not key:
                continue
            by_key.setdefault(key, []).append({
                "page_id": p["id"], "topic": p["topic"], "page_title": p["title"],
                "key": f.get("key", ""), "value": f.get("value", ""),
                "source": f.get("source", ""),
            })
    conflicts = []
    for key, entries in by_key.items():
        values = {_norm(e["value"]) for e in entries}
        if len(values) > 1:
            conflicts.append({"key": entries[0]["key"], "entries": entries})
    return conflicts

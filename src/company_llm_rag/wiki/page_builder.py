"""
위키 페이지 빌더 (#58 Phase 1)

토픽(대표 질문들) → 하이브리드 검색으로 소스 문서 수집 → LLM 합성
(출처 인용 강제 + 구조화 팩트 블록) → wiki_store 저장(draft).

팩트 블록은 온톨로지/그래프(#59)의 시드로 함께 추출됩니다.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from company_llm_rag.llm.factory import current_model_name, resolve_llm
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.wiki import wiki_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_MAX_SOURCE_DOCS = 25          # 합성에 쓸 소스 문서 상한
_DOC_CHARS = 1200              # 문서당 본문 길이 상한
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "wiki" / "page.txt"

_PAGE_SPLIT_RE = re.compile(r"===FACTS===", re.IGNORECASE)


def collect_sources(questions: List[str]) -> List[Dict]:
    """대표 질문들로 검색해 소스 문서를 수집·중복 제거합니다 (위키 문서는 제외)."""
    seen, docs = set(), []
    for q in questions[:5]:
        for d in retrieve_documents(q, n_results=8):
            meta = d.get("metadata", {})
            if meta.get("is_wiki"):
                continue  # 위키로 위키를 만들지 않음 (순환 방지)
            key = meta.get("original_doc_id") or meta.get("url") or meta.get("title")
            if not key or key in seen:
                continue
            if not (d.get("content") or "").strip():
                continue
            seen.add(key)
            docs.append(d)
            if len(docs) >= _MAX_SOURCE_DOCS:
                return docs
    return docs


def _format_sources(docs: List[Dict]) -> str:
    # 출처 인용이 "문서N"이 아닌 실제 제목으로 남도록, 제목을 문서 식별자로 제시
    lines = []
    for d in docs:
        m = d.get("metadata", {})
        title = (m.get("title") or "(제목없음)").strip()
        lines.append(
            f"### {title}\n(소스: {m.get('source', '')}, 작성일: {(m.get('created_at') or '')[:10]})\n"
            f"{(d.get('content') or '')[:_DOC_CHARS]}"
        )
    return "\n\n".join(lines)


def _parse_output(raw: str) -> Tuple[str, List[Dict]]:
    """LLM 출력 → (마크다운 본문, 팩트 리스트). ===FACTS=== 구분자 방식."""
    parts = _PAGE_SPLIT_RE.split(raw, maxsplit=1)
    content = parts[0].replace("===PAGE===", "").strip()
    facts: List[Dict] = []
    if len(parts) > 1:
        s, e = parts[1].find("["), parts[1].rfind("]")
        if s >= 0 and e > s:
            try:
                facts = [f for f in json.loads(parts[1][s:e + 1]) if isinstance(f, dict)]
            except json.JSONDecodeError:
                logger.warning("[Wiki] 팩트 블록 파싱 실패 — 본문만 저장")
    return content, facts


def build_page(topic: str, title: str, questions: List[str]) -> Dict:
    """페이지를 합성해 draft로 저장합니다.

    Raises:
        ValueError: 소스 문서 부족 / 합성 결과 검증 실패
    """
    questions = [q.strip() for q in questions if q.strip()]
    if not questions:
        raise ValueError("대표 질문이 필요합니다")

    docs = collect_sources(questions)
    if len(docs) < 3:
        raise ValueError(f"소스 문서 부족 ({len(docs)}건) — 토픽이 지식베이스에 충분히 없습니다")

    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        f"[토픽] {title}\n"
        f"[사용자들이 실제로 묻는 질문]\n" + "\n".join(f"- {q}" for q in questions) +
        f"\n\n[원본 문서 {len(docs)}건]\n{_format_sources(docs)}"
    )
    llm, model = resolve_llm("chat")
    raw = llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model, temperature=0.2, max_tokens=2500,
    )
    content, facts = _parse_output(raw)

    # 합성 검증: 최소 길이 + 출처 인용 존재
    if len(content) < 200:
        raise ValueError("합성 결과가 너무 짧습니다")
    if "[출처:" not in content:
        raise ValueError("출처 인용이 없는 페이지 — 저장 거부 (프롬프트 규칙 위반)")

    source_ids = []
    hashes = []
    for d in docs:
        m = d.get("metadata", {})
        if m.get("original_doc_id"):
            source_ids.append(m["original_doc_id"])
        if m.get("content_hash"):
            hashes.append(m["content_hash"])

    page = wiki_store.upsert_page(
        topic=topic, title=title, content=content,
        questions=questions, facts=facts,
        source_doc_ids=source_ids,
        source_hash=wiki_store.compute_source_hash(hashes),
        model=current_model_name("chat"),
    )
    logger.info(f"[Wiki] 페이지 합성 완료: {topic} (소스 {len(docs)}건, 팩트 {len(facts)}개)")
    return page

"""RAG 평가 러너 핵심 로직 (#63 1단계).

이 모듈은 순수 함수만 담습니다 — ChromaDB/OpenAI 등 네트워크 호출이 전혀 없어
`tests/test_eval_runner.py`가 가짜(`judge_fn`)만으로 전부 검증할 수 있습니다.
실제 `rag_query`/judge LLM을 연결하는 부분은 `scripts/eval_rag.py`(얇은 CLI)에 있습니다.

설계: docs/issues/63/design.md §2.1
"""

import json
from statistics import mean
from typing import Callable, Dict, List, Optional

# rag_system._NO_ANSWER_PHRASE와 동일한 문구. 순수 함수 모듈이 rag_system을 import하지
# 않도록(무거운 의존성 회피) 문자열을 복제합니다 — scripts/eval_rag.py는 실제 상수를
# 명시적으로 넘겨 두 값이 어긋나지 않게 합니다.
NO_ANSWER_PHRASE = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."

_REQUIRED_FIELDS = ("id", "question", "category")
_VALID_CATEGORIES = {"manual", "hub", "live", "abstain"}
_OPTIONAL_DEFAULTS = {
    "expected_facts": list,
    "expected_sources": list,
    "must_abstain": lambda: False,
    "source_excerpt": lambda: "",
    "notes": lambda: "",
    "reviewed": lambda: False,
}


# ── 골든셋 로드 ──────────────────────────────────────────────────────────

def load_golden(path: str) -> List[Dict]:
    """골든셋 JSONL을 읽고 스키마를 검증합니다.

    필수 필드(id/question/category)와 category 값, 타입(expected_facts/
    expected_sources는 리스트, must_abstain은 bool), id 유일성을 검사합니다.
    선택 필드는 기본값을 채워 반환합니다. 문제가 있으면 전부 모아 한 번에
    ValueError로 보고합니다.
    """
    cases: List[Dict] = []
    errors: List[str] = []
    seen_ids: Dict[str, int] = {}

    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"L{lineno}: JSON 파싱 실패 — {e}")
                continue
            if not isinstance(case, dict):
                errors.append(f"L{lineno}: 객체(dict)가 아님")
                continue

            for field in _REQUIRED_FIELDS:
                if not case.get(field):
                    errors.append(f"L{lineno} (id={case.get('id')!r}): 필수 필드 누락 — {field}")

            cat = case.get("category")
            if cat is not None and cat not in _VALID_CATEGORIES:
                errors.append(
                    f"L{lineno} (id={case.get('id')!r}): category 값이 유효하지 않음 "
                    f"({cat!r}, 허용값={sorted(_VALID_CATEGORIES)})"
                )

            for field, default_factory in _OPTIONAL_DEFAULTS.items():
                if field not in case:
                    case[field] = default_factory()

            if not isinstance(case["expected_facts"], list):
                errors.append(f"L{lineno} (id={case.get('id')!r}): expected_facts는 리스트여야 함")
            if not isinstance(case["expected_sources"], list):
                errors.append(f"L{lineno} (id={case.get('id')!r}): expected_sources는 리스트여야 함")
            if not isinstance(case["must_abstain"], bool):
                errors.append(f"L{lineno} (id={case.get('id')!r}): must_abstain은 bool이어야 함")

            cid = case.get("id")
            if cid is not None:
                if cid in seen_ids:
                    errors.append(f"L{lineno}: id 중복 — {cid!r} (앞서 L{seen_ids[cid]}에서도 사용)")
                else:
                    seen_ids[cid] = lineno

            cases.append(case)

    if errors:
        raise ValueError(f"golden set 검증 실패 ({len(errors)}건):\n" + "\n".join(errors))

    return cases


# ── 채점 ─────────────────────────────────────────────────────────────────

def build_judge_context(docs: List[Dict], max_docs: int = 8, max_chars_per_doc: int = 600) -> str:
    """검색 컨텍스트 문서를 judge 프롬프트용 [1] [2] ... 번호 매김 텍스트로 변환합니다."""
    parts = []
    for i, doc in enumerate(docs[:max_docs], start=1):
        meta = doc.get("metadata") or {}
        title = meta.get("title") or meta.get("original_doc_id") or ""
        content = (doc.get("content") or "").strip()[:max_chars_per_doc]
        parts.append(f"[{i}] {title}\n{content}")
    return "\n\n".join(parts)


def parse_judge_response(raw: str) -> Dict:
    """judge LLM 원문 응답을 JSON으로 파싱합니다. 실패 시 안전한 기본값으로 폴백합니다."""
    fallback = {"claims": [], "facts": [], "notes": "", "_parse_error": True, "_raw": raw}
    if not raw or not isinstance(raw, str):
        return fallback
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return fallback
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return fallback
    if not isinstance(parsed, dict):
        return fallback

    claims = parsed.get("claims")
    facts = parsed.get("facts")
    if not isinstance(claims, list):
        claims = []
    if not isinstance(facts, list):
        facts = []
    return {
        "claims": claims,
        "facts": facts,
        "notes": parsed.get("notes", ""),
        "_parse_error": False,
    }


def compute_faithfulness(claims: List[Dict]) -> float:
    """지지된 claim 비율. claim이 하나도 없으면(사실 주장 없는 답변) 1.0."""
    if not claims:
        return 1.0
    supported = sum(1 for c in claims if c.get("supported"))
    return supported / len(claims)


def compute_correctness(facts: List[Dict]) -> Optional[float]:
    """expected_facts 중 답변에 포함된 비율. facts가 비어 있으면(정답 사실 없음) None(N/A)."""
    if not facts:
        return None
    present = sum(1 for f in facts if f.get("present"))
    return present / len(facts)


def doc_id_matches(expected: str, candidate: str) -> bool:
    """candidate(원본 doc_id)가 expected로 시작하면 매칭(접두어 매칭 — 청크 id 아님)."""
    return bool(candidate) and bool(expected) and candidate.startswith(expected)


def compute_source_hit(
    expected_sources: List[str], refs: List[Dict], docs: List[Dict]
) -> Optional[bool]:
    """expected_sources 중 하나라도 참고문서(refs)나 검색 컨텍스트(docs)에 있으면 True.

    expected_sources가 비어 있으면 해당 없음(None) — live/hub 카테고리처럼 출처를
    지정하지 않는 케이스에 쓰입니다.
    """
    if not expected_sources:
        return None
    candidates = set()
    for r in refs or []:
        doc_id = (r or {}).get("doc_id") if isinstance(r, dict) else None
        if doc_id:
            candidates.add(doc_id)
    for d in docs or []:
        meta = (d or {}).get("metadata") or {}
        doc_id = meta.get("original_doc_id")
        if doc_id:
            candidates.add(doc_id)
    return any(doc_id_matches(exp, cand) for exp in expected_sources for cand in candidates)


def score_case(
    case: Dict,
    answer: str,
    refs: List[Dict],
    docs: List[Dict],
    judge_fn: Callable[[str, str, str, List[str]], str],
    no_answer_phrase: str = NO_ANSWER_PHRASE,
    timing: Optional[Dict] = None,
) -> Dict:
    """케이스 하나를 채점합니다.

    judge_fn(question, answer, context_text, expected_facts) -> judge LLM 원문 응답(str).
    """
    answer = answer or ""
    is_no_answer = no_answer_phrase in answer
    must_abstain = bool(case.get("must_abstain"))
    abstain_correct = is_no_answer if must_abstain else (not is_no_answer)

    source_hit = compute_source_hit(case.get("expected_sources") or [], refs, docs)

    context_text = build_judge_context(docs)
    judge_raw = judge_fn(case["question"], answer, context_text, case.get("expected_facts") or [])
    judge_parsed = parse_judge_response(judge_raw)

    faithfulness = compute_faithfulness(judge_parsed["claims"])
    correctness = compute_correctness(judge_parsed["facts"])
    hallucinated = sum(1 for c in judge_parsed["claims"] if not c.get("supported"))

    return {
        "id": case["id"],
        "category": case.get("category"),
        "question": case["question"],
        "answer": answer,
        "must_abstain": must_abstain,
        "is_no_answer": is_no_answer,
        "abstain_correct": abstain_correct,
        "source_hit": source_hit,
        "faithfulness": faithfulness,
        "correctness": correctness,
        "claim_count": len(judge_parsed["claims"]),
        "hallucinated_claims": hallucinated,
        "judge_parse_error": judge_parsed["_parse_error"],
        "judge": {
            "claims": judge_parsed["claims"],
            "facts": judge_parsed["facts"],
            "notes": judge_parsed["notes"],
        },
        # 재채점(--rejudge) 시 judge_fn에 그대로 다시 넘기는 컨텍스트 — docs를 다시
        # 불러오지 않아도 동일한 조건으로 재채점할 수 있도록 원문 그대로 보존합니다.
        "judge_context_text": context_text,
        "refs": refs,
        "context_docs": [
            {
                "title": (d.get("metadata") or {}).get("title", ""),
                "original_doc_id": (d.get("metadata") or {}).get("original_doc_id", ""),
                "source": (d.get("metadata") or {}).get("source", ""),
                "content_excerpt": (d.get("content") or "")[:300],
            }
            for d in (docs or [])
        ],
        "timing": timing or {},
    }


def rejudge_case(
    saved_result: Dict,
    case: Dict,
    judge_fn: Callable[[str, str, str, List[str]], str],
) -> Dict:
    """저장된 결과(answer/refs/judge_context_text 보존)를 rag_query 재호출 없이 재채점합니다.

    검색은 다시 하지 않고 answer와 그 당시의 judge_context_text를 그대로 재사용 —
    judge 프롬프트·모델을 바꿔 같은 답변에 대한 채점만 다시 하고 싶을 때 사용합니다.
    """
    answer = saved_result.get("answer", "")
    context_text = saved_result.get("judge_context_text", "")
    no_answer_phrase = NO_ANSWER_PHRASE
    is_no_answer = no_answer_phrase in (answer or "")
    must_abstain = bool(case.get("must_abstain"))
    abstain_correct = is_no_answer if must_abstain else (not is_no_answer)

    refs = saved_result.get("refs", [])
    docs = [
        {"metadata": {"original_doc_id": d.get("original_doc_id", ""),
                       "title": d.get("title", ""), "source": d.get("source", "")},
         "content": d.get("content_excerpt", "")}
        for d in saved_result.get("context_docs", [])
    ]
    source_hit = compute_source_hit(case.get("expected_sources") or [], refs, docs)

    judge_raw = judge_fn(case["question"], answer, context_text, case.get("expected_facts") or [])
    judge_parsed = parse_judge_response(judge_raw)
    faithfulness = compute_faithfulness(judge_parsed["claims"])
    correctness = compute_correctness(judge_parsed["facts"])
    hallucinated = sum(1 for c in judge_parsed["claims"] if not c.get("supported"))

    result = dict(saved_result)
    result.update({
        "abstain_correct": abstain_correct,
        "source_hit": source_hit,
        "faithfulness": faithfulness,
        "correctness": correctness,
        "claim_count": len(judge_parsed["claims"]),
        "hallucinated_claims": hallucinated,
        "judge_parse_error": judge_parsed["_parse_error"],
        "judge": {
            "claims": judge_parsed["claims"],
            "facts": judge_parsed["facts"],
            "notes": judge_parsed["notes"],
        },
    })
    return result


def build_query_error_result(
    case: Dict, error_message: str, timing: Optional[Dict] = None
) -> Dict:
    """rag_query 자체가 실패한 케이스(예: OpenAI 429)의 결과 레코드.

    judge를 호출조차 못 했으므로 faithfulness/correctness/abstain_correct/source_hit을
    전부 None으로 남겨 집계에서 제외합니다 — 0점으로 채점하면 실제 모델 품질 저하와
    구분이 안 됩니다. judge 출력 자체의 문제(`judge_parse_error`)와는 별개 원인이므로
    `query_error`(예외 메시지)에 담아 리포트에서 분리 집계합니다.
    """
    return {
        "id": case["id"], "category": case.get("category"), "question": case["question"],
        "answer": "", "must_abstain": bool(case.get("must_abstain")),
        "is_no_answer": False, "abstain_correct": None, "source_hit": None,
        "faithfulness": None, "correctness": None, "claim_count": 0,
        "hallucinated_claims": 0, "judge_parse_error": False, "query_error": error_message,
        "judge": {"claims": [], "facts": [], "notes": ""},
        "judge_context_text": "", "refs": [], "context_docs": [], "timing": timing or {},
    }


# ── 집계 ─────────────────────────────────────────────────────────────────

def _mean_or_none(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return mean(vals) if vals else None


def _bool_rate_or_none(values: List[Optional[bool]]) -> Optional[float]:
    """None(해당 없음/집계 제외)은 걸러내고 나머지를 1.0/0.0으로 환산한 평균."""
    vals = [v for v in values if v is not None]
    return mean([1.0 if v else 0.0 for v in vals]) if vals else None


def summarize(results: List[Dict]) -> Dict:
    """전체 및 카테고리별 집계 지표. query_error 케이스는 모든 평균에서 제외됩니다."""
    n = len(results)
    by_category: Dict[str, Dict] = {}
    for cat in sorted({r.get("category", "") for r in results}):
        cat_results = [r for r in results if r.get("category") == cat]
        by_category[cat] = {
            "n": len(cat_results),
            "mean_faithfulness": _mean_or_none([r["faithfulness"] for r in cat_results]),
            "mean_correctness": _mean_or_none([r["correctness"] for r in cat_results]),
            "abstain_accuracy": _bool_rate_or_none([r["abstain_correct"] for r in cat_results]),
            "source_hit_rate": _bool_rate_or_none([r["source_hit"] for r in cat_results]),
        }

    total_ms_values = [
        r["timing"]["total_ms"] for r in results
        if isinstance(r.get("timing"), dict) and r["timing"].get("total_ms") is not None
    ]

    return {
        "n": n,
        "mean_faithfulness": _mean_or_none([r["faithfulness"] for r in results]),
        "mean_correctness": _mean_or_none([r["correctness"] for r in results]),
        "abstain_accuracy": _bool_rate_or_none([r["abstain_correct"] for r in results]),
        "source_hit_rate": _bool_rate_or_none([r["source_hit"] for r in results]),
        "source_hit_applicable_n": sum(1 for r in results if r["source_hit"] is not None),
        "hallucinated_claims_total": sum(r["hallucinated_claims"] for r in results),
        "claim_count_total": sum(r["claim_count"] for r in results),
        "judge_parse_error_count": sum(1 for r in results if r.get("judge_parse_error")),
        "query_error_count": sum(1 for r in results if r.get("query_error")),
        "mean_total_ms": _mean_or_none(total_ms_values),
        "by_category": by_category,
    }


# ── 베이스라인 비교 ──────────────────────────────────────────────────────

_FAITHFULNESS_REGRESSION_THRESHOLD = 0.2


def compare(results: List[Dict], baseline: List[Dict]) -> Dict:
    """현재 결과와 베이스라인을 id 기준으로 비교하고 회귀 케이스를 표시합니다.

    회귀 기준: faithfulness가 0.2 초과 하락, abstain 정오답이 맞음→틀림으로 뒤집힘,
    또는 source_hit이 True→False/None(상실)로 바뀜.
    """
    base_by_id = {b["id"]: b for b in baseline}
    cur_by_id = {r["id"]: r for r in results}

    per_case = []
    for cid, cur in cur_by_id.items():
        base = base_by_id.get(cid)
        if base is None:
            per_case.append({"id": cid, "category": cur.get("category"), "status": "new"})
            continue

        # query_error 케이스는 faithfulness/correctness/abstain_correct/source_hit이
        # 전부 None(집계 제외)이므로 회귀 판정 대상이 아니다 — 데이터가 없을 뿐, 실제
        # 품질 저하와 구분해야 한다.
        query_error_now = bool(cur.get("query_error"))

        f_delta = None
        faithfulness_regressed = False
        if cur.get("faithfulness") is not None and base.get("faithfulness") is not None:
            f_delta = cur["faithfulness"] - base["faithfulness"]
            faithfulness_regressed = f_delta < -_FAITHFULNESS_REGRESSION_THRESHOLD

        c_delta = None
        if cur.get("correctness") is not None and base.get("correctness") is not None:
            c_delta = cur["correctness"] - base["correctness"]

        abstain_flip = (
            cur.get("abstain_correct") is not None
            and bool(base.get("abstain_correct")) and not bool(cur.get("abstain_correct"))
        )
        source_hit_lost = (
            cur.get("source_hit") is not None
            and bool(base.get("source_hit")) and not bool(cur.get("source_hit"))
        )

        reasons = []
        if faithfulness_regressed:
            reasons.append(f"faithfulness -{abs(f_delta):.2f}")
        if abstain_flip:
            reasons.append("abstain 정답→오답")
        if source_hit_lost:
            reasons.append("source_hit 상실")

        per_case.append({
            "id": cid,
            "category": cur.get("category"),
            "status": "query_error" if query_error_now else "compared",
            "faithfulness_delta": f_delta,
            "correctness_delta": c_delta,
            "abstain_flip": abstain_flip,
            "source_hit_lost": source_hit_lost,
            "regression": bool(reasons),
            "reasons": reasons,
        })

    missing_in_current = sorted(set(base_by_id) - set(cur_by_id))
    for cid in missing_in_current:
        per_case.append({"id": cid, "category": base_by_id[cid].get("category"), "status": "missing"})

    regressions = [c for c in per_case if c.get("regression")]

    return {
        "per_case": per_case,
        "regression_count": len(regressions),
        "regressions": regressions,
        "new_case_ids": [c["id"] for c in per_case if c["status"] == "new"],
        "missing_case_ids": missing_in_current,
    }

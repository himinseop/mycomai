"""RAG 평가 러너(company_llm_rag.eval.runner) 테스트 (#63 1단계)

전부 가짜(fake judge_fn)로 검증합니다 — 네트워크 호출 없음.
golden.jsonl 스키마 테스트만 실제 커밋된 파일을 읽습니다.
"""

import json
import os

import pytest

from company_llm_rag.eval import runner

_GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "eval", "golden.jsonl")


def _write_jsonl(tmp_path, name, lines):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return str(path)


# ── judge 응답 파싱 ──────────────────────────────────────────────────────

class TestParseJudgeResponse:
    def test_valid_json(self):
        raw = '{"claims": [{"text": "a", "supported": true, "ref": 1}], "facts": [], "notes": "ok"}'
        parsed = runner.parse_judge_response(raw)
        assert parsed["_parse_error"] is False
        assert parsed["claims"] == [{"text": "a", "supported": True, "ref": 1}]
        assert parsed["facts"] == []

    def test_json_with_surrounding_prose(self):
        raw = '설명입니다.\n{"claims": [], "facts": [{"fact": "x", "present": true}], "notes": ""}\n끝.'
        parsed = runner.parse_judge_response(raw)
        assert parsed["_parse_error"] is False
        assert parsed["facts"] == [{"fact": "x", "present": True}]

    def test_malformed_json_falls_back(self):
        raw = '{"claims": [oops this is not json'
        parsed = runner.parse_judge_response(raw)
        assert parsed["_parse_error"] is True
        assert parsed["claims"] == []
        assert parsed["facts"] == []

    def test_empty_string_falls_back(self):
        parsed = runner.parse_judge_response("")
        assert parsed["_parse_error"] is True

    def test_none_falls_back(self):
        parsed = runner.parse_judge_response(None)
        assert parsed["_parse_error"] is True

    def test_non_dict_json_falls_back(self):
        parsed = runner.parse_judge_response('[1, 2, 3]')
        assert parsed["_parse_error"] is True

    def test_missing_keys_default_to_empty_lists(self):
        raw = '{"notes": "판정 불가"}'
        parsed = runner.parse_judge_response(raw)
        assert parsed["_parse_error"] is False
        assert parsed["claims"] == []
        assert parsed["facts"] == []


# ── faithfulness / correctness 계산 ──────────────────────────────────────

class TestFaithfulness:
    def test_no_claims_is_perfect(self):
        assert runner.compute_faithfulness([]) == 1.0

    def test_all_supported(self):
        claims = [{"supported": True}, {"supported": True}]
        assert runner.compute_faithfulness(claims) == 1.0

    def test_partial_support(self):
        claims = [{"supported": True}, {"supported": False}, {"supported": True}, {"supported": False}]
        assert runner.compute_faithfulness(claims) == 0.5

    def test_none_supported(self):
        claims = [{"supported": False}]
        assert runner.compute_faithfulness(claims) == 0.0


class TestCorrectness:
    def test_no_facts_is_none(self):
        assert runner.compute_correctness([]) is None

    def test_all_present(self):
        facts = [{"present": True}, {"present": True}]
        assert runner.compute_correctness(facts) == 1.0

    def test_partial_present(self):
        facts = [{"present": True}, {"present": False}, {"present": True}]
        assert runner.compute_correctness(facts) == pytest.approx(2 / 3)

    def test_none_present(self):
        facts = [{"present": False}, {"present": False}]
        assert runner.compute_correctness(facts) == 0.0


# ── abstain 로직 (양방향) ────────────────────────────────────────────────

class TestAbstainLogic:
    @staticmethod
    def _judge_fn(question, answer, context, expected_facts):
        return '{"claims": [], "facts": [], "notes": ""}'

    def test_must_abstain_and_did_abstain_is_correct(self):
        case = {"id": "c1", "question": "q", "category": "abstain", "must_abstain": True}
        result = runner.score_case(case, runner.NO_ANSWER_PHRASE, [], [], self._judge_fn)
        assert result["is_no_answer"] is True
        assert result["abstain_correct"] is True

    def test_must_abstain_but_answered_is_incorrect(self):
        case = {"id": "c2", "question": "q", "category": "abstain", "must_abstain": True}
        result = runner.score_case(case, "확실한 답변입니다.", [], [], self._judge_fn)
        assert result["is_no_answer"] is False
        assert result["abstain_correct"] is False

    def test_must_answer_and_did_answer_is_correct(self):
        case = {"id": "c3", "question": "q", "category": "manual", "must_abstain": False}
        result = runner.score_case(case, "정상 답변입니다.", [], [], self._judge_fn)
        assert result["abstain_correct"] is True

    def test_must_answer_but_abstained_is_incorrect(self):
        case = {"id": "c4", "question": "q", "category": "manual", "must_abstain": False}
        result = runner.score_case(case, runner.NO_ANSWER_PHRASE, [], [], self._judge_fn)
        assert result["abstain_correct"] is False

    def test_custom_no_answer_phrase(self):
        case = {"id": "c5", "question": "q", "category": "abstain", "must_abstain": True}
        result = runner.score_case(
            case, "커스텀 문구입니다.", [], [], self._judge_fn, no_answer_phrase="커스텀 문구"
        )
        assert result["abstain_correct"] is True


# ── source_hit prefix matching ───────────────────────────────────────────

class TestSourceHit:
    def test_no_expected_sources_is_none(self):
        assert runner.compute_source_hit([], [{"doc_id": "docs-x"}], []) is None

    def test_hit_via_refs(self):
        assert runner.compute_source_hit(
            ["docs-platform/features/ecoupon.md"],
            [{"doc_id": "docs-platform/features/ecoupon.md"}],
            [],
        ) is True

    def test_hit_via_context_docs(self):
        docs = [{"metadata": {"original_doc_id": "docs-platform/features/ecoupon.md"}}]
        assert runner.compute_source_hit(["docs-platform/features/ecoupon.md"], [], docs) is True

    def test_prefix_match(self):
        """expected가 candidate의 접두어이면 매칭(청크 id 등 접미어 차이 허용)."""
        docs = [{"metadata": {"original_doc_id": "docs-platform/features/ecoupon.md#chunk3"}}]
        assert runner.compute_source_hit(["docs-platform/features/ecoupon.md"], [], docs) is True

    def test_no_match_is_false(self):
        docs = [{"metadata": {"original_doc_id": "docs-platform/features/other.md"}}]
        assert runner.compute_source_hit(["docs-platform/features/ecoupon.md"], [], docs) is False

    def test_reverse_prefix_does_not_match(self):
        """candidate가 expected보다 짧으면(접두어 관계 반대) 매칭하지 않는다."""
        docs = [{"metadata": {"original_doc_id": "docs-platform/features/e"}}]
        assert runner.compute_source_hit(["docs-platform/features/ecoupon.md"], [], docs) is False

    def test_empty_doc_id_ignored(self):
        docs = [{"metadata": {"original_doc_id": ""}}]
        assert runner.compute_source_hit(["docs-x"], [{"doc_id": ""}], docs) is False


# ── score_case 통합 ───────────────────────────────────────────────────────

class TestScoreCase:
    def test_full_case_with_supported_claims_and_facts(self):
        case = {
            "id": "man-001", "question": "배달료는 누가 부담해?", "category": "manual",
            "expected_facts": ["배달료는 업주 부담"], "expected_sources": ["docs-platform/features/ecoupon.md"],
            "must_abstain": False,
        }
        refs = [{"doc_id": "docs-platform/features/ecoupon.md"}]
        docs = [{"metadata": {"original_doc_id": "docs-platform/features/ecoupon.md", "title": "E쿠폰"}, "content": "배달료는 업주가 부담합니다."}]

        def judge_fn(question, answer, context, expected_facts):
            return json.dumps({
                "claims": [{"text": "배달료는 업주 부담입니다.", "supported": True, "ref": 1}],
                "facts": [{"fact": "배달료는 업주 부담", "present": True}],
                "notes": "",
            })

        result = runner.score_case(case, "배달료는 업주 부담입니다.", refs, docs, judge_fn)
        assert result["faithfulness"] == 1.0
        assert result["correctness"] == 1.0
        assert result["source_hit"] is True
        assert result["hallucinated_claims"] == 0
        assert result["claim_count"] == 1
        assert result["judge_context_text"]  # 컨텍스트가 재채점용으로 보존됨

    def test_hallucinated_claim_counted(self):
        case = {"id": "man-002", "question": "q", "category": "manual"}

        def judge_fn(question, answer, context, expected_facts):
            return json.dumps({
                "claims": [
                    {"text": "맞는 말", "supported": True, "ref": 1},
                    {"text": "지어낸 말", "supported": False, "ref": None},
                ],
                "facts": [], "notes": "",
            })

        result = runner.score_case(case, "답변", [], [], judge_fn)
        assert result["faithfulness"] == 0.5
        assert result["hallucinated_claims"] == 1
        assert result["claim_count"] == 2

    def test_judge_parse_error_propagates(self):
        case = {"id": "man-003", "question": "q", "category": "manual"}
        result = runner.score_case(case, "답변", [], [], lambda *a: "완전히 깨진 응답")
        assert result["judge_parse_error"] is True
        assert result["faithfulness"] == 1.0  # claims가 빈 배열이므로 fallback


# ── rejudge ────────────────────────────────────────────────────────────

class TestRejudge:
    def test_rejudge_reuses_saved_answer_and_context(self):
        case = {"id": "man-001", "question": "q", "category": "manual", "expected_facts": [], "expected_sources": [], "must_abstain": False}
        saved = {
            "id": "man-001", "answer": "저장된 답변", "refs": [], "context_docs": [],
            "judge_context_text": "[1] 제목\n내용", "timing": {"total_ms": 500},
        }
        seen = {}

        def judge_fn(question, answer, context, expected_facts):
            seen["answer"] = answer
            seen["context"] = context
            return '{"claims": [], "facts": [], "notes": ""}'

        result = runner.rejudge_case(saved, case, judge_fn)
        assert seen["answer"] == "저장된 답변"
        assert seen["context"] == "[1] 제목\n내용"
        assert result["timing"] == {"total_ms": 500}  # 검색 관련 필드는 그대로 보존


# ── load_golden 검증 ──────────────────────────────────────────────────────

class TestLoadGolden:
    def test_loads_valid_cases_with_defaults(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "a1", "question": "질문입니다", "category": "manual"}),
        ])
        cases = runner.load_golden(path)
        assert len(cases) == 1
        assert cases[0]["expected_facts"] == []
        assert cases[0]["expected_sources"] == []
        assert cases[0]["must_abstain"] is False
        assert cases[0]["reviewed"] is False

    def test_missing_required_field_raises(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "a1", "category": "manual"}),  # question 누락
        ])
        with pytest.raises(ValueError, match="question"):
            runner.load_golden(path)

    def test_invalid_category_raises(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "a1", "question": "q", "category": "invalid_cat"}),
        ])
        with pytest.raises(ValueError, match="category"):
            runner.load_golden(path)

    def test_duplicate_id_raises(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "dup", "question": "q1", "category": "manual"}),
            json.dumps({"id": "dup", "question": "q2", "category": "manual"}),
        ])
        with pytest.raises(ValueError, match="중복"):
            runner.load_golden(path)

    def test_wrong_type_for_expected_facts_raises(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "a1", "question": "q", "category": "manual", "expected_facts": "not a list"}),
        ])
        with pytest.raises(ValueError, match="expected_facts"):
            runner.load_golden(path)

    def test_wrong_type_for_must_abstain_raises(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            json.dumps({"id": "a1", "question": "q", "category": "manual", "must_abstain": "yes"}),
        ])
        with pytest.raises(ValueError, match="must_abstain"):
            runner.load_golden(path)

    def test_blank_lines_are_skipped(self, tmp_path):
        path = _write_jsonl(tmp_path, "g.jsonl", [
            "",
            json.dumps({"id": "a1", "question": "q", "category": "manual"}),
            "   ",
        ])
        cases = runner.load_golden(path)
        assert len(cases) == 1


# ── summarize ─────────────────────────────────────────────────────────────

class TestSummarize:
    def _result(self, **overrides):
        base = {
            "id": "x", "category": "manual", "faithfulness": 1.0, "correctness": None,
            "abstain_correct": True, "source_hit": None, "hallucinated_claims": 0,
            "claim_count": 0, "judge_parse_error": False, "timing": {"total_ms": 100},
        }
        base.update(overrides)
        return base

    def test_aggregates_across_results(self):
        results = [
            self._result(faithfulness=1.0, correctness=1.0, source_hit=True),
            self._result(faithfulness=0.5, correctness=0.0, source_hit=False, abstain_correct=False),
        ]
        summary = runner.summarize(results)
        assert summary["n"] == 2
        assert summary["mean_faithfulness"] == 0.75
        assert summary["mean_correctness"] == 0.5
        assert summary["abstain_accuracy"] == 0.5
        assert summary["source_hit_rate"] == 0.5
        assert summary["source_hit_applicable_n"] == 2

    def test_correctness_none_excluded_from_mean(self):
        results = [self._result(correctness=None), self._result(correctness=1.0)]
        summary = runner.summarize(results)
        assert summary["mean_correctness"] == 1.0

    def test_by_category_breakdown(self):
        results = [
            self._result(category="manual", faithfulness=1.0),
            self._result(category="abstain", faithfulness=0.0, abstain_correct=False),
        ]
        summary = runner.summarize(results)
        assert summary["by_category"]["manual"]["n"] == 1
        assert summary["by_category"]["abstain"]["n"] == 1
        assert summary["by_category"]["abstain"]["mean_faithfulness"] == 0.0

    def test_empty_results(self):
        summary = runner.summarize([])
        assert summary["n"] == 0
        assert summary["mean_faithfulness"] is None
        assert summary["source_hit_rate"] is None

    def test_query_error_excluded_from_all_means(self):
        """rag_query 실패 케이스는 faithfulness/correctness/abstain/source_hit 전부 집계 제외."""
        case = {"id": "e1", "question": "q", "category": "manual", "must_abstain": False}
        err_result = runner.build_query_error_result(case, "429 rate limit")
        results = [
            self._result(faithfulness=1.0, correctness=1.0, source_hit=True, abstain_correct=True),
            err_result,
        ]
        summary = runner.summarize(results)
        assert summary["n"] == 2
        assert summary["mean_faithfulness"] == 1.0  # query_error 케이스가 0으로 끌어내리지 않음
        assert summary["mean_correctness"] == 1.0
        assert summary["abstain_accuracy"] == 1.0
        assert summary["source_hit_rate"] == 1.0
        assert summary["query_error_count"] == 1
        assert summary["judge_parse_error_count"] == 0  # query_error는 judge_parse_error가 아님

    def test_query_error_excluded_from_category_breakdown(self):
        case = {"id": "e2", "question": "q", "category": "manual", "must_abstain": False}
        err_result = runner.build_query_error_result(case, "boom")
        results = [self._result(category="manual", faithfulness=0.8, abstain_correct=True), err_result]
        summary = runner.summarize(results)
        assert summary["by_category"]["manual"]["n"] == 2
        assert summary["by_category"]["manual"]["mean_faithfulness"] == 0.8
        assert summary["by_category"]["manual"]["abstain_accuracy"] == 1.0


class TestBuildQueryErrorResult:
    def test_fields_are_none_not_zero(self):
        case = {"id": "e1", "question": "q", "category": "manual", "must_abstain": True}
        r = runner.build_query_error_result(case, "Error code: 429 - rate limit")
        assert r["query_error"] == "Error code: 429 - rate limit"
        assert r["faithfulness"] is None
        assert r["correctness"] is None
        assert r["abstain_correct"] is None
        assert r["source_hit"] is None
        assert r["judge_parse_error"] is False
        assert r["answer"] == ""
        assert r["claim_count"] == 0
        assert r["hallucinated_claims"] == 0


# ── compare / regression flags ─────────────────────────────────────────────

class TestCompare:
    def _r(self, id, faithfulness=1.0, correctness=None, abstain_correct=True, source_hit=None, category="manual"):
        return {"id": id, "category": category, "faithfulness": faithfulness, "correctness": correctness,
                "abstain_correct": abstain_correct, "source_hit": source_hit}

    def test_no_regression_when_scores_stable(self):
        baseline = [self._r("a", faithfulness=0.9)]
        current = [self._r("a", faithfulness=0.85)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 0

    def test_faithfulness_drop_over_threshold_flagged(self):
        baseline = [self._r("a", faithfulness=0.9)]
        current = [self._r("a", faithfulness=0.6)]  # -0.3 > 0.2 임계치
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 1
        assert "faithfulness" in cmp["regressions"][0]["reasons"][0]

    def test_faithfulness_drop_under_threshold_not_flagged(self):
        baseline = [self._r("a", faithfulness=0.9)]
        current = [self._r("a", faithfulness=0.75)]  # -0.15 < 0.2 임계치
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 0

    def test_abstain_flip_flagged(self):
        baseline = [self._r("a", abstain_correct=True)]
        current = [self._r("a", abstain_correct=False)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 1
        assert cmp["regressions"][0]["abstain_flip"] is True

    def test_abstain_flip_wrong_direction_not_flagged(self):
        """이전엔 틀렸다가 이번에 맞은 경우는 회귀가 아니라 개선."""
        baseline = [self._r("a", abstain_correct=False)]
        current = [self._r("a", abstain_correct=True)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 0

    def test_source_hit_lost_flagged(self):
        baseline = [self._r("a", source_hit=True)]
        current = [self._r("a", source_hit=False)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 1
        assert cmp["regressions"][0]["source_hit_lost"] is True

    def test_source_hit_gained_not_flagged(self):
        baseline = [self._r("a", source_hit=False)]
        current = [self._r("a", source_hit=True)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 0

    def test_new_case_not_in_baseline(self):
        baseline = [self._r("a")]
        current = [self._r("a"), self._r("b")]
        cmp = runner.compare(current, baseline)
        assert cmp["new_case_ids"] == ["b"]

    def test_missing_case_from_baseline(self):
        baseline = [self._r("a"), self._r("b")]
        current = [self._r("a")]
        cmp = runner.compare(current, baseline)
        assert cmp["missing_case_ids"] == ["b"]

    def test_multiple_regression_reasons_combined(self):
        baseline = [self._r("a", faithfulness=0.9, abstain_correct=True, source_hit=True)]
        current = [self._r("a", faithfulness=0.5, abstain_correct=False, source_hit=False)]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 1
        assert len(cmp["regressions"][0]["reasons"]) == 3

    def test_query_error_current_not_flagged_as_regression(self):
        """이번 실행에서 rag_query가 실패한 케이스(데이터 없음)는 베이스라인이 아무리
        좋아도 회귀로 잘못 판정되면 안 된다 — 품질 저하가 아니라 API 실패일 뿐."""
        baseline = [self._r("a", faithfulness=0.9, abstain_correct=True, source_hit=True)]
        case = {"id": "a", "question": "q", "category": "manual", "must_abstain": False}
        current = [runner.build_query_error_result(case, "429 rate limit")]
        cmp = runner.compare(current, baseline)
        assert cmp["regression_count"] == 0
        assert cmp["per_case"][0]["status"] == "query_error"

    def test_query_error_in_baseline_only_no_crash(self):
        """베이스라인 쪽이 query_error였던 케이스도(faithfulness=None) 델타 계산에서
        안전하게 스킵돼야 한다(TypeError 없이)."""
        case = {"id": "a", "question": "q", "category": "manual", "must_abstain": False}
        baseline = [runner.build_query_error_result(case, "429 rate limit")]
        current = [self._r("a", faithfulness=0.8)]
        cmp = runner.compare(current, baseline)
        assert cmp["per_case"][0]["faithfulness_delta"] is None
        assert cmp["regression_count"] == 0


# ── golden.jsonl 스키마 테스트 (실제 커밋된 파일) ────────────────────────
#
# golden.jsonl은 scripts/build_golden_set.py(LLM 호출)로 생성한다. OpenAI 자격증명이
# 없거나 무효화된 환경(예: .env의 OPENAI_API_KEY 만료)에서는 파일이 아직 없을 수
# 있으므로, 그런 경우엔 실패 대신 명확한 사유로 skip한다 — 파일이 생기면 이 스킵은
# 자동으로 사라지고 아래 스키마 검증이 정상적으로 수행된다.
_GOLDEN_MISSING_REASON = (
    "tests/eval/golden.jsonl이 없습니다 — scripts/build_golden_set.py를 먼저 실행하세요 "
    "(OpenAI 자격증명 문제로 초안 생성이 막혀 있었다면 그것부터 해결)."
)


@pytest.mark.skipif(not os.path.isfile(_GOLDEN_PATH), reason=_GOLDEN_MISSING_REASON)
class TestGoldenFileSchema:
    def test_golden_file_loads_and_validates(self):
        cases = runner.load_golden(_GOLDEN_PATH)
        assert len(cases) >= 50, f"골든셋 케이스가 50건 미만입니다 (현재 {len(cases)}건)"

    def test_golden_file_has_all_categories(self):
        cases = runner.load_golden(_GOLDEN_PATH)
        categories = {c["category"] for c in cases}
        assert categories == {"manual", "hub", "live", "abstain"}

    def test_abstain_cases_have_must_abstain_true(self):
        cases = runner.load_golden(_GOLDEN_PATH)
        for c in cases:
            if c["category"] == "abstain":
                assert c["must_abstain"] is True, f"{c['id']}: abstain 케이스는 must_abstain=true여야 함"

    def test_manual_cases_have_expected_sources(self):
        cases = runner.load_golden(_GOLDEN_PATH)
        for c in cases:
            if c["category"] == "manual":
                assert c["expected_sources"], f"{c['id']}: manual 케이스는 expected_sources가 있어야 함"
                for src in c["expected_sources"]:
                    assert src.startswith("docs-"), f"{c['id']}: expected_sources는 docs- 접두어여야 함 ({src})"

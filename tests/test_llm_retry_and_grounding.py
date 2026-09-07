"""429 재시도(openai_provider), 오류 시 참고문서 제거·근거 라벨(rag_system) 단위 테스트."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openai  # noqa: E402
import httpx  # noqa: E402

from company_llm_rag.llm import openai_provider as op  # noqa: E402
from company_llm_rag import rag_system  # noqa: E402


def _rate_limit_error(msg="Rate limit reached ... Please try again in 1.5s. Visit x", retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    resp = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://api.openai.com/v1/chat"))
    return openai.RateLimitError(msg, response=resp, body=None)


class TestRetry:
    def test_wait_parses_try_again_in_message(self):
        e = _rate_limit_error("Please try again in 1.5s.")
        assert 1.9 <= op._retry_wait_seconds(e, 0) <= 2.1

    def test_wait_parses_ms(self):
        e = _rate_limit_error("Please try again in 800ms.")
        assert 1.0 <= op._retry_wait_seconds(e, 0) <= 1.4

    def test_wait_prefers_retry_after_header(self):
        e = _rate_limit_error("try again in 1s", retry_after=5)
        assert 5.4 <= op._retry_wait_seconds(e, 0) <= 5.6

    def test_wait_backoff_and_cap(self):
        e = _rate_limit_error("no hint here")
        assert op._retry_wait_seconds(e, 0) == 2.5
        assert op._retry_wait_seconds(e, 10) == op._RETRY_MAX_WAIT_SEC

    def test_call_with_retry_recovers(self, monkeypatch):
        monkeypatch.setattr(op.time, "sleep", lambda s: None)
        monkeypatch.setattr(op, "_RETRY_ATTEMPTS", 3)
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _rate_limit_error("try again in 1s")
            return "ok"
        assert op._call_with_retry(fn, "chat") == "ok"
        assert calls["n"] == 3

    def test_call_with_retry_gives_up_after_attempts(self, monkeypatch):
        monkeypatch.setattr(op.time, "sleep", lambda s: None)
        monkeypatch.setattr(op, "_RETRY_ATTEMPTS", 2)
        def fn():
            raise _rate_limit_error("try again in 1s")
        with pytest.raises(openai.RateLimitError):
            op._call_with_retry(fn, "chat")

    def test_non_retryable_raises_immediately(self, monkeypatch):
        slept = []
        monkeypatch.setattr(op.time, "sleep", lambda s: slept.append(s))
        def fn():
            raise ValueError("boom")
        with pytest.raises(ValueError):
            op._call_with_retry(fn, "chat")
        assert slept == []

    def test_provider_chat_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(op.time, "sleep", lambda s: None)
        monkeypatch.setattr(op, "_RETRY_ATTEMPTS", 3)
        prov = op.OpenAIProvider(api_key="test-key", default_model="m")
        calls = {"n": 0}
        def create(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _rate_limit_error("try again in 1s")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="hello"))])
        monkeypatch.setattr(prov._client.chat.completions, "create", create)
        assert prov.chat([{"role": "user", "content": "hi"}]) == "hello"
        assert calls["n"] == 2


def _doc(source, category="", injected=False):
    d = {"content": "x", "metadata": {"source": source}}
    if category:
        d["metadata"]["docs_category"] = category
    if injected:
        d["_injected"] = True
    return d


class TestGroundingLabel:
    def test_manual_top(self):
        assert rag_system._grounding_label([_doc("docs"), _doc("jira")]) == "manual"

    def test_digest_top(self):
        assert rag_system._grounding_label([_doc("docs", "digest")]) == "digest"

    def test_non_docs_top(self):
        assert rag_system._grounding_label([_doc("jira"), _doc("docs")]) == ""

    def test_injected_skipped(self):
        assert rag_system._grounding_label([_doc("docs", injected=True), _doc("sharepoint")]) == ""

    def test_empty(self):
        assert rag_system._grounding_label([]) == ""


class TestErrorNoReferences:
    def test_llm_error_yields_no_references(self, monkeypatch):
        """LLM 실패(오류 문구) 시 참고문서가 비어야 한다."""
        docs = [_doc("docs"), _doc("jira")]
        monkeypatch.setattr(rag_system, "rewrite_query", lambda q, h=None: {"rewritten": q, "keywords": [], "is_question": True, "intent": ""})
        monkeypatch.setattr(rag_system, "retrieve_documents", lambda *a, **k: (list(docs), {"vector_ms": 0, "keyword_ms": 0}))
        monkeypatch.setattr(rag_system, "_is_usable_content", lambda d: True)
        monkeypatch.setattr(rag_system, "_inject_jira_docs", lambda q, d: d)
        import company_llm_rag.graph.entity_link as el
        monkeypatch.setattr(el, "inject_entity_docs", lambda q, d: d)
        monkeypatch.setattr(rag_system, "_try_hub_direct_answer", lambda d: None)
        import company_llm_rag.wiki.direct as wd
        monkeypatch.setattr(wd, "try_wiki_direct_answer", lambda d, q: None)
        monkeypatch.setattr(rag_system, "get_llm_response", lambda *a, **k: rag_system._LLM_ERROR_PHRASE)
        monkeypatch.setattr(rag_system, "_build_references", lambda *a, **k: [{"title": "should not appear"}])
        monkeypatch.setattr(rag_system, "_build_manual_grounded_references", lambda *a, **k: [{"title": "should not appear"}])
        answer, refs, timing = rag_system.rag_query("정산 주기?", return_refs=True)
        assert answer == rag_system._LLM_ERROR_PHRASE
        assert refs == []
        assert timing["grounding"] == ""

    def test_manual_answer_has_grounding(self, monkeypatch):
        docs = [_doc("docs"), _doc("jira")]
        monkeypatch.setattr(rag_system, "rewrite_query", lambda q, h=None: {"rewritten": q, "keywords": [], "is_question": True, "intent": ""})
        monkeypatch.setattr(rag_system, "retrieve_documents", lambda *a, **k: (list(docs), {"vector_ms": 0, "keyword_ms": 0}))
        monkeypatch.setattr(rag_system, "_is_usable_content", lambda d: True)
        monkeypatch.setattr(rag_system, "_inject_jira_docs", lambda q, d: d)
        import company_llm_rag.graph.entity_link as el
        monkeypatch.setattr(el, "inject_entity_docs", lambda q, d: d)
        monkeypatch.setattr(rag_system, "_try_hub_direct_answer", lambda d: None)
        import company_llm_rag.wiki.direct as wd
        monkeypatch.setattr(wd, "try_wiki_direct_answer", lambda d, q: None)
        monkeypatch.setattr(rag_system, "get_llm_response", lambda *a, **k: "매뉴얼에 따르면 가능합니다.")
        monkeypatch.setattr(rag_system, "_build_manual_grounded_references", lambda *a, **k: [])
        answer, refs, timing = rag_system.rag_query("구독 가능?", return_refs=True)
        assert timing["grounding"] == "manual"
        assert refs == []


class TestGroundingMarker:
    def test_marker_extracted_and_stripped(self):
        text, g = rag_system._extract_grounding_marker("가능합니다.\n[[근거:매뉴얼]]")
        assert text == "가능합니다." and g == "manual"

    def test_marker_digest_and_other(self):
        assert rag_system._extract_grounding_marker("x [[근거: 다이제스트 ]]")[1] == "digest"
        assert rag_system._extract_grounding_marker("x\n[[근거:기타]]")[1] == ""

    def test_no_marker_returns_none(self):
        text, g = rag_system._extract_grounding_marker("그냥 답변")
        assert text == "그냥 답변" and g is None

    def test_decide_prefers_marker_over_heuristic(self):
        docs = [_doc("docs")]
        assert rag_system._decide_grounding("", docs) == ""          # LLM이 '기타'라고 하면 매뉴얼 1위여도 라벨 없음
        assert rag_system._decide_grounding(None, docs) == "manual"  # 마커 없으면 휴리스틱


class TestProvenanceTableLinksSkipped:
    def test_table_row_links_ignored_prose_links_kept(self):
        from company_llm_rag.rag import provenance
        content = (
            "본문 설명 ([정산기획](../sharepoint-index/digests/2020-08_정산기획서.md)) 참고.\n"
            "| 2021-11 | [광주시장](../sharepoint-index/digests/2021-11_광주시장.md) | WMPO-1 | 계보 |\n"
            "  | 2022-08 | [봉선시장](../sharepoint-index/digests/2022-08_봉선시장.md) | 계보 |\n"
            "본문 이슈 WMPO-2 언급.\n"
        )
        out = provenance.extract_from_chunks([{"content": content, "metadata": {"docs_relpath": "platform/features/sales-settlement.md"}}])
        assert out["digest_relpaths"] == ["platform/sharepoint-index/digests/2020-08_정산기획서.md"]
        assert out["issue_keys"] == ["WMPO-2"]


class TestEntityInjectionShape:
    def test_detect_entities_case_insensitive(self, monkeypatch):
        import company_llm_rag.graph.entity_link as el
        monkeypatch.setattr(el, "get_entities", lambda: [
            {"name": "E쿠폰", "manual": "features/ecoupon.md", "aliases": ["선물하기"]},
            {"name": "쿠폰", "manual": "features/coupon.md", "aliases": []},
        ])
        names = [e["name"] for e in el.detect_entities("e쿠폰 판매 가능?")]
        assert "E쿠폰" in names

    def test_other_docs_capped_deduped_and_appended(self, monkeypatch):
        import company_llm_rag.graph.entity_link as el
        from company_llm_rag.config import settings
        monkeypatch.setattr(settings, "GRAPH_ENTITY_INJECT_ENABLED", True)
        monkeypatch.setattr(el, "detect_entities", lambda q: [
            {"name": "E쿠폰", "manual": "", "aliases": []}, {"name": "쿠폰", "manual": "", "aliases": []}])
        issue_nodes = [{"id": f"issue:K-{i}", "meta": {}} for i in range(3)]
        def mentioned(name, node_type, limit):
            return issue_nodes if node_type == "issue" else [{"id": "doc:c1", "meta": {"original_doc_id": "conf-1"}}]
        monkeypatch.setattr(el, "_mentioned_nodes", mentioned)
        class Coll:
            def get(self, where, include, limit):
                key = list(where.values())[0]["$eq"]
                return {"ids": [key], "documents": ["c"], "metadatas": [{"source": "jira", "jira_issue_key": key}]}
        import company_llm_rag.database as dbm
        monkeypatch.setattr(dbm.db_manager, "get_collection", lambda: Coll())
        retrieved = [{"content": "r", "metadata": {"source": "docs", "original_doc_id": "docs-x"}}]
        out = el.inject_entity_docs("e쿠폰 쿠폰", retrieved)
        assert out[0] is retrieved[0]                       # 검색 결과가 앞
        injected = [d for d in out if d.get("_injected")]
        assert len(injected) <= el._INJECT_MAX_OTHER_DOCS   # 총 상한
        keys = [d["metadata"]["jira_issue_key"] for d in injected]
        assert len(keys) == len(set(keys))                  # 엔티티 간 중복 없음


class TestNumericGuard:
    def _docs(self):
        return [
            {"content": "결제수단은 다섯 갈래다. 정산 주기는 업체마다 일·주·월로 설정한다.", "metadata": {"source": "docs"}},
            {"content": "| 간편결제 | 매장별 설정 | 3.3% |", "metadata": {"source": "docs", "docs_category": "digest"}},
        ]

    def test_number_only_in_digest_downgrades_and_caveats(self):
        ans, g = rag_system._apply_numeric_guard("카드·간편결제 수수료는 3.3%입니다.", "manual", self._docs())
        assert g == "" and "3.3%" in ans and "담당 팀 확인" in ans

    def test_number_present_in_manual_keeps_label(self):
        docs = [{"content": "기본 적립률 1%가 자동으로 생긴다.", "metadata": {"source": "docs"}}]
        ans, g = rag_system._apply_numeric_guard("기본 적립률은 1%입니다.", "manual", docs)
        assert g == "manual" and "담당 팀 확인" not in ans

    def test_no_numbers_untouched(self):
        ans, g = rag_system._apply_numeric_guard("업체 단위로 정산합니다.", "manual", self._docs())
        assert g == "manual" and ans == "업체 단위로 정산합니다."

    def test_non_manual_grounding_untouched(self):
        ans, g = rag_system._apply_numeric_guard("수수료 3.3%", "", self._docs())
        assert g == "" and "담당 팀 확인" not in ans

    def test_whitespace_insensitive_match(self):
        docs = [{"content": "영업일 2 ~ 3일이 소요된다. 배달료 3,000원", "metadata": {"source": "docs"}}]
        ans, g = rag_system._apply_numeric_guard("연동에 영업일 2~3일이 걸립니다.", "manual", docs)
        assert g == "manual"


class TestHubDirectDistanceGate:
    def _docs(self, dist):
        return [
            {"content": "Q", "metadata": {"is_hub_direct": True, "original_doc_id": "hub-1", "title": "Q"},
             "_rrf": 0.12, "_distance": dist},
            {"content": "x", "metadata": {"source": "docs"}, "_rrf": 0.02, "_distance": 0.3},
        ]

    def test_far_hub_doc_not_direct(self, monkeypatch):
        from company_llm_rag.rag import hub_direct
        from company_llm_rag.config import settings
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_TEAM_NAME", "Knowledge Hub")
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_DIRECT_MAX_DISTANCE", 0.40)
        import company_llm_rag.hub_store as hs
        monkeypatch.setattr(hs, "hub_get_reply", lambda doc_id: "원문")
        assert hub_direct.try_hub_direct_answer(self._docs(0.455)) is None

    def test_close_hub_doc_is_direct(self, monkeypatch):
        from company_llm_rag.rag import hub_direct
        from company_llm_rag.config import settings
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_TEAM_NAME", "Knowledge Hub")
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_DIRECT_MAX_DISTANCE", 0.40)
        import company_llm_rag.hub_store as hs
        monkeypatch.setattr(hs, "hub_get_reply", lambda doc_id: "원문")
        monkeypatch.setattr(hub_direct, "_build_hub_intro", lambda q: "안내\n\n---\n\n")
        out = hub_direct.try_hub_direct_answer(self._docs(0.30))
        assert out is not None and out.endswith("원문")

    def test_gate_disabled_when_zero(self, monkeypatch):
        from company_llm_rag.rag import hub_direct
        from company_llm_rag.config import settings
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_TEAM_NAME", "Knowledge Hub")
        monkeypatch.setattr(settings, "KNOWLEDGE_HUB_DIRECT_MAX_DISTANCE", 0)
        import company_llm_rag.hub_store as hs
        monkeypatch.setattr(hs, "hub_get_reply", lambda doc_id: "원문")
        monkeypatch.setattr(hub_direct, "_build_hub_intro", lambda q: "")
        assert hub_direct.try_hub_direct_answer(self._docs(0.9)) == "원문"


def test_numeric_guard_minutes_unit():
    docs = [{"content": "다른 내용", "metadata": {"source": "docs"}}]
    ans, g = rag_system._apply_numeric_guard("7분간 미접수면 취소됩니다.", "manual", docs)
    assert g == "" and "7분" in ans


def test_manual_marker_without_manual_chunk_is_dropped():
    docs = [{"content": "hub", "metadata": {"source": "teams", "is_hub_direct": True}},
            {"content": "c", "metadata": {"source": "confluence"}}]
    assert rag_system._decide_grounding("manual", docs) == ""
    docs.append({"content": "m", "metadata": {"source": "docs"}})
    assert rag_system._decide_grounding("manual", docs) == "manual"

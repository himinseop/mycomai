"""출처 기반 참고문서 테스트 (#61 §12)

그래프(app_data.db) 조회는 전부 monkeypatch로 대체합니다 — 실제 DB 접근 없음.
"""

import json

from company_llm_rag.rag import provenance
from company_llm_rag import rag_system


# ── extract_from_chunks: 다이제스트 링크·이슈키 추출/정규화 ──────────────

class TestExtractFromChunks:
    def test_digest_link_relative_resolution(self):
        """../ 상대경로가 매뉴얼 docs_relpath 기준 저장소 루트 경로로 정규화된다."""
        chunks = [{
            "content": "관련 기획: [바우처 개편](../sharepoint-index/digests/2023-02_voucher.md) 참고",
            "metadata": {"docs_relpath": "platform/features/voucher.md",
                         "source": "docs", "docs_category": "features"},
        }]
        result = provenance.extract_from_chunks(chunks)
        assert result["digest_relpaths"] == [
            "platform/sharepoint-index/digests/2023-02_voucher.md"
        ]

    def test_digest_link_encoding_and_typography_preserved(self):
        """URL 인코딩·타이포그래피 문자는 디코딩하지 않고 그대로 보존한다."""
        href = "../sharepoint-index/digests/2023-02_%EB%B0%94%EC%9A%B0%EC%B2%98%E2%80%99s.md"
        chunks = [{
            "content": f"[제목]({href})",
            "metadata": {"docs_relpath": "platform/features/voucher.md"},
        }]
        result = provenance.extract_from_chunks(chunks)
        assert result["digest_relpaths"] == [
            "platform/sharepoint-index/digests/2023-02_%EB%B0%94%EC%9A%B0%EC%B2%98%E2%80%99s.md"
        ]

    def test_non_digest_link_ignored(self):
        """다이제스트가 아닌 매뉴얼 간 링크는 추출하지 않는다."""
        chunks = [{
            "content": "[다른 매뉴얼](../features/coupon.md) 참고",
            "metadata": {"docs_relpath": "platform/features/voucher.md"},
        }]
        result = provenance.extract_from_chunks(chunks)
        assert result["digest_relpaths"] == []

    def test_dedup_across_chunks_keeps_order(self):
        chunks = [
            {"content": "[a](../sharepoint-index/digests/x.md)",
             "metadata": {"docs_relpath": "platform/features/a.md"}},
            {"content": "[b](../sharepoint-index/digests/y.md) 다시 [a](../sharepoint-index/digests/x.md)",
             "metadata": {"docs_relpath": "platform/features/a.md"}},
        ]
        result = provenance.extract_from_chunks(chunks)
        assert result["digest_relpaths"] == [
            "platform/sharepoint-index/digests/x.md",
            "platform/sharepoint-index/digests/y.md",
        ]

    def test_issue_key_candidates_dedup_order(self):
        chunks = [{
            "content": "관련 일감: WMPO-123, 이후 WMPO-123 재언급, CUPPING-45",
            "metadata": {"docs_relpath": "platform/features/a.md"},
        }]
        result = provenance.extract_from_chunks(chunks)
        assert result["issue_keys"] == ["WMPO-123", "CUPPING-45"]

    def test_issue_key_shaped_false_positive_still_extracted(self):
        """이슈키 형태(문자+숫자)면 실제 프로젝트가 아니어도 후보로 뽑힌다.
        오탐 배제는 이 단계가 아니라 그래프 실존 검증(build_provenance_references)에서 한다.
        (`ECP-C-01`처럼 하이픈 뒤가 숫자로 시작하지 않는 기능 ID는 애초에 정규식에 안 걸린다.)"""
        chunks = [{
            "content": "기능 ID: ECP-01 (실제 지라 프로젝트 아님)",
            "metadata": {"docs_relpath": "platform/features/a.md"},
        }]
        result = provenance.extract_from_chunks(chunks)
        assert "ECP-01" in result["issue_keys"]

    def test_no_docs_relpath_yields_no_digest_links(self):
        chunks = [{"content": "[a](../sharepoint-index/digests/x.md)", "metadata": {}}]
        result = provenance.extract_from_chunks(chunks)
        assert result["digest_relpaths"] == []


# ── build_provenance_references: 그래프 조회 기반 조립 ───────────────────

class TestBuildProvenanceReferences:
    def test_real_key_included_typo_like_key_excluded(self, monkeypatch):
        """그래프에 실존하는 이슈만 포함 — ECP-C-01/C-01 류 오탐은 배제된다."""
        fake_nodes = {
            "issue:WMPO-123": {
                "id": "issue:WMPO-123", "type": "issue", "label": "실존 이슈",
                "meta": {"url": "https://jira.example/browse/WMPO-123", "project": "WMPO"},
            },
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )

        refs = provenance.build_provenance_references(
            digest_relpaths=[], issue_keys=["WMPO-123", "C-01", "ECP-C-01"], max_refs=10,
        )

        assert len(refs) == 1
        assert refs[0]["source"] == "jira"
        assert refs[0]["issue_key"] == "WMPO-123"
        assert refs[0]["url"] == "https://jira.example/browse/WMPO-123"

    def test_issue_key_url_falls_back_to_jira_base_url(self, monkeypatch):
        fake_nodes = {
            "issue:WMPO-9": {"id": "issue:WMPO-9", "type": "issue", "label": "URL 없는 이슈", "meta": {}},
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )
        monkeypatch.setattr(provenance.settings, "JIRA_BASE_URL", "https://o2olab.atlassian.net")

        refs = provenance.build_provenance_references(
            digest_relpaths=[], issue_keys=["WMPO-9"], max_refs=10,
        )
        assert refs[0]["url"] == "https://o2olab.atlassian.net/browse/WMPO-9"

    def test_digest_ref_plus_related_issues_implementation_role_first(self, monkeypatch):
        """다이제스트 참고문서 + 관련 일감(구현 우선, 다이제스트당 최대 2)."""
        digest_id = "doc:docs-platform/sharepoint-index/digests/2023-02_voucher.md"
        fake_nodes = {
            digest_id: {
                "id": digest_id, "type": "doc", "label": "바우처 개편 다이제스트",
                "meta": {
                    "url": "https://sharepoint.example/voucher.docx",
                    "digest_issues": json.dumps([
                        {"role": "원인", "key": "WMPO-102", "note": "원인"},
                        {"role": "구현", "key": "WMPO-100", "note": "구현"},
                        {"role": "후속", "key": "WMPO-101", "note": "후속"},
                    ], ensure_ascii=False),
                },
            },
            "issue:WMPO-100": {"id": "issue:WMPO-100", "type": "issue", "label": "구현 이슈",
                                "meta": {"url": "https://jira.example/browse/WMPO-100"}},
            "issue:WMPO-101": {"id": "issue:WMPO-101", "type": "issue", "label": "후속 이슈",
                                "meta": {"url": "https://jira.example/browse/WMPO-101"}},
            "issue:WMPO-102": {"id": "issue:WMPO-102", "type": "issue", "label": "원인 이슈",
                                "meta": {"url": "https://jira.example/browse/WMPO-102"}},
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )

        refs = provenance.build_provenance_references(
            digest_relpaths=["platform/sharepoint-index/digests/2023-02_voucher.md"],
            issue_keys=[], max_refs=10,
        )

        assert refs[0]["source"] == "docs"
        assert refs[0]["url"] == "https://sharepoint.example/voucher.docx"
        jira_keys = [r["issue_key"] for r in refs if r["source"] == "jira"]
        # 다이제스트당 최대 2건, 구현 우선 → 후속. 원인은 상한 초과로 제외.
        assert jira_keys == ["WMPO-100", "WMPO-101"]

    def test_missing_digest_node_silently_skipped(self, monkeypatch):
        monkeypatch.setattr(provenance.graph_store, "get_nodes", lambda ids: {})
        refs = provenance.build_provenance_references(
            digest_relpaths=["platform/sharepoint-index/digests/none.md"],
            issue_keys=["WMPO-1"], max_refs=10,
        )
        assert refs == []

    def test_zero_candidates_yields_empty_list(self, monkeypatch):
        monkeypatch.setattr(provenance.graph_store, "get_nodes", lambda ids: {})
        refs = provenance.build_provenance_references(
            digest_relpaths=[], issue_keys=[], max_refs=10,
        )
        assert refs == []

    def test_max_refs_cap_respected(self, monkeypatch):
        fake_nodes = {
            f"issue:WMPO-{i}": {
                "id": f"issue:WMPO-{i}", "type": "issue", "label": f"이슈{i}",
                "meta": {"url": f"https://jira.example/browse/WMPO-{i}"},
            }
            for i in range(5)
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )
        refs = provenance.build_provenance_references(
            digest_relpaths=[], issue_keys=[f"WMPO-{i}" for i in range(5)], max_refs=2,
        )
        assert len(refs) == 2

    def test_url_dedup_across_digest_and_direct_issue(self, monkeypatch):
        """같은 이슈가 다이제스트 관련 일감과 매뉴얼 직접 인용 양쪽에 있으면 한 번만 포함."""
        digest_id = "doc:docs-platform/sharepoint-index/digests/x.md"
        fake_nodes = {
            digest_id: {
                "id": digest_id, "type": "doc", "label": "다이제스트",
                "meta": {
                    "url": "https://sharepoint.example/x.docx",
                    "digest_issues": json.dumps([{"role": "구현", "key": "WMPO-1", "note": ""}]),
                },
            },
            "issue:WMPO-1": {"id": "issue:WMPO-1", "type": "issue", "label": "이슈1",
                              "meta": {"url": "https://jira.example/browse/WMPO-1"}},
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )
        refs = provenance.build_provenance_references(
            digest_relpaths=["platform/sharepoint-index/digests/x.md"],
            issue_keys=["WMPO-1"], max_refs=10,
        )
        jira_refs = [r for r in refs if r["source"] == "jira"]
        assert len(jira_refs) == 1


# ── rag_system 모드 분기 ─────────────────────────────────────────────

class TestIsManualGrounded:
    def test_manual_top_is_grounded(self):
        docs = [{"metadata": {"source": "docs", "docs_category": "features"}}]
        assert rag_system._is_manual_grounded(docs) is True

    def test_digest_top_is_grounded(self):
        """E2E 검증 후 확장: 다이제스트 최상위도 출처 기반 모드 — 키워드·주입 잡음 제외 (#61 §12)."""
        docs = [{"metadata": {"source": "docs", "docs_category": "digest"}}]
        assert rag_system._is_manual_grounded(docs) is True

    def test_jira_top_is_not_grounded(self):
        docs = [{"metadata": {"source": "jira"}}]
        assert rag_system._is_manual_grounded(docs) is False

    def test_empty_docs_is_not_grounded(self):
        assert rag_system._is_manual_grounded([]) is False


class TestBuildManualGroundedReferences:
    def _manual_chunk(self, content):
        return {
            "content": content,
            "metadata": {
                "source": "docs", "docs_category": "features",
                "docs_relpath": "platform/features/voucher.md", "title": "바우처 매뉴얼",
            },
            "_distance": 0.0,
        }

    def _jira_doc(self, key, injected=False, distance=0.5):
        doc = {
            "content": f"{key} 내용",
            "metadata": {"source": "jira", "title": key, "url": f"https://jira.example/browse/{key}",
                         "jira_issue_key": key},
            "_distance": distance,
        }
        if injected:
            doc["_injected"] = True
        return doc

    def test_provenance_kept_injected_and_search_result_excluded(self, monkeypatch):
        """E2E 검증 후 정책 확정: 주입(entity link) 문서는 주제 수준 연결이라 구문
        출처가 아님 — 검색 결과와 함께 제외. provenance·인용·Hub·컨텍스트 다이제스트만 유지."""
        digest_id = "doc:docs-platform/sharepoint-index/digests/2023-02_voucher.md"
        fake_nodes = {
            digest_id: {
                "id": digest_id, "type": "doc", "label": "바우처 다이제스트",
                "meta": {"url": "https://sharepoint.example/voucher.docx", "digest_issues": "[]"},
            },
        }
        monkeypatch.setattr(
            provenance.graph_store, "get_nodes",
            lambda ids: {i: fake_nodes[i] for i in ids if i in fake_nodes},
        )

        manual = self._manual_chunk(
            "관련 기획: [바우처 개편](../sharepoint-index/digests/2023-02_voucher.md)"
        )
        irrelevant_jira = self._jira_doc("WMPO-999")           # 키워드로만 걸린 무관 이슈 — 제외
        injected_jira = self._jira_doc("WMPO-1", injected=True)  # 엔티티 주입 — 제외 (주제 수준)

        retrieved_docs = [manual, irrelevant_jira, injected_jira]
        refs = rag_system._build_manual_grounded_references(retrieved_docs, cited_indices=set())

        urls = {r["url"] for r in refs}
        assert "https://sharepoint.example/voucher.docx" in urls
        assert "https://jira.example/browse/WMPO-1" not in urls
        assert "https://jira.example/browse/WMPO-999" not in urls

    def test_context_digest_kept_as_reference(self, monkeypatch):
        """컨텍스트에 포함된 다이제스트는 답변의 정책 근거 — 참고문서 유지 (#61 11-A 원본 링크)."""
        monkeypatch.setattr(provenance.graph_store, "get_nodes", lambda ids: {})
        manual = self._manual_chunk("다이제스트 링크 없는 본문")
        digest_doc = {
            "content": "다이제스트 본문",
            "metadata": {
                "source": "docs", "docs_category": "digest",
                "title": "E쿠폰 고도화", "url": "https://sharepoint.example/ecoupon.pptx",
            },
            "_distance": 0.9,  # 거리 필터 무관하게 유지되어야 함
        }
        irrelevant_jira = self._jira_doc("WMPO-999")
        refs = rag_system._build_manual_grounded_references(
            [manual, digest_doc, irrelevant_jira], cited_indices=set()
        )
        urls = {r["url"] for r in refs}
        assert "https://sharepoint.example/ecoupon.pptx" in urls
        assert "https://jira.example/browse/WMPO-999" not in urls

    def test_no_provenance_no_citation_yields_empty_list(self, monkeypatch):
        monkeypatch.setattr(provenance.graph_store, "get_nodes", lambda ids: {})
        manual = self._manual_chunk("다이제스트 링크도 이슈키도 없는 본문")
        irrelevant_jira = self._jira_doc("WMPO-999")
        refs = rag_system._build_manual_grounded_references(
            [manual, irrelevant_jira], cited_indices=set()
        )
        assert refs == []

    def test_cited_ref_included_when_manual_grounded(self, monkeypatch):
        monkeypatch.setattr(provenance.graph_store, "get_nodes", lambda ids: {})
        manual = self._manual_chunk("본문")
        cited_jira = self._jira_doc("WMPO-5")
        refs = rag_system._build_manual_grounded_references(
            [manual, cited_jira], cited_indices={1}
        )
        assert any(r["url"] == "https://jira.example/browse/WMPO-5" for r in refs)

    def test_non_manual_grounded_uses_existing_full_references(self):
        """1순위가 jira면 기존 로직(_build_references)이 검색 결과 전체를 대상으로 한다 — 회귀 없음."""
        jira_top = self._jira_doc("WMPO-1", distance=0.1)
        other_jira = self._jira_doc("WMPO-2", distance=0.1)
        retrieved_docs = [jira_top, other_jira]

        assert rag_system._is_manual_grounded(retrieved_docs) is False
        refs = rag_system._build_references(retrieved_docs, listing=False, cited_indices=set())
        urls = {r["url"] for r in refs}
        assert "https://jira.example/browse/WMPO-1" in urls
        assert "https://jira.example/browse/WMPO-2" in urls

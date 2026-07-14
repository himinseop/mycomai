"""
LLM 위키 테스트 (#58 Phase 1)

ChromaDB는 fake collection으로 대체 — 임베딩 등록/제거 호출 검증.
"""

import json

import pytest

from company_llm_rag.config import settings


class FakeCollection:
    def __init__(self):
        self.docs = {}   # id → {document, metadata}

    def add(self, ids, documents, metadatas, **kw):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = {"document": d, "metadata": m}

    def get(self, where=None, include=None, **kw):
        ids = []
        for cid, rec in self.docs.items():
            if where:
                k, v = next(iter(where.items()))
                if rec["metadata"].get(k) != v:
                    continue
            ids.append(cid)
        return {"ids": ids}

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)


@pytest.fixture()
def wiki_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "APP_DATA_DB_PATH", str(tmp_path / "app.db"))
    # wiki_store의 스레드 로컬 연결 캐시 초기화
    from company_llm_rag.wiki import wiki_store
    import threading
    monkeypatch.setattr(wiki_store, "_local", threading.local())

    fake = FakeCollection()
    from company_llm_rag import database
    monkeypatch.setattr(database.db_manager, "get_collection", lambda *a, **k: fake)

    wiki_store.init_wiki_db()
    return wiki_store, fake


def _make_page(ws, topic="point-accrual"):
    return ws.upsert_page(
        topic=topic, title="포인트 적립 정책",
        content="## 개요\n포인트는 결제 시 적립됩니다. [출처: 포인트 정책 문서]",
        questions=["포인트 적립 어떻게 해?", "적립율은 얼마야?"],
        facts=[{"key": "적립 시점", "value": "결제 시", "source": "포인트 정책 문서"}],
        source_doc_ids=["doc-1", "doc-2", "doc-3"],
        source_hash="abc123", model="gpt-4o-mini",
    )


def test_upsert_and_embeddings(wiki_env):
    ws, fake = wiki_env
    page = _make_page(ws)
    assert page["status"] == "draft"
    assert page["facts"][0]["key"] == "적립 시점"
    # 대표 질문 2개가 임베딩 등록됨 (is_wiki 메타)
    assert len(fake.docs) == 2
    meta = list(fake.docs.values())[0]["metadata"]
    assert meta["is_wiki"] is True and meta["wiki_id"] == page["id"]


def test_status_transitions_and_embedding_lifecycle(wiki_env):
    ws, fake = wiki_env
    page = _make_page(ws)
    # 승인
    assert ws.set_status(page["id"], "approved")
    assert ws.get_page(page["id"])["status"] == "approved"
    assert len(fake.docs) == 2                       # 임베딩 유지
    # 폐기 → 임베딩 제거 + 검색 주입 차단
    ws.set_status(page["id"], "disabled")
    assert len(fake.docs) == 0
    assert ws.get_page_by_wiki_id(page["id"]) is None
    # 복구 → 임베딩 재등록
    ws.set_status(page["id"], "draft")
    assert len(fake.docs) == 2
    assert ws.get_page_by_wiki_id(page["id"]) is not None
    # 잘못된 상태
    with pytest.raises(ValueError):
        ws.set_status(page["id"], "published")


def test_rebuild_demotes_to_draft(wiki_env):
    ws, fake = wiki_env
    page = _make_page(ws)
    ws.set_status(page["id"], "approved")
    updated = _make_page(ws)                          # 같은 토픽 재생성
    assert updated["id"] == page["id"]
    assert updated["status"] == "draft"               # 재검수 강등


def test_source_hash_stability(wiki_env):
    ws, _ = wiki_env
    h1 = ws.compute_source_hash(["b", "a", "c"])
    h2 = ws.compute_source_hash(["c", "a", "b"])
    h3 = ws.compute_source_hash(["a", "b"])
    assert h1 == h2 and h1 != h3                      # 순서 무관, 내용 민감


def test_builder_output_parsing():
    from company_llm_rag.wiki.page_builder import _parse_output
    raw = """===PAGE===
## 개요
정산은 매주 월요일입니다. [출처: 정산 가이드]
===FACTS===
[{"key": "정산주기", "value": "매주 월요일", "source": "정산 가이드"}]"""
    content, facts = _parse_output(raw)
    assert content.startswith("## 개요")
    assert "[출처:" in content
    assert facts[0]["key"] == "정산주기"
    # 팩트 블록 없거나 깨져도 본문은 유지
    content2, facts2 = _parse_output("===PAGE===\n본문만 [출처: x]")
    assert facts2 == [] and "본문만" in content2
    content3, facts3 = _parse_output("===PAGE===\n본문 [출처: x]\n===FACTS===\n깨진 json")
    assert facts3 == [] and "본문" in content3


def test_builder_rejects_no_citation(wiki_env, monkeypatch):
    ws, _ = wiki_env
    from company_llm_rag.wiki import page_builder as pb
    docs = [{"content": f"내용 {i}", "metadata": {"title": f"문서{i}", "original_doc_id": f"d{i}",
                                               "content_hash": f"h{i}", "source": "sharepoint"}}
            for i in range(5)]
    monkeypatch.setattr(pb, "collect_sources", lambda qs: docs)

    class FakeLLM:
        def chat(self, *a, **k):
            return "===PAGE===\n출처 인용이 없는 긴 본문입니다. " * 20
    monkeypatch.setattr(pb, "resolve_llm", lambda role: (FakeLLM(), None))
    with pytest.raises(ValueError, match="출처 인용"):
        pb.build_page("t", "제목", ["질문 하나"])


def test_builder_happy_path(wiki_env, monkeypatch):
    ws, fake = wiki_env
    from company_llm_rag.wiki import page_builder as pb
    docs = [{"content": f"내용 {i}", "metadata": {"title": f"문서{i}", "original_doc_id": f"d{i}",
                                               "content_hash": f"h{i}", "source": "confluence"}}
            for i in range(6)]
    monkeypatch.setattr(pb, "collect_sources", lambda qs: docs)

    body = ("## 개요\n" + "정산 절차 설명. [출처: 문서1] " * 20 +
            "\n===FACTS===\n[{\"key\": \"주기\", \"value\": \"주 1회\", \"source\": \"문서1\"}]")

    class FakeLLM:
        def chat(self, *a, **k):
            return "===PAGE===\n" + body
    monkeypatch.setattr(pb, "resolve_llm", lambda role: (FakeLLM(), None))
    monkeypatch.setattr(pb, "current_model_name", lambda role: "fake-model")

    page = pb.build_page("settlement", "정산 절차", ["정산 언제 돼?"])
    assert page["status"] == "draft"
    assert page["facts"][0]["key"] == "주기"
    assert len(page["source_doc_ids"]) == 6
    assert page["source_hash"]
    assert len(fake.docs) == 1                        # 질문 1개 임베딩


def test_wiki_context_injection(wiki_env):
    """검색 결과의 위키 문서(질문 텍스트)가 페이지 본문으로 치환되는지."""
    ws, _ = wiki_env
    page = _make_page(ws)
    # retrieval_module의 주입 로직과 동일한 경로 사용
    from company_llm_rag.wiki.wiki_store import get_page_by_wiki_id
    p = get_page_by_wiki_id(page["id"])
    assert p is not None and "[출처:" in p["content"]
    ws.set_status(page["id"], "disabled")
    assert get_page_by_wiki_id(page["id"]) is None

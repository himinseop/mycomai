from datetime import datetime, timezone

import pytest

import company_llm_rag.rag_system as rag_system
import company_llm_rag.retrieval_module as retrieval_module


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = cls(2026, 3, 25, 0, 0, 0, tzinfo=timezone.utc)
        if tz is None:
            return base.replace(tzinfo=None)
        return base.astimezone(tz)


def _jira_doc(created_at: str, title: str) -> dict:
    return {
        "content": f"{title} description",
        "metadata": {
            "source": "jira",
            "title": title,
            "created_at": created_at,
        },
    }


class TestRecencyQueryDetection:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("최근 등록된 지라 일감 보여줘", True),
            ("최신 Jira 이슈 알려줘", True),
            ("방금 등록된 지라 티켓 보여줘", True),
            ("요즘 올라온 버그 알려줘", True),
            ("지라 이슈 담당자 알려줘", False),
        ],
    )
    def test_is_recency_query(self, query, expected):
        assert rag_system._is_recency_query(query) is expected


class TestExplicitPeriodParsing:
    @pytest.mark.parametrize(
        ("query", "expected_days"),
        [
            ("최근 7일 지라 일감 보여줘", 7),
            ("지난 2주간 등록된 Jira 이슈 알려줘", 14),
            ("1개월 내 생성된 Jira 이슈 보여줘", 30),
            ("최근 한 달 지라 현황 알려줘", 30),
            ("최근 두 달 Jira 이슈 목록", 60),
            ("최근 세 달 Jira 이슈 목록", 90),
            ("지라 이슈 알려줘", None),
        ],
    )
    def test_parse_explicit_period(self, query, expected_days):
        assert rag_system._parse_explicit_period(query) == expected_days


class TestJiraRecencyFilter:
    def test_apply_jira_recency_filter_uses_explicit_period(self, monkeypatch):
        monkeypatch.setattr(rag_system, "datetime", FixedDateTime)
        docs = [
            _jira_doc("2026-03-23T00:00:00+00:00", "WMPO-1"),
            _jira_doc("2026-03-10T00:00:00+00:00", "WMPO-2"),
        ]

        filtered, window = rag_system._apply_jira_recency_filter(docs, explicit_days=7)

        assert window == 7
        assert [doc["metadata"]["title"] for doc in filtered] == ["WMPO-1"]

    def test_apply_jira_recency_filter_expands_default_window(self, monkeypatch):
        monkeypatch.setattr(rag_system, "datetime", FixedDateTime)
        docs = [
            _jira_doc("2026-03-20T00:00:00+00:00", "WMPO-1"),
            _jira_doc("2026-02-20T00:00:00+00:00", "WMPO-2"),
            _jira_doc("2026-01-30T00:00:00+00:00", "WMPO-3"),
            _jira_doc("2025-12-20T00:00:00+00:00", "WMPO-4"),
        ]

        filtered, window = rag_system._apply_jira_recency_filter(docs)

        assert window == 60
        assert [doc["metadata"]["title"] for doc in filtered] == ["WMPO-1", "WMPO-2", "WMPO-3"]


class TestRecencyScore:
    def test_recency_score_prefers_newer_jira_documents(self, monkeypatch):
        monkeypatch.setattr(retrieval_module, "datetime", FixedDateTime)

        newer = retrieval_module._recency_score(
            {"source": "jira", "created_at": "2026-03-24T00:00:00+00:00"},
            jira_scale=True,
        )
        older = retrieval_module._recency_score(
            {"source": "jira", "created_at": "2025-12-25T00:00:00+00:00"},
            jira_scale=True,
        )

        assert newer > older
        assert 0 < older < 1

    def test_recency_score_returns_neutral_when_date_missing(self, monkeypatch):
        monkeypatch.setattr(retrieval_module, "datetime", FixedDateTime)

        assert retrieval_module._recency_score({}, jira_scale=False) == 0.5

"""
인사이트 API 테스트 (#56)

단일 엔드포인트(POST /api/v1/insights) + 도메인 자동 선택 기준.
설계 TC 1~12 + rate limit + 도메인 분류를 커버합니다. LLM은 mock.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from company_llm_rag.config import settings

MOCK_LLM_JSON = json.dumps({
    "summary": "6월 총매출은 전월 대비 증가했습니다.",
    "highlights": [
        {"type": "growth", "title": "성장", "detail": "증가",
         "evidence": {"metric": "total", "value": 1}},
    ],
    "anomalies": [],
}, ensure_ascii=False)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """tmp DB로 store를 초기화하고 라우터만 붙인 테스트 앱 + 키 발급."""
    db_path = tmp_path_factory.mktemp("insight") / "app_data.db"
    orig_db = settings.APP_DATA_DB_PATH
    settings.APP_DATA_DB_PATH = str(db_path)

    from company_llm_rag.insight_api import store
    store.init_insight_db()
    sales_client = store.create_client("테스트-매출", ["sales"])
    voc_client = store.create_client("테스트-VOC", ["voc"])
    all_client = store.create_client("테스트-전체", ["*"])
    revoked = store.create_client("테스트-폐기", ["sales"])
    store.set_client_active(revoked["id"], False)

    from company_llm_rag.insight_api.router import router
    app = FastAPI()
    app.include_router(router)

    yield {
        "client": TestClient(app),
        "sales_key": sales_client["api_key"],
        "voc_key": voc_client["api_key"],
        "all_key": all_client["api_key"],
        "revoked_key": revoked["api_key"],
        "store": store,
    }
    settings.APP_DATA_DB_PATH = orig_db


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """기본: 해석 LLM이 정상 JSON을 반환하도록 mock."""
    from company_llm_rag.insight_api import router as router_module
    monkeypatch.setattr(router_module, "_call_llm", lambda messages: MOCK_LLM_JSON)
    yield monkeypatch


def _sales_body(**over):
    body = {
        "period": {"from": "2026-06-01", "to": "2026-06-30"},
        "records": [
            {"date": "2026-06-01", "amount": 100000, "count": 10,
             "dimension": {"channel": "제로페이"}},
            {"date": "2026-06-02", "amount": 200000, "count": 20,
             "dimension": {"channel": "카드"}},
        ],
    }
    body.update(over)
    return body


def _voc_body(**over):
    recs = []
    for d in range(1, 11):
        recs.append({"date": f"2026-06-{d:02d}", "text": f"배송이 빨라요 {d}",
                     "rating": 5, "category": "배송", "channel": "앱"})
    recs += [
        {"date": "2026-06-05", "text": "포인트 적립이 안 돼요. 확인 부탁드립니다.",
         "rating": 1, "category": "포인트", "channel": "앱"},
        {"date": "2026-06-06", "text": "정산 금액이 이상해요",
         "rating": 2, "category": "정산", "channel": "웹"},
    ]
    body = {"period": {"from": "2026-06-01", "to": "2026-06-30"}, "records": recs}
    body.update(over)
    return body


def _post(env, body, key=None):
    headers = {"X-API-Key": key} if key else {}
    return env["client"].post("/api/v1/insights", json=body, headers=headers)


# ── TC 1~3, 12: 인증/인가 ───────────────────────────────────────────────────

def test_tc1_missing_key(env):
    assert _post(env, _sales_body()).status_code == 401


def test_tc2_invalid_and_revoked_key(env):
    assert _post(env, _sales_body(), key="mci_wrong").status_code == 401
    assert _post(env, _sales_body(), key=env["revoked_key"]).status_code == 401


def test_tc3_scope_forbidden_after_auto_selection(env):
    # sales scope 키로 voc 데이터 → 자동 선택된 voc가 scope 밖 → 403
    assert _post(env, _voc_body(), key=env["sales_key"]).status_code == 403
    # voc scope 키로 sales 데이터 → 403
    assert _post(env, _sales_body(), key=env["voc_key"]).status_code == 403


def test_wildcard_scope_allows_all(env):
    assert _post(env, _sales_body(), key=env["all_key"]).status_code == 200
    assert _post(env, _voc_body(), key=env["all_key"]).status_code == 200


def test_tc12_ip_allowlist(env, monkeypatch):
    monkeypatch.setattr(settings, "API_ALLOWED_IPS", ["10.0.0.0/8"])
    assert _post(env, _sales_body(), key=env["sales_key"]).status_code == 403


# ── 도메인 자동 선택 ────────────────────────────────────────────────────────

def test_auto_select_sales_by_structure(env):
    r = _post(env, _sales_body(), key=env["sales_key"])
    assert r.status_code == 200
    assert r.json()["domain"] == "sales"
    assert r.json()["domain_selection"] == "structure"


def test_auto_select_voc_by_structure(env):
    r = _post(env, _voc_body(), key=env["voc_key"])
    assert r.status_code == 200
    assert r.json()["domain"] == "voc"
    assert r.json()["domain_selection"] == "structure"


def test_ambiguous_falls_back_to_llm(env, monkeypatch):
    # amount+text 혼합 → 구조 감지 후보 2개 → LLM 분류
    from company_llm_rag.insight_api import classifier
    monkeypatch.setattr(classifier, "_call_llm",
                        lambda messages: '{"domain": "voc"}')
    records = [{"date": f"2026-06-{d:02d}", "amount": 1000,
                "text": f"후기 {d}"} for d in range(1, 11)]
    r = _post(env, {"records": records, "question": "고객 불만 주제를 알려줘"},
              key=env["all_key"])
    assert r.status_code == 200
    assert r.json()["domain"] == "voc"
    assert r.json()["domain_selection"] == "llm"


def test_classification_failure_422(env, monkeypatch):
    from company_llm_rag.insight_api import classifier
    def _boom(messages):
        raise RuntimeError("llm down")
    monkeypatch.setattr(classifier, "_call_llm", _boom)
    records = [{"date": "2026-06-01", "value": 1}] * 10   # 어떤 signature도 불충족
    r = _post(env, {"records": records}, key=env["all_key"])
    assert r.status_code == 422
    assert "domain" in r.json()["detail"]


def test_explicit_domain_override(env):
    r = _post(env, {**_sales_body(), "domain": "sales"}, key=env["sales_key"])
    assert r.status_code == 200
    assert r.json()["domain_selection"] == "explicit"


def test_explicit_unknown_domain_422(env):
    r = _post(env, {**_sales_body(), "domain": "nope"}, key=env["sales_key"])
    assert r.status_code == 422


def test_period_inferred_from_records(env):
    body = _sales_body()
    del body["period"]
    r = _post(env, body, key=env["sales_key"])
    assert r.status_code == 200
    p = r.json()["stats"]["period"]
    assert p["from"] == "2026-06-01" and p["to"] == "2026-06-02"


# ── TC 4~6: 정상 분석 ───────────────────────────────────────────────────────

def test_tc4_basic_sales(env):
    r = _post(env, _sales_body(), key=env["sales_key"])
    assert r.status_code == 200
    data = r.json()
    assert data["summary"]
    assert data["stats"]["total"] == 300000          # 서버 계산값 일치
    assert data["stats"]["days_observed"] == 2
    assert data["stats"]["count_total"] == 30
    assert data["meta"]["model"]
    assert data["request_id"].startswith("req-")


def test_tc5_compare_period_growth(env):
    body = _sales_body(
        compare_period={"from": "2026-05-01", "to": "2026-05-31"},
        records=[
            {"date": "2026-05-10", "amount": 100000},
            {"date": "2026-06-10", "amount": 150000},
        ],
    )
    r = _post(env, body, key=env["sales_key"])
    assert r.status_code == 200
    cmp = r.json()["stats"]["compare"]
    assert cmp["prev_total"] == 100000
    assert cmp["growth"] == 0.5


def test_tc6_dimension_aggregation(env):
    r = _post(env, _sales_body(), key=env["sales_key"])
    dims = r.json()["stats"]["dimensions"]
    assert "channel" in dims
    by_value = {i["value"]: i for i in dims["channel"]}
    assert by_value["카드"]["amount"] == 200000
    assert abs(by_value["카드"]["share"] - 2 / 3) < 0.001


# ── TC 7~8: 입력 검증 ───────────────────────────────────────────────────────

def test_tc7_empty_and_over_limit(env):
    assert _post(env, _sales_body(records=[]), key=env["sales_key"]).status_code == 422
    over = [{"date": "2026-06-01", "amount": 1}] * 10_001
    assert _post(env, _sales_body(records=over), key=env["sales_key"]).status_code == 422


def test_tc8_invalid_period(env):
    body = _sales_body(period={"from": "2026-06-30", "to": "2026-06-01"})
    assert _post(env, body, key=env["sales_key"]).status_code == 422


def test_no_records_in_period(env):
    body = _sales_body(records=[{"date": "2025-01-01", "amount": 1}])
    assert _post(env, body, key=env["sales_key"]).status_code == 422


def test_payload_size_limit(env, monkeypatch):
    from company_llm_rag.insight_api import router as router_module
    monkeypatch.setattr(router_module, "MAX_PAYLOAD_BYTES", 10)
    assert _post(env, _sales_body(), key=env["sales_key"]).status_code == 422


# ── TC 9~10: LLM 실패 / 호출 이력 ──────────────────────────────────────────

def test_tc9_llm_failure_502(env, monkeypatch):
    from company_llm_rag.insight_api import router as router_module

    def _boom(messages):
        raise RuntimeError("llm down")
    monkeypatch.setattr(router_module, "_call_llm", _boom)

    r = _post(env, _sales_body(), key=env["sales_key"])
    assert r.status_code == 502
    hist = env["store"].get_call_history(limit=1)
    assert hist["items"][0]["status"] == 502
    assert "llm down" in (hist["items"][0]["error"] or "")


def test_tc10_call_history_no_raw_data(env):
    r = _post(env, _sales_body(question="6월 매출 어때?"), key=env["sales_key"])
    assert r.status_code == 200
    hist = env["store"].get_call_history(limit=1)
    item = hist["items"][0]
    assert item["status"] == 200
    assert item["domain"] == "sales"
    assert item["client_name"] == "테스트-매출"
    summary = json.loads(item["request_summary"])
    assert summary["rows"] == 2
    assert summary["domain_selection"] == "structure"
    assert summary["question"] == "6월 매출 어때?"
    assert "records" not in summary                    # 원본 데이터 미저장
    assert "amount" not in item["request_summary"]


# ── TC 11: 이상치 탐지 ─────────────────────────────────────────────────────

def test_tc11_anomaly_detection(env):
    records = [{"date": f"2026-06-{d:02d}", "amount": 100000} for d in range(1, 15)]
    records.append({"date": "2026-06-15", "amount": 2000000})   # 스파이크
    r = _post(env, _sales_body(records=records), key=env["sales_key"])
    assert r.status_code == 200
    anomalies = r.json()["stats"]["anomalies"]
    assert any(a["date"] == "2026-06-15" and a["direction"] == "spike"
               for a in anomalies)


# ── rate limit ──────────────────────────────────────────────────────────────

def test_rate_limit_429_and_logged(env):
    from company_llm_rag.insight_api import ratelimit
    limited = env["store"].create_client("테스트-리밋", ["sales"], rate_limit_per_min=2)
    ratelimit.reset()
    assert _post(env, _sales_body(), key=limited["api_key"]).status_code == 200
    assert _post(env, _sales_body(), key=limited["api_key"]).status_code == 200
    r = _post(env, _sales_body(), key=limited["api_key"])
    assert r.status_code == 429
    hist = env["store"].get_call_history(limit=1)
    assert hist["items"][0]["status"] == 429          # 429도 이력에 기록
    ratelimit.reset()


def test_rate_limit_default_applies(env, monkeypatch):
    from company_llm_rag.insight_api import ratelimit
    monkeypatch.setattr(settings, "INSIGHT_RATE_LIMIT_PER_MIN", 1)
    ratelimit.reset()
    assert _post(env, _sales_body(), key=env["sales_key"]).status_code == 200
    assert _post(env, _sales_body(), key=env["sales_key"]).status_code == 429
    ratelimit.reset()


# ── 금액/증감률 표시 문자열 (LLM 단위 변환 오류 방지 — 서버 확정) ──────────

def test_krw_display_formatting(env):
    from company_llm_rag.insight_api.domains.sales import _format_krw, _format_pct
    assert _format_krw(16_050_000) == "1,605만 원"
    assert _format_krw(3_600_000) == "360만 원"
    assert _format_krw(320_000_000) == "3.2억 원"
    assert _format_krw(9_500) == "9,500원"
    assert _format_pct(0.5) == "+50.0%"
    assert _format_pct(-0.122) == "-12.2%"

    r = _post(env, _sales_body(), key=env["sales_key"])
    stats = r.json()["stats"]
    assert stats["total_display"] == "30만 원"
    assert stats["best_day"]["amount_display"] == "20만 원"


# ── voc 도메인 ──────────────────────────────────────────────────────────────

def test_voc_stats_and_sample_privacy(env):
    r = _post(env, _voc_body(), key=env["voc_key"])
    assert r.status_code == 200
    stats = r.json()["stats"]
    assert stats["total_count"] == 12
    assert stats["rating"]["negative_count"] == 2
    assert abs(stats["rating"]["negative_ratio"] - 2 / 12) < 0.001
    cats = {c["value"]: c["count"] for c in stats["categories"]}
    assert cats == {"배송": 10, "포인트": 1, "정산": 1}
    assert "samples" not in stats                     # 원문 샘플은 응답에 비노출
    # 이력에도 VOC 원문 미저장
    item = env["store"].get_call_history(limit=1)["items"][0]
    assert item["status"] == 200 and item["domain"] == "voc"
    assert "포인트 적립이 안 돼요" not in (item["request_summary"] or "")


def test_voc_negative_priority_sampling(env):
    from company_llm_rag.insight_api.domains.voc import VocDomain, VocInsightRequest
    req = VocInsightRequest.model_validate(_voc_body())
    stats = VocDomain().preprocess(req)
    # 샘플 선두는 부정 피드백 (최신순)
    assert stats["samples"][0]["rating"] <= 2
    assert stats["samples"][1]["rating"] <= 2


# ── 도메인 목록 ─────────────────────────────────────────────────────────────

def test_list_domains_scoped(env):
    r = env["client"].get("/api/v1/insights/domains",
                          headers={"X-API-Key": env["sales_key"]})
    assert r.status_code == 200
    domains = r.json()["domains"]
    assert [d["name"] for d in domains] == ["sales"]
    assert domains[0]["description"]

    r2 = env["client"].get("/api/v1/insights/domains",
                           headers={"X-API-Key": env["all_key"]})
    assert {d["name"] for d in r2.json()["domains"]} == {"sales", "voc"}

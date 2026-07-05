"""
인사이트 API 테스트 (#56, Phase 1)

docs/issues/56/design.md 검증 시나리오 TC 1~12를 커버합니다.
LLM은 mock — 실제 OpenAI 호출 없음.
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
    """tmp DB로 store를 초기화하고 라우터만 붙인 테스트 앱 + 키 2개 발급."""
    db_path = tmp_path_factory.mktemp("insight") / "app_data.db"
    orig_db = settings.APP_DATA_DB_PATH
    settings.APP_DATA_DB_PATH = str(db_path)

    from company_llm_rag.insight_api import store
    store.init_insight_db()
    sales_client = store.create_client("테스트-매출", ["sales"])
    other_client = store.create_client("테스트-타도메인", ["other"])
    revoked = store.create_client("테스트-폐기", ["sales"])
    store.set_client_active(revoked["id"], False)

    from company_llm_rag.insight_api.router import router
    app = FastAPI()
    app.include_router(router)

    yield {
        "client": TestClient(app),
        "sales_key": sales_client["api_key"],
        "other_key": other_client["api_key"],
        "revoked_key": revoked["api_key"],
        "store": store,
    }
    settings.APP_DATA_DB_PATH = orig_db


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """기본: LLM이 정상 JSON을 반환하도록 mock."""
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


def _post(env, body, key=None):
    headers = {"X-API-Key": key} if key else {}
    return env["client"].post("/api/v1/insights/sales", json=body, headers=headers)


# ── TC 1~3, 12: 인증/인가 ───────────────────────────────────────────────────

def test_tc1_missing_key(env):
    assert _post(env, _sales_body()).status_code == 401


def test_tc2_invalid_and_revoked_key(env):
    assert _post(env, _sales_body(), key="mci_wrong").status_code == 401
    assert _post(env, _sales_body(), key=env["revoked_key"]).status_code == 401


def test_tc3_scope_forbidden(env):
    r = _post(env, _sales_body(), key=env["other_key"])
    assert r.status_code == 403


def test_tc12_ip_allowlist(env, monkeypatch):
    monkeypatch.setattr(settings, "API_ALLOWED_IPS", ["10.0.0.0/8"])
    r = _post(env, _sales_body(), key=env["sales_key"])
    assert r.status_code == 403


def test_unknown_domain_404(env):
    r = env["client"].post("/api/v1/insights/nope", json={},
                           headers={"X-API-Key": env["sales_key"]})
    assert r.status_code == 404


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
    r = _post(env, body, key=env["sales_key"])
    assert r.status_code == 422


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
    r = _post(env, _sales_body(), key=env["sales_key"])
    assert r.status_code == 200
    hist = env["store"].get_call_history(limit=1)
    item = hist["items"][0]
    assert item["status"] == 200
    assert item["domain"] == "sales"
    assert item["client_name"] == "테스트-매출"
    summary = json.loads(item["request_summary"])
    assert summary["rows"] == 2
    assert "records" not in summary                    # 원본 매출 데이터 미저장
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


# ── Phase 2: rate limit ────────────────────────────────────────────────────

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


# ── 부가: 도메인 목록 ───────────────────────────────────────────────────────

def test_list_domains_scoped(env):
    r = env["client"].get("/api/v1/insights/domains",
                          headers={"X-API-Key": env["sales_key"]})
    assert r.status_code == 200
    assert r.json()["domains"] == ["sales"]


# ── Phase 3: voc 도메인 (레지스트리 확장 검증) ─────────────────────────────

def _voc_body():
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
    return {"period": {"from": "2026-06-01", "to": "2026-06-30"}, "records": recs}


@pytest.fixture(scope="module")
def voc_key(env):
    return env["store"].create_client("테스트-VOC", ["voc"])["api_key"]


def test_voc_registered(env):
    from company_llm_rag.insight_api.domains import DOMAIN_REGISTRY
    assert set(DOMAIN_REGISTRY.keys()) >= {"sales", "voc"}


def test_voc_scope_isolation(env, voc_key):
    # sales 키로 voc 호출 → 403, voc 키로 sales 호출 → 403
    r = env["client"].post("/api/v1/insights/voc", json=_voc_body(),
                           headers={"X-API-Key": env["sales_key"]})
    assert r.status_code == 403
    assert _post(env, _sales_body(), key=voc_key).status_code == 403


def test_voc_stats_and_sample_privacy(env, voc_key):
    r = env["client"].post("/api/v1/insights/voc", json=_voc_body(),
                           headers={"X-API-Key": voc_key})
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


def test_voc_negative_priority_sampling(env, voc_key):
    from company_llm_rag.insight_api.domains.voc import VocDomain, VocInsightRequest
    req = VocInsightRequest.model_validate(_voc_body())
    stats = VocDomain().preprocess(req)
    # 샘플 선두는 부정 피드백 (최신순)
    assert stats["samples"][0]["rating"] <= 2
    assert stats["samples"][1]["rating"] <= 2

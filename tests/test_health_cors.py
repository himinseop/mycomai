"""/health CORS — devops 브라우저 호출 허용(대시보드 관리 목록), 다른 경로에는 미적용."""
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ORIGIN = "http://devops.wmpo.local:9090"
_fake_settings_db: Dict[str, str] = {}


@pytest.fixture(scope="module")
def ns():
    """web_app 모듈은 import 시 DB/정적파일 마운트가 필요하므로 CORS 관련 소스 구간만 격리 실행."""
    import importlib, inspect
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from company_llm_rag.config import settings
    src = inspect.getsource(importlib.import_module("company_llm_rag.web_app"))
    start = src.index("_HEALTH_CORS_SETTING_KEY = ")
    end = src.index("    return response\n", start) + len("    return response\n")
    app = FastAPI()
    ns = {"Request": Request, "JSONResponse": JSONResponse, "settings": settings, "app": app,
          "Optional": Optional, "List": List, "Dict": Dict, "json": json, "time": time,
          "logger": logging.getLogger("t"),
          "get_setting": lambda k, d="": _fake_settings_db.get(k, d)}
    exec(src[start:end], ns)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/other")
    async def other():
        return {"x": 1}

    from fastapi.testclient import TestClient
    ns["client"] = TestClient(app)
    return ns


@pytest.fixture
def client(ns, monkeypatch):
    _fake_settings_db.clear()
    ns["_health_cors_invalidate"]()
    monkeypatch.setattr(ns["settings"], "HEALTH_CORS_ORIGINS", ["*"])
    return ns["client"]


def _set_db(ns, entries):
    _fake_settings_db["health_cors_origins"] = json.dumps(entries)
    ns["_health_cors_invalidate"]()


def test_health_get_has_cors_header(client):
    r = client.get("/health", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_health_preflight_ok(client):
    r = client.options("/health", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "GET" in r.headers["access-control-allow-methods"]


def test_health_without_origin_no_header(client):
    r = client.get("/health")
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_other_path_not_opened(client):
    r = client.get("/other", headers={"Origin": ORIGIN})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_env_fallback_restricted_list(client, ns, monkeypatch):
    monkeypatch.setattr(ns["settings"], "HEALTH_CORS_ORIGINS", [ORIGIN])
    ns["_health_cors_invalidate"]()
    ok = client.get("/health", headers={"Origin": ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert ok.headers.get("vary") == "Origin"
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "http://evil.local"}).headers


def test_db_list_overrides_env(client, ns):
    # .env는 * 이지만 대시보드 목록이 있으면 그것만 허용
    _set_db(ns, ["devops.wmpo.co.kr"])
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": ORIGIN}).headers
    r = client.get("/health", headers={"Origin": "https://devops.wmpo.co.kr"})
    assert r.headers["access-control-allow-origin"] == "https://devops.wmpo.co.kr"


def test_db_empty_list_blocks_all(client, ns):
    _set_db(ns, [])
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": ORIGIN}).headers


def test_hostname_entries_match_any_scheme_and_port(client, ns):
    _set_db(ns, ["devops.wmpo.local", "test-devops.thecupping.co.kr", "devops.wmpo.co.kr"])
    for origin in ["http://devops.wmpo.local:9090", "https://test-devops.thecupping.co.kr",
                   "https://devops.wmpo.co.kr", "http://devops.wmpo.co.kr:8080"]:
        assert client.get("/health", headers={"Origin": origin}).headers.get("access-control-allow-origin") == origin, origin
    for origin in ["https://evil.devops.wmpo.co.kr", "https://devops.wmpo.co.kr.evil.com", "http://evil.local"]:
        assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": origin}).headers, origin


def test_host_port_entry_requires_port(client, ns):
    _set_db(ns, ["devops.wmpo.local:9090"])
    assert client.get("/health", headers={"Origin": ORIGIN}).headers.get("access-control-allow-origin") == ORIGIN
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "http://devops.wmpo.local:9091"}).headers


def test_cache_invalidation_applies_immediately(client, ns):
    _set_db(ns, ["a.local"])
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "http://b.local"}).headers
    _set_db(ns, ["b.local"])
    assert client.get("/health", headers={"Origin": "http://b.local"}).headers.get("access-control-allow-origin") == "http://b.local"


@pytest.mark.parametrize("raw,expected", [
    ("devops.wmpo.co.kr", "devops.wmpo.co.kr"),
    (" Devops.WMPO.co.kr ", "devops.wmpo.co.kr"),
    ("devops.wmpo.local:9090", "devops.wmpo.local:9090"),
    ("https://devops.wmpo.co.kr/", "https://devops.wmpo.co.kr"),
    ("*", "*"),
    ("", None),
    ("ftp://x.local", None),
    ("https://x.local/path", None),
    ("bad host", None),
    ("-bad.local", None),
    ("x.local:abc", None),
])
def test_normalize_cors_entry(ns, raw, expected):
    assert ns["_normalize_cors_entry"](raw) == expected

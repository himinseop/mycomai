"""
인사이트 API 인증 — 내부 전용 보안 장치

- X-API-Key 헤더 인증 (해시 매칭, 활성 클라이언트만)
- API_ALLOWED_IPS 설정 시 IP allowlist (CIDR 지원, 미설정이면 미적용)
- 도메인 scope 검사는 라우터에서 ensure_scope()로 수행
"""

import ipaddress
from typing import Dict, Optional

from fastapi import Header, HTTPException, Request

from company_llm_rag.config import settings
from company_llm_rag.insight_api.store import find_client_by_key
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def check_ip_allowed(request: Request) -> None:
    """API_ALLOWED_IPS가 설정된 경우 소스 IP를 검사합니다 (CIDR 지원)."""
    allowlist = settings.API_ALLOWED_IPS
    if not allowlist:
        return
    host = _client_ip(request)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 파싱 불가한 소스는 allowlist 활성 시 거부
        raise HTTPException(status_code=403, detail="source ip not allowed")
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return
        except ValueError:
            logger.warning(f"[InsightAPI] API_ALLOWED_IPS 항목 무시(형식 오류): {entry}")
    raise HTTPException(status_code=403, detail="source ip not allowed")


async def require_client(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict:
    """API Key를 검증하고 클라이언트를 반환합니다. 실패 시 401/403/404."""
    if not settings.INSIGHT_API_ENABLED:
        raise HTTPException(status_code=404, detail="insight api disabled")
    check_ip_allowed(request)
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing api key")
    client = find_client_by_key(x_api_key)
    if client is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return client


def ensure_scope(client: Dict, domain: str) -> None:
    """클라이언트가 해당 도메인 scope를 가졌는지 검사합니다."""
    if domain not in client.get("scopes", []):
        raise HTTPException(status_code=403, detail=f"domain '{domain}' not in scope")

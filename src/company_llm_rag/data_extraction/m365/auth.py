"""
Microsoft 365 공통 인증 모듈

MSAL 기반 Application 인증 및 Microsoft Graph API 호출 유틸리티.
sharepoint_extractor, teams_extractor 등에서 공유합니다.
"""

import time
from typing import Dict

import msal
import requests
from requests.exceptions import Timeout, ConnectionError as RequestsConnectionError

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger
from company_llm_rag.exceptions import AuthenticationError

logger = get_logger(__name__)


def get_msal_app() -> msal.ConfidentialClientApplication:
    """MSAL 애플리케이션 인스턴스를 생성합니다."""
    authority = f"https://login.microsoftonline.com/{settings.TENANT_ID}"
    return msal.ConfidentialClientApplication(
        settings.CLIENT_ID,
        authority=authority,
        client_credential=settings.CLIENT_SECRET,
    )


def get_access_token() -> str:
    """
    Microsoft Graph API용 액세스 토큰을 획득합니다.

    Returns:
        액세스 토큰

    Raises:
        Exception: 토큰 획득 실패 시
    """
    app = get_msal_app()
    scope = ["https://graph.microsoft.com/.default"]
    result = app.acquire_token_for_client(scopes=scope)

    if "access_token" in result and result["access_token"]:
        return result["access_token"]

    logger.error(f"MSAL acquire_token_for_client result: {result}")
    error_msg = (
        result.get("error_description")
        or result.get("error")
        or "Access token is empty or could not be acquired."
    )
    raise AuthenticationError(f"Could not acquire access token: {error_msg}")


def call_graph_api(endpoint: str, access_token: str, max_retries: int = 3) -> Dict:
    """
    Microsoft Graph API에 GET 요청을 보냅니다.
    429 Rate Limit 발생 시 Retry-After 헤더에 따라 재시도합니다.

    Args:
        endpoint: API 엔드포인트 URL
        access_token: 액세스 토큰
        max_retries: 최대 재시도 횟수

    Returns:
        API 응답 (JSON)

    Raises:
        Exception: 최대 재시도 초과 시
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    for attempt in range(max_retries):
        try:
            response = requests.get(endpoint, headers=headers, timeout=60)
        except (Timeout, RequestsConnectionError) as e:
            wait = 5 * (attempt + 1)
            logger.warning(
                f"네트워크 오류 ({type(e).__name__}), {wait}s 후 재시도 "
                f"(attempt {attempt + 1}/{max_retries}): {endpoint}"
            )
            if attempt + 1 < max_retries:
                time.sleep(wait)
                continue
            raise

        if response.status_code == 429:
            retry_after = max(int(response.headers.get("Retry-After", 10)), 10)
            logger.warning(
                f"Rate limited. Retrying after {retry_after}s "
                f"(attempt {attempt + 1}/{max_retries})..."
            )
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        return response.json()

    raise Exception(f"Max retries exceeded for endpoint: {endpoint}")

"""
Teams 채널 메시지 전송 모듈

RAG 답변이 부족할 때 지정된 Teams 채널에 문의 메시지를 남깁니다.
해당 채널의 대화는 추후 임베딩을 통해 지식베이스에 재학습됩니다.

필요한 Azure AD 권한:
- ChannelMessage.Send (Application)
"""

import requests
import msal

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _get_access_token() -> str:
    authority = f"https://login.microsoftonline.com/{settings.TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        settings.CLIENT_ID,
        authority=authority,
        client_credential=settings.CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"토큰 획득 실패: {result.get('error_description')}")
    return result["access_token"]


def send_inquiry_to_teams(question: str, rag_answer: str) -> bool:
    """
    지정된 Teams 채널에 문의 메시지를 전송합니다.

    Args:
        question: 사용자 질문
        rag_answer: RAG 시스템의 답변 (없으면 빈 문자열)

    Returns:
        전송 성공 여부
    """
    if not settings.TEAMS_INQUIRY_TEAM_ID or not settings.TEAMS_INQUIRY_CHANNEL_ID:
        logger.warning("TEAMS_INQUIRY_TEAM_ID 또는 TEAMS_INQUIRY_CHANNEL_ID가 설정되지 않았습니다.")
        return False

    try:
        token = _get_access_token()
    except Exception as e:
        logger.error(f"Teams 토큰 획득 실패: {e}")
        return False

    answer_section = (
        f"<br><b>🤖 AI 답변:</b><br>{rag_answer.replace(chr(10), '<br>')}"
        if rag_answer and rag_answer != "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
        else "<br><b>🤖 AI 답변:</b> 관련 정보를 찾지 못했습니다."
    )

    body_content = (
        f"<b>📌 AI 검색 문의가 접수되었습니다.</b><br><br>"
        f"<b>❓ 질문:</b><br>{question.replace(chr(10), '<br>')}"
        f"{answer_section}<br><br>"
        f"<i>위 질문에 대해 아시는 분은 답변 부탁드립니다. "
        f"이 채널의 대화는 AI 학습에 활용됩니다.</i>"
    )

    url = (
        f"https://graph.microsoft.com/v1.0"
        f"/teams/{settings.TEAMS_INQUIRY_TEAM_ID}"
        f"/channels/{settings.TEAMS_INQUIRY_CHANNEL_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "body": {
            "contentType": "html",
            "content": body_content,
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Teams 문의 전송 완료: {response.json().get('id')}")
        return True
    except Exception as e:
        logger.error(f"Teams 메시지 전송 실패: {e}")
        return False


def is_inquiry_configured() -> bool:
    """Teams 문의 채널이 설정되어 있는지 확인합니다."""
    return bool(settings.TEAMS_INQUIRY_TEAM_ID and settings.TEAMS_INQUIRY_CHANNEL_ID)

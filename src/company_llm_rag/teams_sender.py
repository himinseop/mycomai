"""
Teams 채널 메시지 전송 모듈

RAG 답변이 부족할 때 지정된 Teams 채널에 문의 메시지를 남깁니다.
해당 채널의 대화는 추후 임베딩을 통해 지식베이스에 재학습됩니다.

Incoming Webhook 방식 사용 (Azure AD Application 권한 불필요)
설정 방법:
  Teams 채널 → ... → 커넥터 → Incoming Webhook → 구성 → URL 복사
  → TEAMS_INQUIRY_WEBHOOK_URL 환경변수에 설정
"""

from typing import Dict, List

import requests

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _format_conversation(conversation_history: List[Dict]) -> str:
    """대화 히스토리를 텍스트로 포맷합니다."""
    lines = []
    for msg in conversation_history:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"AI: {content}")
    return "\n\n".join(lines)


def send_inquiry_to_teams(question: str, conversation_history: List[Dict]) -> bool:
    """
    지정된 Teams 채널에 문의 메시지를 전송합니다.

    Args:
        question: 사용자의 최초(현재) 질문
        conversation_history: 세션 대화 히스토리 (현재 Q&A 포함)

    Returns:
        전송 성공 여부
    """
    if not settings.TEAMS_INQUIRY_WEBHOOK_URL:
        logger.warning("TEAMS_INQUIRY_WEBHOOK_URL이 설정되지 않았습니다.")
        return False

    conversation_text = _format_conversation(conversation_history)

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"Q : {question}",
                            "weight": "Bolder",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": conversation_text,
                            "wrap": True,
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": "위 질문에 대한 내용이나 보충설명은 답변 부탁드립니다. 이 채널의 대화는 AI 학습에 활용됩니다.",
                            "wrap": True,
                            "isSubtle": True,
                            "spacing": "Medium",
                        },
                    ],
                },
            }
        ],
    }

    try:
        response = requests.post(
            settings.TEAMS_INQUIRY_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Teams 문의 전송 완료 (Incoming Webhook)")
        return True
    except Exception as e:
        logger.error(f"Teams 메시지 전송 실패: {e}")
        return False


def is_inquiry_configured() -> bool:
    """Teams 문의 채널이 설정되어 있는지 확인합니다."""
    return bool(settings.TEAMS_INQUIRY_WEBHOOK_URL)

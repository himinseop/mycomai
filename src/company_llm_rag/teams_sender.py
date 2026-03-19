"""
Teams 채널 메시지 전송 모듈

RAG 답변이 부족할 때 지정된 Teams 채널에 문의 메시지를 남깁니다.
해당 채널의 대화는 추후 임베딩을 통해 지식베이스에 재학습됩니다.

Incoming Webhook 방식 사용 (Azure AD Application 권한 불필요)
설정 방법:
  Teams 채널 → ... → 커넥터 → Incoming Webhook → 구성 → URL 복사
  → TEAMS_INQUIRY_WEBHOOK_URL 환경변수에 설정
"""

import requests

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def send_inquiry_to_teams(question: str, rag_answer: str) -> bool:
    """
    지정된 Teams 채널에 문의 메시지를 전송합니다.

    Args:
        question: 사용자 질문
        rag_answer: RAG 시스템의 답변 (없으면 빈 문자열)

    Returns:
        전송 성공 여부
    """
    if not settings.TEAMS_INQUIRY_WEBHOOK_URL:
        logger.warning("TEAMS_INQUIRY_WEBHOOK_URL이 설정되지 않았습니다.")
        return False

    has_answer = (
        rag_answer
        and rag_answer != "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
    )
    answer_text = rag_answer if has_answer else "관련 정보를 찾지 못했습니다."

    # Adaptive Card 형식으로 메시지 구성
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
                            "text": "📌 AI 검색 문의가 접수되었습니다.",
                            "weight": "Bolder",
                            "size": "Medium",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": "❓ 질문",
                            "weight": "Bolder",
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": question,
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": "🤖 AI 답변",
                            "weight": "Bolder",
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": answer_text,
                            "wrap": True,
                            "color": "Default",
                        },
                        {
                            "type": "TextBlock",
                            "text": "위 질문에 대해 아시는 분은 답변 부탁드립니다. 이 채널의 대화는 AI 학습에 활용됩니다.",
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

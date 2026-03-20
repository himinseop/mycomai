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
from company_llm_rag.llm.openai_provider import OpenAIProvider

logger = get_logger(__name__)

# 요약용 경량 모델 사용
_summarizer_llm = OpenAIProvider(default_model="gpt-4o-mini", default_temperature=0.3)


def _summarize_conversation(question: str, conversation_history: List[Dict]) -> str:
    """대화 내용을 3줄 이내로 요약합니다."""
    if not conversation_history:
        return ""

    history_text = "\n".join(
        f"{'사용자' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in conversation_history
    )

    try:
        return _summarizer_llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "아래 대화를 보고, 사용자가 무엇을 궁금해했는지 핵심만 3줄 이내로 요약하세요. "
                        "개인 정보나 민감한 내용은 제외하고, 질문의 맥락과 추가로 궁금해했던 내용을 중심으로 작성하세요. "
                        "번호나 불릿 없이 자연스러운 문장으로 작성하세요."
                    ),
                },
                {"role": "user", "content": history_text},
            ],
            max_tokens=300,
        ).strip()
    except Exception as e:
        logger.warning(f"대화 요약 실패, 생략: {e}")
        return ""


def send_inquiry_to_teams(question: str, conversation_history: List[Dict]) -> bool:
    """
    지정된 Teams 채널에 문의 메시지를 전송합니다.

    Args:
        question: 사용자의 현재 질문 (히스토리가 없을 때 fallback)
        conversation_history: 세션 대화 히스토리 (현재 Q&A 포함)

    Returns:
        전송 성공 여부
    """
    if not settings.TEAMS_INQUIRY_WEBHOOK_URL:
        logger.warning("TEAMS_INQUIRY_WEBHOOK_URL이 설정되지 않았습니다.")
        return False

    # 대화의 가장 첫 번째 질문을 사용
    first_question = next(
        (m["content"] for m in conversation_history if m["role"] == "user"),
        question,
    )

    summary = _summarize_conversation(first_question, conversation_history)

    body_blocks = [
        {
            "type": "TextBlock",
            "text": f"❓ Q : {first_question}",
            "weight": "Bolder",
            "wrap": True,
        },
    ]

    if summary:
        body_blocks.append({
            "type": "TextBlock",
            "text": f"📝 {summary}",
            "wrap": True,
            "spacing": "Medium",
        })

    body_blocks.append({
        "type": "TextBlock",
        "text": "💬 위 질문에 대한 내용이나 보충설명은 답변 부탁드립니다. 이 채널의 대화는 AI 학습에 활용됩니다.",
        "wrap": True,
        "isSubtle": True,
        "spacing": "Medium",
    })

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body_blocks,
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

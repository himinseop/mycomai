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
_summarizer_llm = OpenAIProvider(default_model=settings.OPENAI_SUMMARIZE_MODEL, default_temperature=0.3)


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

    ai_name = settings.AI_NAME or "오사장AI"

    body_blocks = [
        # ── 헤더 ──────────────────────────────────────────────────
        {
            "type": "TextBlock",
            "text": f"💬 [{ai_name}] 답변을 찾지 못한 질문입니다",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "아래 질문에 대해 아는 내용이 있다면 **이 메시지에 답글**로 남겨주세요.\n답글은 AI 지식베이스 학습에 자동 반영됩니다.",
            "wrap": True,
            "isSubtle": True,
            "spacing": "Small",
        },
        {"type": "Separator"},
        # ── [질문] ────────────────────────────────────────────────
        {
            "type": "TextBlock",
            "text": "**[질문]**",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": first_question,
            "wrap": True,
            "spacing": "Small",
        },
    ]

    if summary:
        body_blocks += [
            {
                "type": "TextBlock",
                "text": "**[대화 맥락 요약]**",
                "weight": "Bolder",
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": summary,
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
            },
        ]

    body_blocks += [
        {"type": "Separator"},
        {
            "type": "TextBlock",
            "text": "**[답변]** ← 답글로 작성해 주세요",
            "weight": "Bolder",
            "color": "Good",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": "정확한 정보, 참고 문서 링크, 담당자 안내 등 어떤 내용이든 환영합니다.",
            "wrap": True,
            "isSubtle": True,
            "spacing": "Small",
        },
    ]

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


def send_feedback_alert_to_teams(question: str, answer: str) -> bool:
    """
    👎 피드백 수신 시 Teams 채널에 알림을 전송합니다.
    메시지는 Knowledge Hub 수집 시 명확한 Q&A 레코드로 활용될 수 있도록 구조화됩니다.

    메시지 구조 (AI 학습용):
      [유형] 불만족 피드백
      [질문] 사용자 질문
      [AI 답변] AI가 생성한 답변
      [올바른 답변] ← 팀원이 이 메시지에 답글로 작성하는 영역

    Args:
        question: 사용자 질문
        answer:   AI 답변

    Returns:
        전송 성공 여부
    """
    if not settings.TEAMS_INQUIRY_WEBHOOK_URL:
        return False

    answer_text = answer[:500] + "…" if len(answer) > 500 else answer
    ai_name = settings.AI_NAME or "오사장AI"

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
                        # ── 헤더 ──────────────────────────────────────
                        {
                            "type": "TextBlock",
                            "text": f"👎 [{ai_name}] 불만족 피드백 접수",
                            "weight": "Bolder",
                            "color": "Attention",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": "아래 내용을 확인하고, 더 정확한 답변을 **이 메시지에 답글**로 남겨주세요.\n답글은 AI 지식베이스 학습에 자동 반영됩니다.",
                            "wrap": True,
                            "isSubtle": True,
                            "spacing": "Small",
                        },
                        # ── 구분선 ────────────────────────────────────
                        {"type": "Separator"},
                        # ── [질문] ────────────────────────────────────
                        {
                            "type": "TextBlock",
                            "text": "**[질문]**",
                            "weight": "Bolder",
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": question,
                            "wrap": True,
                            "spacing": "Small",
                        },
                        # ── [AI 답변] ─────────────────────────────────
                        {
                            "type": "TextBlock",
                            "text": f"**[{ai_name} 답변]**",
                            "weight": "Bolder",
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": answer_text,
                            "wrap": True,
                            "isSubtle": True,
                            "spacing": "Small",
                        },
                        # ── [올바른 답변] 안내 ─────────────────────────
                        {"type": "Separator"},
                        {
                            "type": "TextBlock",
                            "text": "**[올바른 답변]** ← 답글로 작성해 주세요",
                            "weight": "Bolder",
                            "color": "Good",
                            "spacing": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": "정확한 정보, 참고 문서 링크, 담당자 안내 등 어떤 내용이든 환영합니다.",
                            "wrap": True,
                            "isSubtle": True,
                            "spacing": "Small",
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
        logger.info("Teams 👎 피드백 알림 전송 완료")
        return True
    except Exception as e:
        logger.error(f"Teams 피드백 알림 전송 실패: {e}")
        return False

"""
인사이트 도메인 추상 클래스

도메인 = 입력 스키마 + 전처리(결정적 통계 선계산) + 프롬프트 + 응답 파싱.
수치는 preprocess()가 Python으로 계산하고, LLM은 해석만 담당합니다
(수치 할루시네이션 차단 — 설계 원칙, docs/issues/56/design.md).
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Type

from pydantic import BaseModel

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "insights"


class InsightDomain(ABC):
    """도메인별 인사이트 파이프라인 정의."""

    name: str
    request_model: Type[BaseModel]
    # 도메인 자동 선택용: 이 필드들을 가진 records면 이 도메인일 가능성이 높음
    signature_fields: set = set()
    # 도메인 자동 선택(LLM 분류)·/domains 응답에 쓰이는 한 줄 설명
    description: str = ""

    @abstractmethod
    def preprocess(self, req: BaseModel) -> Dict:
        """결정적 통계를 선계산합니다. 입력 의미 오류는 ValueError(→422)."""

    @abstractmethod
    def build_messages(self, req: BaseModel, stats: Dict,
                       question: str = "") -> List[Dict[str, str]]:
        """선계산된 통계(+사용자 질문)로 LLM messages를 구성합니다."""

    @abstractmethod
    def parse_response(self, raw: str) -> Dict:
        """LLM 출력 → {"summary", "highlights", "anomalies"} 구조로 파싱합니다."""

    def load_prompt(self) -> str:
        """prompts/insights/<name>.txt 프롬프트를 로드합니다."""
        path = _PROMPTS_DIR / f"{self.name}.txt"
        return path.read_text(encoding="utf-8")

    def postprocess_stats(self, stats: Dict) -> Dict:
        """응답에 노출할 stats 가공 (기본: 그대로). LLM 전용 필드 제거 등에 사용."""
        return stats

    def request_summary(self, req: BaseModel, stats: Dict) -> Dict:
        """이력에 남길 요청 요약 (원본 데이터 제외). 도메인별 오버라이드 가능."""
        return {}


def parse_llm_json(raw: str) -> Dict:
    """LLM 출력에서 JSON 객체를 관대하게 파싱합니다 (코드펜스 허용)."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return {}

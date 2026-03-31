"""
Reranker 추상 인터페이스

구현체 교체 시 이 계약만 따르면 retrieval_module.py 변경 없이 동작합니다.
"""

from abc import ABC, abstractmethod
from typing import Dict, List


class RerankerProvider(ABC):
    """Reranker 추상 인터페이스."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """현재 사용 중인 모델명."""

    @abstractmethod
    def rerank(self, query: str, docs: List[Dict], top_n: int) -> List[Dict]:
        """질문-문서 쌍의 관련도를 계산하여 재정렬합니다.

        Args:
            query: 사용자 질문
            docs: 검색 결과 리스트 (content, metadata 포함)
            top_n: 반환할 상위 문서 수

        Returns:
            재정렬된 상위 top_n개 문서 (_rerank_score 필드 추가됨)
        """

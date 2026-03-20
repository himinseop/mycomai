"""
BaseExtractor 추상 클래스

모든 데이터 소스 Extractor의 공통 기반 클래스.
날짜 포맷팅, 프로그레스 로깅 등 공통 유틸리티를 제공합니다.
"""

import time
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Iterator, Dict

from company_llm_rag.logger import get_logger


class BaseExtractor(ABC):
    """데이터 소스 Extractor 기본 클래스."""

    # 서브클래스에서 override 가능
    PROGRESS_EVERY: int = 50

    def __init__(self) -> None:
        self.logger = get_logger(self.__class__.__name__)
        self._start_time: float = 0.0

    @abstractmethod
    def extract(self) -> Iterator[Dict]:
        """
        데이터 소스에서 아이템을 추출하여 표준 스키마 dict로 yield합니다.

        Yields:
            표준 문서 스키마 dict
        """

    def log_progress(self, current: int, total: int, label: str = "") -> None:
        """
        진행률을 로그에 출력합니다.

        Args:
            current: 현재 처리 건수
            total:   전체 건수 (0이면 퍼센트 미표시)
            label:   출력할 추가 레이블 문자열
        """
        elapsed = time.time() - self._start_time if self._start_time else 0
        if total > 0:
            pct = int(current / total * 100)
            self.logger.info(
                f"{label} {current}/{total} ({pct}%) | 경과: {self.fmt_elapsed(elapsed)}"
            )
        else:
            self.logger.info(
                f"{label} {current} | 경과: {self.fmt_elapsed(elapsed)}"
            )

    def start_timer(self) -> None:
        """진행률 타이머를 시작합니다."""
        self._start_time = time.time()

    @staticmethod
    def fmt_elapsed(seconds: float) -> str:
        """경과 시간을 'H:MM:SS' 형식으로 변환합니다."""
        return str(timedelta(seconds=int(seconds)))

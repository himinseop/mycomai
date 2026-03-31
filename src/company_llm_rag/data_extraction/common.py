"""
데이터 추출기 공통 유틸리티

모든 extractor가 공유하는 진행률 로깅, 시간 포맷 등 헬퍼.
"""

import json
import time
from datetime import timedelta
from typing import Dict

from company_llm_rag.logger import get_logger


def fmt_elapsed(seconds: float) -> str:
    """경과 시간을 H:MM:SS 형식으로 포맷합니다."""
    return str(timedelta(seconds=int(seconds)))


def log_progress(
    logger,
    source: str,
    group: str,
    current: int,
    total: int,
    start_time: float,
    every: int = 50,
) -> None:
    """진행률 로그를 출력합니다.

    Args:
        source: 소스명 (Jira, Confluence, Teams 등)
        group: 그룹명 (프로젝트, 스페이스, 채널 등)
        current: 현재 처리 수
        total: 전체 수
        start_time: time.time() 시작 시각
        every: 로그 출력 간격
    """
    if current % every == 0 or current == total:
        pct = int(current / total * 100) if total else 0
        elapsed = fmt_elapsed(time.time() - start_time)
        logger.info(f"[{source}][{group}] {current}/{total} ({pct}%) | 경과: {elapsed}")


def emit_document(doc: Dict) -> None:
    """표준 문서 스키마를 JSONL로 stdout에 출력합니다."""
    print(json.dumps(doc, ensure_ascii=False))

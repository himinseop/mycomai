"""
로깅 설정 모듈

애플리케이션 전역에서 사용할 구조화된 로거를 제공합니다.
"""

import logging
import sys
from typing import Optional

from company_llm_rag.config import settings


class ColoredFormatter(logging.Formatter):
    """컬러 출력을 지원하는 로그 포매터"""

    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'        # Reset
    }

    def format(self, record):
        """로그 레코드를 포맷팅합니다."""
        # 터미널에서만 컬러 적용
        if sys.stderr.isatty():
            color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
            record.levelname = f"{color}{record.levelname}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logger(
    name: str,
    level: Optional[str] = None,
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    로거를 설정합니다.

    Args:
        name: 로거 이름 (보통 모듈 이름)
        level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 로그 파일 경로 (선택사항)

    Returns:
        설정된 Logger 객체
    """
    logger = logging.getLogger(name)

    # 이미 핸들러가 설정되어 있으면 중복 설정 방지
    if logger.handlers:
        return logger

    # 로그 레벨 설정
    if level is None:
        level = getattr(settings, 'LOG_LEVEL', 'INFO')

    logger.setLevel(getattr(logging, level.upper()))

    # 콘솔 핸들러 설정
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)

    # 포맷 설정
    console_format = ColoredFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # 파일 핸들러 설정 (선택사항)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    로거를 가져옵니다.

    Args:
        name: 로거 이름 (보통 __name__ 사용)

    Returns:
        Logger 객체
    """
    return setup_logger(name)


# 애플리케이션 기본 로거
app_logger = get_logger("mycomai")

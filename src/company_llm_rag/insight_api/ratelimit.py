"""
인사이트 API rate limit (Phase 2, #56)

키(클라이언트)별 분당 호출 수를 in-memory sliding window로 제한합니다.
단일 프로세스(uvicorn 워커 1개) 기준 — 워커 확장 시 저장소 공유 필요.
한도: 클라이언트별 rate_limit_per_min, 없으면 INSIGHT_RATE_LIMIT_PER_MIN.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from company_llm_rag.config import settings

_WINDOW_SECONDS = 60

_lock = threading.Lock()
_windows: Dict[int, Deque[float]] = defaultdict(deque)


def check_rate_limit(client: Dict) -> bool:
    """호출을 허용하면 True(카운트 증가), 한도 초과면 False."""
    limit = client.get("rate_limit_per_min") or settings.INSIGHT_RATE_LIMIT_PER_MIN
    if limit <= 0:  # 0 이하 = 무제한
        return True
    now = time.monotonic()
    with _lock:
        window = _windows[client["id"]]
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


def reset(client_id: int = None) -> None:
    """윈도 초기화 (테스트/키 재발급용)."""
    with _lock:
        if client_id is None:
            _windows.clear()
        else:
            _windows.pop(client_id, None)

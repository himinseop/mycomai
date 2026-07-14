"""
위키 신선도 관리 (#58 Phase 2)

수집 파이프라인 종료 후 호출: 각 페이지의 소스 문서를 재검색해 source_hash가
바뀐 페이지만 재생성합니다. 재생성물은 draft로 강등(재검수) + Teams 알림.
"""

from typing import Dict

from company_llm_rag.wiki import wiki_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _notify_teams(text: str) -> None:
    """Knowledge Hub Incoming Webhook으로 간단 알림 (미설정 시 무시)."""
    from company_llm_rag.config import settings
    if not settings.KNOWLEDGE_HUB_WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(settings.KNOWLEDGE_HUB_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning(f"[Wiki] Teams 알림 실패: {e}")


def refresh_stale_pages(notify: bool = True) -> Dict:
    """소스 변경된 페이지를 재생성합니다. 반환: 요약 dict."""
    from company_llm_rag.wiki.page_builder import build_page, collect_sources

    pages = [p for p in wiki_store.list_pages()
             if p["status"] != wiki_store.STATUS_DISABLED]
    rebuilt, unchanged, failed = [], [], []

    for p in pages:
        try:
            docs = collect_sources(p["questions"])
            hashes = [d.get("metadata", {}).get("content_hash", "") for d in docs]
            new_hash = wiki_store.compute_source_hash(hashes)
            if new_hash == p["source_hash"]:
                unchanged.append(p["topic"])
                continue
            was_approved = p["status"] == wiki_store.STATUS_APPROVED
            build_page(p["topic"], p["title"], p["questions"])  # draft로 저장됨
            rebuilt.append({"topic": p["topic"], "title": p["title"],
                            "was_approved": was_approved})
            logger.info(f"[Wiki] 소스 변경 감지 → 재생성: {p['topic']}"
                        f"{' (승인 페이지 — draft 강등)' if was_approved else ''}")
        except Exception as e:
            failed.append({"topic": p["topic"], "error": str(e)[:200]})
            logger.error(f"[Wiki] 재생성 실패: {p['topic']} — {e}")

    summary = {"rebuilt": rebuilt, "unchanged": unchanged, "failed": failed}
    if rebuilt and notify:
        lines = [f"📖 위키 자동 갱신: {len(rebuilt)}건 재생성 — 재검수 필요"]
        for r in rebuilt:
            lines.append(f"· {r['title']}" + (" (승인 해제됨)" if r["was_approved"] else ""))
        lines.append("관리자 → 위키 탭에서 검수 후 승인해 주세요.")
        _notify_teams("\n".join(lines))
    logger.info(f"[Wiki] 신선도 점검 완료: 재생성 {len(rebuilt)} / 유지 {len(unchanged)} / 실패 {len(failed)}")
    return summary

"""
출처 기반 참고문서 (#61 §12, 2026-09-02 확정)

매뉴얼 근거 답변에서 참고문서를 검색 결과(키워드 유사성) 그대로 노출하면
매뉴얼과 무관한 지라/컨플/쉐어포인트가 딸려온다. 이 모듈은 대신 매뉴얼
저자가 해당 구문 옆에 남긴 출처(다이제스트 인라인 링크·이슈키)만 근거로
추출·검증해 참고문서로 제공한다. 전부 app_data.db 그래프 조회로만 동작하며
LLM 호출·추가 지연이 없다.
"""

import json
import posixpath
import re
from typing import Dict, List, Set

from company_llm_rag.config import settings
from company_llm_rag.graph import graph_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

# 매뉴얼 본문 마크다운 링크 중 다이제스트를 가리키는 것만 (href에 sharepoint-index/digests/ 포함)
_DIGEST_LINK_RE = re.compile(r'\[[^\]]*\]\(([^)]*sharepoint-index/digests/[^)]+)\)')

# 이슈키 후보 (기능 ID 'ECP-C-01' 류 오탐은 그래프 실존 검증에서 걸러짐)
_ISSUE_KEY_RE = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')

# 다이제스트 관련 일감 역할 우선순위 (#61 §12: 구현 우선, 부족하면 후속·원인)
_DIGEST_ISSUE_ROLE_PRIORITY = {"구현": 0, "후속": 1, "원인": 2, "미구현": 3}
_MAX_DIGEST_ISSUES_PER_DIGEST = 2


def _normalize_digest_href(manual_relpath: str, href: str) -> str:
    """다이제스트 링크 href를 매뉴얼 docs_relpath 기준 저장소 루트 상대경로로 정규화합니다.

    예: manual_relpath='platform/features/coupon.md',
        href='../sharepoint-index/digests/2023-02_voucher.md'
        → 'platform/sharepoint-index/digests/2023-02_voucher.md'

    URL 인코딩·타이포그래피 문자(’ 등)는 디코딩하지 않고 그대로 보존한다.
    """
    href = (href or "").strip()
    if not manual_relpath or not href:
        return ""
    base_dir = posixpath.dirname(manual_relpath)
    return posixpath.normpath(posixpath.join(base_dir, href))


def extract_from_chunks(manual_chunks: List[Dict]) -> Dict[str, List[str]]:
    """컨텍스트에 포함된 매뉴얼 청크(source=docs, docs_category≠digest) 본문에서
    다이제스트 링크·이슈키 후보를 추출합니다 (등장 순서 유지, dedup).

    Returns:
        {"digest_relpaths": [...], "issue_keys": [...]}
    """
    digest_relpaths: List[str] = []
    seen_digests: Set[str] = set()
    issue_keys: List[str] = []
    seen_keys: Set[str] = set()

    for doc in manual_chunks:
        meta = doc.get("metadata", {}) or {}
        content = doc.get("content", "") or ""
        manual_relpath = meta.get("docs_relpath", "") or ""

        for href in _DIGEST_LINK_RE.findall(content):
            relpath = _normalize_digest_href(manual_relpath, href)
            if relpath and relpath not in seen_digests:
                seen_digests.add(relpath)
                digest_relpaths.append(relpath)

        for key in _ISSUE_KEY_RE.findall(content):
            if key not in seen_keys:
                seen_keys.add(key)
                issue_keys.append(key)

    return {"digest_relpaths": digest_relpaths, "issue_keys": issue_keys}


def _digest_reference(node_id: str, node: Dict) -> Dict:
    """다이제스트 doc 노드 → 참고문서 dict (기존 digest ref 형식, source='docs')."""
    meta = node.get("meta", {}) or {}
    doc_id = node_id[len("doc:"):] if node_id.startswith("doc:") else node_id
    return {
        "title": node.get("label", ""),
        "url": meta.get("url", "") or "",
        "source": "docs",
        "content_type": "",
        "doc_id": doc_id,
        "issue_key": "",
        "project_key": "",
        "space_name": "",
        "space_key": "",
        "ancestors": "",
        "site_name": "",
        "file_path": "",
        "team_name": "",
        "channel_name": "",
        "chat_topic": "",
        "author": "",
        "created_at": meta.get("digest_date", "") or "",
        "snippet": "",
        "page_nums": [],
        "hub_reply": "",
        "docs_category": "digest",
    }


def _issue_reference(key: str, node: Dict) -> Dict:
    """이슈 노드 → 참고문서 dict (source='jira'). node가 없으면 호출하지 않는다."""
    meta = node.get("meta", {}) or {}
    url = meta.get("url", "") or (
        f"{settings.JIRA_BASE_URL}/browse/{key}" if settings.JIRA_BASE_URL else ""
    )
    return {
        "title": node.get("label", "") or key,
        "url": url,
        "source": "jira",
        "content_type": "",
        "doc_id": "",
        "issue_key": key,
        "project_key": meta.get("project") or (key.split("-")[0] if "-" in key else ""),
        "space_name": "",
        "space_key": "",
        "ancestors": "",
        "site_name": "",
        "file_path": "",
        "team_name": "",
        "channel_name": "",
        "chat_topic": "",
        "author": "",
        "created_at": (meta.get("created_at") or "")[:10],
        "snippet": "",
        "page_nums": [],
        "hub_reply": "",
    }


def build_provenance_references(
    digest_relpaths: List[str], issue_keys: List[str], max_refs: int,
) -> List[Dict]:
    """그래프 조회로 출처 기반 참고문서 dict 목록을 만듭니다 (#61 §12 우선순위 1→3).

    1. 다이제스트 링크 → doc 노드(title·url) → 기존 digest ref 형식
    2. 그 다이제스트의 관련 일감(digest_issues) — 구현 우선, 다이제스트당 최대 2
    3. 매뉴얼에 직접 인용된 이슈키 — 그래프에 실존하는 이슈만
    노드가 없으면 조용히 스킵(debug 로그). URL·이슈키 dedup, max_refs 상한.
    """
    references: List[Dict] = []
    if max_refs <= 0:
        return references

    seen_urls: Set[str] = set()
    seen_issue_keys: Set[str] = set()

    digest_node_ids = [f"doc:docs-{p}" for p in digest_relpaths]
    digest_nodes = graph_store.get_nodes(digest_node_ids)

    # 다이제스트별 관련 일감 후보(역할 우선순위 정렬) 및 조회할 이슈키 전체를 먼저 모은다
    digest_issue_candidates: Dict[str, List[str]] = {}
    all_issue_keys: Set[str] = set(issue_keys)
    for node_id, node in digest_nodes.items():
        try:
            issues = json.loads((node.get("meta") or {}).get("digest_issues") or "[]")
        except (json.JSONDecodeError, TypeError):
            issues = []
        ranked = sorted(
            (e for e in issues if isinstance(e, dict) and (e.get("key") or "").strip()),
            key=lambda e: _DIGEST_ISSUE_ROLE_PRIORITY.get(e.get("role", ""), 9),
        )
        keys = [e["key"].strip() for e in ranked[:_MAX_DIGEST_ISSUES_PER_DIGEST]]
        digest_issue_candidates[node_id] = keys
        all_issue_keys.update(keys)

    issue_nodes = graph_store.get_nodes([f"issue:{k}" for k in all_issue_keys]) if all_issue_keys else {}

    def _add_issue_ref(key: str) -> bool:
        if len(references) >= max_refs or key in seen_issue_keys:
            return False
        node = issue_nodes.get(f"issue:{key}")
        if not node:
            logger.debug(f"[Provenance] 이슈 노드 없음(오탐 배제), 스킵: {key}")
            return False
        ref = _issue_reference(key, node)
        url = ref.get("url", "")
        if url and url in seen_urls:
            seen_issue_keys.add(key)
            return False
        seen_issue_keys.add(key)
        if url:
            seen_urls.add(url)
        references.append(ref)
        return True

    # 1) + 2) 다이제스트 링크 → doc 참고문서 → 관련 일감
    for relpath, node_id in zip(digest_relpaths, digest_node_ids):
        if len(references) >= max_refs:
            break
        node = digest_nodes.get(node_id)
        if not node:
            logger.debug(f"[Provenance] 다이제스트 노드 없음, 스킵: {node_id}")
            continue
        url = (node.get("meta") or {}).get("url", "") or ""
        if not url:
            logger.debug(f"[Provenance] 다이제스트 url 없음, 스킵: {node_id}")
        elif url not in seen_urls:
            seen_urls.add(url)
            references.append(_digest_reference(node_id, node))

        for key in digest_issue_candidates.get(node_id, []):
            if len(references) >= max_refs:
                break
            _add_issue_ref(key)

    # 3) 매뉴얼에 직접 인용된 이슈키
    for key in issue_keys:
        if len(references) >= max_refs:
            break
        _add_issue_ref(key)

    return references[:max_refs]

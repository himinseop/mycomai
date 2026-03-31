"""
인용 치환, 문서 표시명, 소스 라벨, Teams URL 빌더

rag_system.py에서 분리된 표시/링크 관련 유틸리티.
"""

import json
import re
from typing import Dict, List, Tuple
from urllib.parse import quote

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def ensure_list(value) -> list:
    """메타데이터 값을 list로 변환합니다 (JSON 직렬화된 str 포함)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            logger.debug(f"Failed to parse metadata value as JSON list: {value[:80]}...")
    return []


def build_teams_url(meta: dict) -> str:
    """Teams 채널/채팅 메시지 딥링크 URL을 생성합니다."""
    tenant_id = settings.TENANT_ID
    if not tenant_id:
        return ""

    source_id = meta.get('source_id', '')
    if not source_id:
        orig = meta.get('original_doc_id', '')
        if orig.startswith('teams-chat-'):
            source_id = orig[len('teams-chat-'):]
        elif orig.startswith('teams-'):
            source_id = orig[len('teams-'):]

    team_id = meta.get('teams_team_id', '')
    channel_id = meta.get('teams_channel_id', '')
    chat_id = meta.get('teams_chat_id', '')

    if team_id and channel_id and source_id:
        team_name = quote(meta.get('teams_team_name', ''), safe='')
        channel_name = quote(meta.get('teams_channel_name', ''), safe='')
        return (
            f"https://teams.microsoft.com/l/message/{channel_id}/{source_id}"
            f"?tenantId={tenant_id}&groupId={team_id}"
            f"&parentMessageId={source_id}&teamName={team_name}&channelName={channel_name}"
        )
    if chat_id and source_id:
        return (
            f"https://teams.microsoft.com/l/message/{chat_id}/{source_id}"
            f"?tenantId={tenant_id}&parentMessageId={source_id}"
        )
    if chat_id:
        return f"https://teams.microsoft.com/l/chat/{chat_id}/0?tenantId={tenant_id}"
    return ""


def doc_source_label(meta: dict) -> str:
    """프롬프트용 출처 한 줄 레이블을 생성합니다."""
    source = meta.get('source', 'unknown')
    title = meta.get('title', '')
    url = meta.get('url') or build_teams_url(meta) or ''
    author = meta.get('author', '')
    date = (meta.get('created_at') or meta.get('updated_at') or '')[:10]

    if source == 'jira':
        return f"[Jira] 제목: {title} | URL: {url} | 담당자: {author} | 날짜: {date}"
    if source == 'confluence':
        return f"[Confluence] 제목: {title} | URL: {url} | 작성자: {author} | 날짜: {date}"
    if source == 'sharepoint':
        return f"[SharePoint] 제목: {title} | URL: {url} | 작성자: {author} | 날짜: {date}"
    if source == 'teams':
        channel = meta.get('teams_channel_name') or meta.get('teams_chat_topic', '')
        return f"[Teams] 채널/채팅: {channel} | 작성자: {author} | 날짜: {date} | URL: {url}"
    return f"[{source}] 제목: {title} | URL: {url}"


def doc_display_name(meta: dict) -> str:
    """문서 표시명 — Jira: 이슈키, SharePoint: 파일명, 그 외: 제목"""
    source = meta.get("source", "")
    if source == "jira":
        key = meta.get("jira_issue_key", "")
        return key if key else (meta.get("title", "") or "Jira")
    if source == "confluence":
        return meta.get("title", "") or "Confluence"
    if source == "sharepoint":
        url = meta.get("url", "") or ""
        try:
            from urllib.parse import urlparse, unquote, parse_qs
            u = urlparse(url)
            qs = parse_qs(u.query)
            if "file" in qs:
                return unquote(qs["file"][0].split("/")[-1])
            segs = [s for s in u.path.split("/") if s]
            if segs:
                last = unquote(segs[-1])
                if not last.lower().endswith(".aspx"):
                    return last
        except Exception:
            pass
        return meta.get("title", "") or "SharePoint"
    if source == "teams":
        tn = meta.get("teams_team_name") or ""
        cn = meta.get("teams_channel_name") or ""
        ct = meta.get("teams_chat_topic") or ""
        if tn and cn:
            return f"Teams {tn}/{cn}"
        if ct:
            return f"Teams {ct}"
        return "Teams"
    return meta.get("title", "") or source


_REF_PATTERN = re.compile(r'\[REF(\d+)\]')
_JIRA_INLINE_RE = re.compile(r'\[([A-Z]+-\d+)\](?!\()')


def resolve_citations(answer: str, retrieved_docs: List[Dict]) -> Tuple[str, set]:
    """답변 내 [REF1] 인용을 마크다운 링크로 치환합니다."""
    cited: set = set()

    jira_key_map: dict = {}
    for i, doc in enumerate(retrieved_docs):
        meta = doc["metadata"]
        if meta.get("source") == "jira":
            key = meta.get("jira_issue_key", "")
            if key and key not in jira_key_map:
                url = meta.get("url", "") or ""
                jira_key_map[key] = (i, url)

    def replace_ref(m: re.Match) -> str:
        try:
            idx = int(m.group(1)) - 1
        except ValueError:
            return ""
        if idx < 0 or idx >= len(retrieved_docs):
            return ""
        cited.add(idx)
        doc = retrieved_docs[idx]
        meta = doc["metadata"]
        url = meta.get("url", "") or ""
        if not url or url == "None":
            url = build_teams_url(meta)
        name = doc_display_name(meta)
        return f"[{name}]({url})" if url else name

    new_answer = _REF_PATTERN.sub(replace_ref, answer)

    def replace_jira_inline(m: re.Match) -> str:
        key = m.group(1)
        if key in jira_key_map:
            idx, url = jira_key_map[key]
            cited.add(idx)
            return f"[{key}]({url})" if url else f"[{key}]"
        return m.group(0)

    new_answer = _JIRA_INLINE_RE.sub(replace_jira_inline, new_answer)
    return new_answer, cited

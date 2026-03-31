import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from company_llm_rag.config import settings
from company_llm_rag.exceptions import LLMError
from company_llm_rag.llm.factory import default_llm
from company_llm_rag.rag.citations import (
    ensure_list, build_teams_url, doc_source_label, doc_display_name, resolve_citations,
)
from company_llm_rag.rag.hub_direct import try_hub_direct_answer
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(env_path: str, default_filename: str) -> str:
    """
    프롬프트 파일을 로드합니다.
    env_path가 설정되어 있으면 해당 경로를 사용하고,
    없으면 패키지 내 기본 파일(prompts/)을 사용합니다.
    {AI_NAME}, {COMPANY_NAME}, {COMPANY_DESCRIPTION} 치환을 지원합니다.
    """
    path = Path(env_path) if env_path else _PROMPTS_DIR / default_filename
    try:
        content = path.read_text(encoding="utf-8")
        return content.format(
            AI_NAME=settings.AI_NAME,
            COMPANY_NAME=settings.COMPANY_NAME,
            COMPANY_DESCRIPTION=settings.COMPANY_DESCRIPTION,
        )
    except FileNotFoundError:
        logger.warning(f"Prompt file not found: {path}. Using empty string.")
        return ""



# 하위 호환 별칭 (내부에서 _ prefix로 참조하는 곳을 위해)
_ensure_list = ensure_list
_build_teams_url = build_teams_url
_doc_source_label = doc_source_label
_doc_display_name = doc_display_name
_resolve_citations = resolve_citations
_try_hub_direct_answer = try_hub_direct_answer


def build_rag_prompt(
    user_query: str,
    retrieved_docs: List[Dict],
    recency_window: int = 0,
    recency_explicit: bool = False,
) -> str:
    """
    Constructs a prompt for the LLM using the user's query and retrieved documents.

    Args:
        recency_window: 최신성 필터 적용 기간(일). 0이면 선언 없음.
        recency_explicit: True이면 사용자가 직접 기간을 지정한 경우.
    """
    context_parts = []
    for i, doc in enumerate(retrieved_docs):
        meta = doc['metadata']
        source = meta.get('source', 'unknown')
        source_label = _doc_source_label(meta)

        # 댓글/답글 포함
        comments_or_replies = []
        if source in ("jira", "confluence"):
            for c in _ensure_list(meta.get('comments')):
                if not isinstance(c, dict):
                    continue
                comments_or_replies.append(
                    f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}"
                )
        elif source == "teams":
            for r in _ensure_list(meta.get('replies')):
                if not isinstance(r, dict):
                    continue
                comments_or_replies.append(
                    f"Reply by {r.get('sender') or r.get('author')} on {r.get('created_at')}: {r.get('content')}"
                )

        comments_str = "\n".join(comments_or_replies)
        # 소스별 추가 메타데이터 (LLM이 상태/담당자 등을 판단할 수 있도록)
        extra = ""
        if source == "jira":
            status   = meta.get("status", "")
            assignee = meta.get("assignee", "")
            issue_type = meta.get("jira_issue_type", "")
            if status:   extra += f"상태: {status} | "
            if assignee: extra += f"담당자: {assignee} | "
            if issue_type: extra += f"유형: {issue_type}"
            extra = extra.rstrip(" |")

        context_parts.append(
            f"--- [REF{i+1}] | {source_label}{(' | ' + extra) if extra else ''} ---\n"
            f"{doc['content']}\n"
            f"{comments_str}\n"
            f"----------------------------------------------------------"
        )

    context = "\n\n".join(context_parts)

    instructions = _load_prompt(settings.RAG_INSTRUCTIONS_FILE, "rag_instructions.txt")
    if recency_window > 0:
        if recency_explicit:
            recency_hint = f"\n[검색 기준: 사용자 지정 최근 {recency_window}일 이내 Jira 일감을 표시합니다]\n"
        else:
            recency_hint = f"\n[검색 기준: 최근 {recency_window}일 이내 등록/수정된 Jira 일감을 우선 표시합니다]\n"
    else:
        recency_hint = ""
    prompt = (
        f"{instructions}\n\n"
        f"{recency_hint}"
        "Company Knowledge Base:\n"
        f"{context}\n\n"
        f"User Query: {user_query}\n\n"
        "Answer:"
    )
    return prompt

_MAX_HISTORY_TURNS = 10  # 최대 유지할 대화 턴 수 (초과 시 오래된 것부터 제거)


_RECENCY_KEYWORDS = [
    '최근', '최신', '가장 최근', '요즘',
    '이번 주', '이번달', '이번 분기',
    '최근에 등록', '최근 등록', '최근 생성', '최근에 생성',
    '새로 등록', '방금 등록', '새로 생성',
    '최근 추가', '새로 추가', '새로운',
    '최근 올라온', '최근 만든',
]

# Jira 날짜 필터 정책: 결과 부족 시 기간 점진 확대 (일)
_JIRA_RECENCY_WINDOWS = [30, 60, 90]
_JIRA_RECENCY_MIN_RESULTS = 3  # 최소 결과 수 미만이면 기간 확대

# 사용자 지정 기간 파싱 패턴 (순서 중요: 개월 > 주 > 일)
_EXPLICIT_PERIOD_PATTERNS = [
    (re.compile(r'(\d+)\s*(?:개월|달)'), lambda m: int(m.group(1)) * 30),
    (re.compile(r'(\d+)\s*주\s*(?:일|간|동안|이내)?'), lambda m: int(m.group(1)) * 7),
    (re.compile(r'(\d+)\s*일\s*(?:간|이내|동안)?'), lambda m: int(m.group(1))),
    (re.compile(r'한\s*달'), lambda m: 30),
    (re.compile(r'두\s*달'), lambda m: 60),
    (re.compile(r'세\s*달'), lambda m: 90),
]


def _parse_explicit_period(query: str) -> Optional[int]:
    """쿼리에서 사용자가 명시한 기간을 파싱해 일(day) 수로 반환합니다.
    예: '최근 7일' → 7, '지난 2주간' → 14, '1개월 내' → 30
    기간 표현이 없으면 None 반환.
    """
    for pattern, converter in _EXPLICIT_PERIOD_PATTERNS:
        m = pattern.search(query)
        if m:
            return converter(m)
    return None


def _is_listing_query(query: str) -> bool:
    """목록/현황/집계를 요청하는 쿼리인지 감지합니다."""
    q = query.lower()
    return any(k in q for k in [
        '목록', '리스트', '현황', '전체', '모두', '몇 개', '몇개',
        '진행중', '진행 중', '완료된', '대기중', '대기 중',
        '최근', '이번 주', '이번달', '이번 분기',
    ])

def _is_recency_query(query: str) -> bool:
    """최신/최근 항목을 요청하는 쿼리인지 감지합니다."""
    q = query.lower()
    return any(k in q for k in _RECENCY_KEYWORDS)

def _sort_by_recency(docs: List[Dict]) -> List[Dict]:
    """docs를 created_at 내림차순으로 정렬합니다. 날짜 없는 항목은 뒤로 이동."""
    def _date_key(doc: Dict) -> str:
        meta = doc.get("metadata", {})
        return meta.get("created_at") or meta.get("updated_at") or ""
    return sorted(docs, key=_date_key, reverse=True)


def _apply_jira_recency_filter(
    docs: List[Dict], explicit_days: Optional[int] = None
) -> Tuple[List[Dict], int]:
    """Jira 문서를 최근 N일 기준으로 필터링합니다.

    explicit_days가 있으면 해당 기간을 그대로 적용합니다.
    없으면 _JIRA_RECENCY_WINDOWS 순서로 점진 확대합니다.

    Returns:
        (필터링된 문서 리스트, 적용된 기간(일)) — Jira 없으면 (원본, 0)
    """
    jira_docs = [d for d in docs if d.get('metadata', {}).get('source') == 'jira']
    other_docs = [d for d in docs if d.get('metadata', {}).get('source') != 'jira']

    if not jira_docs:
        return docs, 0

    now = datetime.now(timezone.utc)

    windows = [explicit_days] if explicit_days is not None else _JIRA_RECENCY_WINDOWS
    min_results = 0 if explicit_days is not None else _JIRA_RECENCY_MIN_RESULTS

    for days in windows:
        cutoff = now - timedelta(days=days)
        filtered = []
        for doc in jira_docs:
            meta = doc.get('metadata', {})
            date_str = meta.get('created_at') or meta.get('updated_at') or ''
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.rstrip("Z"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    filtered.append(doc)
            except Exception:
                continue

        if len(filtered) >= min_results or days == windows[-1]:
            label = "사용자 지정" if explicit_days is not None else "자동"
            logger.info(f"[Jira 날짜 필터({label})] 최근 {days}일 적용 → {len(jira_docs)}개 중 {len(filtered)}개 통과")
            return filtered + other_docs, days

    return docs, 0


def _detect_filters(query: str) -> dict:
    """쿼리 텍스트에서 소스 및 파일 타입 필터를 감지합니다.
    키워드 목록은 config.SOURCE_FILTER_KEYWORDS / EXTENSION_FILTER_KEYWORDS에서 읽습니다.
    """
    q = query.lower()

    sources = [
        src for src, keywords in settings.SOURCE_FILTER_KEYWORDS.items()
        if any(k in q for k in keywords)
    ]

    extensions = []
    for ext_csv, keywords in settings.EXTENSION_FILTER_KEYWORDS.items():
        if any(k in q for k in keywords):
            exts = [e.strip() for e in ext_csv.split(",")]
            extensions.extend(exts)
            # 파일 타입 검색은 SharePoint 소스로 한정
            if 'sharepoint' not in sources and any(
                e in ('.xlsx', '.xls', '.pptx', '.ppt', '.docx', '.doc', '.pdf') for e in exts
            ):
                sources.append('sharepoint')

    return {'sources': sources, 'extensions': extensions}
_NO_ANSWER_PHRASE = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
_MAX_REFERENCES = 10   # 참고 링크 최대 표시 수
_MAX_REF_DISTANCE = 0.35  # 벡터 거리 기준치 — 이 이상이면 참고문서에서 제외 (L2 메트릭, 0.0=완전일치)
_MIN_FALLBACK_REFS = 3  # 인용 문서 0개일 때 최소 표시할 참고문서 수
_JIRA_KEY_RE = re.compile(r'\b([A-Z]+-\d+)\b')

# 쓸모없는 문서 패턴 (검색 후 LLM 컨텍스트에서 제외)
_EMPTY_CONTENT_PATTERNS = re.compile(
    r'^(content not extracted'       # SharePoint 텍스트 추출 실패
    r'|<systemEventMessage\s*/>'     # Teams 시스템 이벤트
    r'|<p\s*/>'                      # Confluence 빈 HTML
    r'|.*%%EOF\s*$'                  # PDF 바이너리 파편
    r'|.*startxref\s+\d+\s*$'       # PDF 바이너리 파편
    r')$',
    re.IGNORECASE | re.DOTALL,
)
_MIN_CONTENT_LEN = 10  # 10자 미만은 제외


def _is_usable_content(doc: Dict) -> bool:
    """LLM 컨텍스트에 포함할 가치가 있는 문서인지 판단합니다."""
    content = doc.get('content', '') or ''
    stripped = content.strip()
    if len(stripped) < _MIN_CONTENT_LEN:
        return False
    if _EMPTY_CONTENT_PATTERNS.match(stripped):
        return False
    return True


def _inject_jira_docs(query: str, retrieved_docs: List[Dict]) -> List[Dict]:
    """쿼리에 Jira 이슈 키(예: WMPO-10564)가 있으면 해당 청크를 앞에 삽입합니다.
    검색 결과에 이미 있는 경우 _injected 플래그만 추가합니다."""
    keys = _JIRA_KEY_RE.findall(query)
    if not keys:
        return retrieved_docs

    keys_set = set(keys)

    # 검색 결과에 이미 포함된 쿼리 키 문서는 _injected 마킹 (거리 필터 예외 처리용)
    for doc in retrieved_docs:
        if doc['metadata'].get('jira_issue_key', '') in keys_set:
            doc['_injected'] = True

    from company_llm_rag.database import db_manager
    collection = db_manager.get_collection()

    existing_keys = {d['metadata'].get('jira_issue_key', '') for d in retrieved_docs}
    injected: List[Dict] = []
    for key in keys:
        if key in existing_keys:
            continue
        try:
            res = collection.get(
                where={'jira_issue_key': {'$eq': key}},
                include=['documents', 'metadatas'],
                limit=5,
            )
            for i, doc_id in enumerate(res.get('ids', [])):
                injected.append({
                    'content': res['documents'][i],
                    'metadata': res['metadatas'][i],
                    '_distance': 0.0,
                    '_injected': True,
                })
        except Exception as e:
            logger.debug(f"Jira 이슈 직접 조회 실패 ({key}): {e}")

    if injected:
        logger.info(f"[Jira 직접 주입] {keys} → {len(injected)}개 청크 삽입")
    return injected + retrieved_docs


_SLIDE_RE = re.compile(r'\[Slide (\d+)\]')
_PAGE_RE  = re.compile(r'\[Page (\d+)\]')
_PPT_EXTS = {'.pptx', '.ppt'}
_PDF_EXTS = {'.pdf'}


def _extract_page_nums(content: str, pattern: re.Pattern) -> List[int]:
    """청크 텍스트에서 슬라이드/페이지 번호 목록을 추출합니다."""
    return sorted({int(m) for m in pattern.findall(content)})


def _build_references(retrieved_docs: List[Dict], listing: bool = False, cited_indices: set = None) -> List[Dict]:
    """retrieved_docs에서 참고 링크 목록을 생성합니다."""
    # URL별 슬라이드/페이지 번호 사전 수집 (같은 파일의 여러 청크에서 합산)
    url_slides: dict = {}
    for doc in retrieved_docs:
        meta = doc["metadata"]
        url = meta.get("url", "") or ""
        if not url:
            continue
        content = doc.get("content", "") or ""
        title_lower = meta.get("title", "").lower()
        ext = next((e for e in _PPT_EXTS | _PDF_EXTS if title_lower.endswith(e)), None)
        if ext in _PPT_EXTS:
            nums = _extract_page_nums(content, _SLIDE_RE)
            if nums:
                url_slides.setdefault(url, set()).update(nums)
        elif ext in _PDF_EXTS:
            nums = _extract_page_nums(content, _PAGE_RE)
            if nums:
                url_slides.setdefault(url, set()).update(nums)

    max_refs = int(_MAX_REFERENCES * 1.5) if listing else _MAX_REFERENCES
    seen: set = set()          # URL 기반 중복 제거
    seen_issue_keys: set = set()  # Jira issue_key 기반 중복 제거
    references = []

    # 2-pass: 인용 문서 우선 → 나머지 거리 필터 적용
    # pass 1: LLM이 답변에서 직접 인용한 문서 + injected 문서 (최상단 배치)
    # pass 2: 나머지 문서 (거리 기준치 적용)
    pass_order = []
    for i, doc in enumerate(retrieved_docs):
        is_cited = cited_indices is not None and i in cited_indices
        is_hub = (settings.KNOWLEDGE_HUB_TEAM_NAME
                  and doc.get('metadata', {}).get('teams_team_name', '') == settings.KNOWLEDGE_HUB_TEAM_NAME)
        if is_cited or doc.get('_injected', False) or is_hub:
            pass_order.insert(len([p for p in pass_order if p[1]]), (i, True))  # 우선 그룹
        else:
            pass_order.append((i, False))

    priority_count = sum(1 for _, p in pass_order if p)
    for i, is_priority in pass_order:
        if len(references) >= max_refs:
            break
        doc = retrieved_docs[i]
        meta = doc["metadata"]
        # 비우선 문서는 거리 기준치 적용 (단, 인용 문서가 0개면 상위 N개 fallback)
        if not is_priority and doc.get('_distance', 0.0) > _MAX_REF_DISTANCE:
            if priority_count > 0 or len(references) >= _MIN_FALLBACK_REFS:
                continue
        url = meta.get("url", "") or ""
        if not url or url == "None":
            url = _build_teams_url(meta)
        if not url:
            continue
        if url in seen:
            continue
        source = meta.get("source", "")
        title = meta.get("title", "")
        author = meta.get("author", "") or ""
        issue_key = meta.get("jira_issue_key", "") if source == "jira" else ""
        # Jira: 동일 이슈키의 다른 청크(댓글 등 다른 URL)도 중복 제거
        if source == "jira" and issue_key and issue_key in seen_issue_keys:
            continue
        seen.add(url)
        if source == "jira" and issue_key:
            seen_issue_keys.add(issue_key)
        space_name = ""
        space_key = ""
        ancestors = ""
        if source == "confluence":
            space_name = meta.get("confluence_space_name") or meta.get("confluence_space_key") or ""
            space_key = meta.get("confluence_space_key") or ""
            ancestors = meta.get("confluence_ancestors", "") or ""
        project_key = ""
        if source == "jira":
            project_key = meta.get("jira_project_key") or (issue_key.split("-")[0] if issue_key else "") or ""
        site_name = ""
        file_path = ""
        if source == "sharepoint":
            site_name = meta.get("sharepoint_site_name") or ""
            file_path = meta.get("sharepoint_file_path") or ""
        team_name = ""
        channel_name = ""
        chat_topic = ""
        created_at = ""
        snippet = ""
        if source == "teams":
            tn = meta.get("teams_team_name") or ""
            cn = meta.get("teams_channel_name") or ""
            team_name = "" if tn in ("", "None") else tn
            channel_name = "" if cn in ("", "None") else cn
            chat_topic = meta.get("teams_chat_topic") or ""
            if chat_topic in ("None", "null"):
                chat_topic = ""
            created_at = meta.get("created_at", "") or ""
            snippet = (doc.get("content") or "").strip()[:90]
        page_nums = sorted(url_slides.get(url, set()))
        references.append({
            "title": title,
            "url": url,
            "source": source,
            "content_type": meta.get("content_type", ""),
            "doc_id": meta.get("original_doc_id", ""),
            "issue_key": issue_key,
            "project_key": project_key,
            "space_name": space_name,
            "space_key": space_key,
            "ancestors": ancestors,
            "site_name": site_name,
            "file_path": file_path,
            "team_name": team_name,
            "channel_name": channel_name,
            "chat_topic": chat_topic,
            "author": author,
            "created_at": created_at,
            "snippet": snippet,
            "page_nums": page_nums,
        })
    return references


def get_llm_response(
    prompt: str,
    conversation_history: Optional[List[Dict]] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """
    LLM으로부터 응답을 가져옵니다.

    Args:
        prompt: 현재 턴의 RAG 컨텍스트 포함 프롬프트
        conversation_history: 이전 대화 히스토리 (role/content 딕셔너리 리스트)
        model: 사용할 모델 (기본값: settings.OPENAI_CHAT_MODEL)
        temperature: 생성 온도 (기본값: settings.OPENAI_TEMPERATURE)

    Returns:
        LLM 응답 텍스트
    """
    system_prompt = _load_prompt(settings.SYSTEM_PROMPT_FILE, "system_prompt.txt")
    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    try:
        return default_llm.chat(messages, model=model, temperature=temperature)
    except LLMError as e:
        logger.error(f"LLM 호출 실패: {e}", exc_info=True)
        return "답변 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


def rag_query(
    user_query: str,
    conversation_history: Optional[List[Dict]] = None,
    n_results: Optional[int] = None,
    return_refs: bool = False,
    _docs_out: Optional[List] = None,
):
    """
    RAG 쿼리 실행: 문서를 검색하고 LLM 응답을 생성합니다.

    Args:
        user_query: 사용자 질문
        conversation_history: 이전 대화 히스토리
        n_results: 검색할 문서 개수 (기본값: settings.RETRIEVAL_TOP_K)
        return_refs: True이면 (answer, references) 튜플 반환

    Returns:
        str 또는 (str, List[Dict]) — return_refs=True일 때 참고 링크 포함
    """
    t0 = time.monotonic()
    filters = _detect_filters(user_query)
    listing = _is_listing_query(user_query)
    explicit_period = _parse_explicit_period(user_query)
    recency = _is_recency_query(user_query) or (explicit_period is not None)
    effective_n = (n_results or settings.RETRIEVAL_TOP_K) * (3 if listing else 1)
    retrieved_docs, ret_timing = retrieve_documents(
        user_query,
        n_results=effective_n,
        source_filter=filters['sources'] or None,
        url_extensions=filters['extensions'] or None,
        return_timing=True,
        return_scores=True,
        recency_boost=recency,
    )
    t_retrieval = time.monotonic()
    retrieval_ms = int((t_retrieval - t0) * 1000)

    # 쓸모없는 문서 제거 (빈 내용, PDF 바이너리, Teams 시스템 이벤트 등)
    retrieved_docs = [d for d in retrieved_docs if _is_usable_content(d)]

    # 최신/최근 쿼리 처리
    recency_window = 0
    if recency:
        retrieved_docs, recency_window = _apply_jira_recency_filter(retrieved_docs, explicit_days=explicit_period)
        retrieved_docs = _sort_by_recency(retrieved_docs)

    t_inject_start = time.monotonic()
    retrieved_docs = _inject_jira_docs(user_query, retrieved_docs)
    inject_ms = int((time.monotonic() - t_inject_start) * 1000)

    if _docs_out is not None:
        _docs_out.extend(retrieved_docs)

    if not retrieved_docs:
        answer = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
        timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": 0, "total_ms": retrieval_ms, "doc_count": 0, "model": default_llm.model_name}
        return (answer, [], timing) if return_refs else answer

    # Knowledge Hub 원문 직접 응답: 1위 문서가 Knowledge Hub이면 LLM 없이 원문 반환
    hub_direct = _try_hub_direct_answer(retrieved_docs)
    if hub_direct:
        t_llm = time.monotonic()
        total_ms = int((t_llm - t0) * 1000)
        timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": 0, "total_ms": total_ms, "doc_count": len(retrieved_docs), "model": "knowledge_hub_direct"}
        if not return_refs:
            return hub_direct
        return hub_direct, [], timing

    prompt = build_rag_prompt(user_query, retrieved_docs, recency_window=recency_window, recency_explicit=(explicit_period is not None))
    llm_response = get_llm_response(prompt, conversation_history=conversation_history)
    t_llm = time.monotonic()
    llm_ms = int((t_llm - t_retrieval) * 1000)
    total_ms = int((t_llm - t0) * 1000)

    # [REF1] 인용 치환 → 실제 문서명+링크 마크다운
    llm_response, cited = _resolve_citations(llm_response, retrieved_docs)

    logger.info(
        f"[RAG 성능] 검색={retrieval_ms}ms (벡터={ret_timing['vector_ms']}ms / FTS={ret_timing['keyword_ms']}ms) | 직접조회={inject_ms}ms | LLM={llm_ms}ms | "
        f"총={total_ms}ms | 문서={len(retrieved_docs)}개 | 인용={len(cited)}건"
    )
    timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": llm_ms, "total_ms": total_ms, "doc_count": len(retrieved_docs), "model": default_llm.model_name}

    if not return_refs:
        return llm_response

    is_no_answer = llm_response.strip().endswith(_NO_ANSWER_PHRASE)
    if is_no_answer:
        references = []
    else:
        # [REFn] 인용 제거 이후: 검색된 전체 문서를 참고문서로 표시 (거리 필터는 _build_references 내부)
        references = _build_references(retrieved_docs, listing, cited_indices=cited)
    return llm_response, references, timing


def rag_query_stream(
    user_query: str,
    conversation_history: Optional[List[Dict]] = None,
    n_results: Optional[int] = None,
    _docs_out: Optional[List] = None,
):
    """
    RAG 스트리밍: 검색 후 LLM 응답을 토큰 단위로 yield합니다.

    Yields:
        {"type": "token", "text": str}         — LLM 토큰
        {"type": "done", "answer": str,
         "references": list, "timing": dict,
         "is_no_answer": bool}                  — 완료 (참고 링크 포함)
        {"type": "error", "message": str}       — 오류
    """
    t0 = time.monotonic()
    filters = _detect_filters(user_query)
    listing = _is_listing_query(user_query)
    explicit_period = _parse_explicit_period(user_query)
    recency = _is_recency_query(user_query) or (explicit_period is not None)
    effective_n = (n_results or settings.RETRIEVAL_TOP_K) * (3 if listing else 1)
    retrieved_docs, ret_timing = retrieve_documents(
        user_query,
        n_results=effective_n,
        source_filter=filters['sources'] or None,
        url_extensions=filters['extensions'] or None,
        return_timing=True,
        return_scores=True,
        recency_boost=recency,
    )
    t_retrieval = time.monotonic()
    retrieval_ms = int((t_retrieval - t0) * 1000)

    # 쓸모없는 문서 제거 (빈 내용, PDF 바이너리, Teams 시스템 이벤트 등)
    retrieved_docs = [d for d in retrieved_docs if _is_usable_content(d)]

    # 최신/최근 쿼리 처리
    recency_window = 0
    if recency:
        retrieved_docs, recency_window = _apply_jira_recency_filter(retrieved_docs, explicit_days=explicit_period)
        retrieved_docs = _sort_by_recency(retrieved_docs)

    t_inject_start = time.monotonic()
    retrieved_docs = _inject_jira_docs(user_query, retrieved_docs)
    inject_ms = int((time.monotonic() - t_inject_start) * 1000)

    # 호출자가 원할 경우 retrieved_docs를 외부로 노출 (분석용)
    if _docs_out is not None:
        _docs_out.extend(retrieved_docs)

    if not retrieved_docs:
        answer = _NO_ANSWER_PHRASE
        timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": 0, "total_ms": retrieval_ms, "doc_count": 0, "model": default_llm.model_name}
        yield {"type": "done", "answer": answer, "references": [], "timing": timing, "is_no_answer": True}
        return

    # Knowledge Hub 원문 직접 응답
    hub_direct = _try_hub_direct_answer(retrieved_docs)
    if hub_direct:
        total_ms = int((time.monotonic() - t0) * 1000)
        timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": 0, "total_ms": total_ms, "doc_count": len(retrieved_docs), "model": "knowledge_hub_direct"}
        yield {"type": "token", "text": hub_direct}
        yield {"type": "done", "answer": hub_direct, "references": [], "timing": timing, "is_no_answer": False}
        return

    prompt = build_rag_prompt(user_query, retrieved_docs, recency_window=recency_window, recency_explicit=(explicit_period is not None))
    system_prompt = _load_prompt(settings.SYSTEM_PROMPT_FILE, "system_prompt.txt")
    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    full_answer = ""
    t_llm_start = time.monotonic()
    try:
        for chunk in default_llm.stream_chat(messages):
            full_answer += chunk
            yield {"type": "token", "text": chunk}
    except Exception as e:
        logger.error(f"스트리밍 LLM 오류: {e}", exc_info=True)
        yield {"type": "error", "message": str(e)}
        return

    t_llm = time.monotonic()
    llm_ms = int((t_llm - t_llm_start) * 1000)
    total_ms = int((t_llm - t0) * 1000)

    # [REF1] 인용 치환 → 실제 문서명+링크 마크다운
    full_answer, cited = _resolve_citations(full_answer, retrieved_docs)

    timing = {"retrieval_ms": retrieval_ms, "vector_ms": ret_timing["vector_ms"], "keyword_ms": ret_timing["keyword_ms"], "inject_ms": inject_ms, "llm_ms": llm_ms, "total_ms": total_ms, "doc_count": len(retrieved_docs), "model": default_llm.model_name}
    logger.info(
        f"[RAG 스트리밍 성능] 검색={retrieval_ms}ms (벡터={ret_timing['vector_ms']}ms / FTS={ret_timing['keyword_ms']}ms) | 직접조회={inject_ms}ms | LLM={llm_ms}ms | "
        f"총={total_ms}ms | 문서={len(retrieved_docs)}개 | 인용={len(cited)}건"
    )

    is_no_answer = full_answer.strip().endswith(_NO_ANSWER_PHRASE)
    if is_no_answer:
        references = []
    else:
        # [REFn] 인용 제거 이후: 검색된 전체 문서를 참고문서로 표시 (거리 필터는 _build_references 내부)
        references = _build_references(retrieved_docs, listing, cited_indices=cited)
    yield {"type": "done", "answer": full_answer, "references": references, "timing": timing, "is_no_answer": is_no_answer}


if __name__ == "__main__":
    logger.info("Company LLM RAG System ready. Type 'exit' to quit.")
    conversation_history: List[Dict] = []

    while True:
        try:
            query = input("\nEnter your query: ")
            if query.lower() == 'exit':
                break

            logger.debug(f"Processing query: {query}")
            response = rag_query(query, conversation_history=conversation_history)
            print("\nLLM Response:")
            print(response)

            # 히스토리에 현재 턴 추가 (RAG 컨텍스트 문서 제외, 순수 Q&A만 저장)
            conversation_history.append({"role": "user", "content": query})
            conversation_history.append({"role": "assistant", "content": response})

            # 오래된 히스토리 제거 (턴 단위: user+assistant = 1턴)
            max_messages = _MAX_HISTORY_TURNS * 2
            if len(conversation_history) > max_messages:
                conversation_history = conversation_history[-max_messages:]

        except EOFError:
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)

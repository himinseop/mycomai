import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import quote

from company_llm_rag.config import settings
from company_llm_rag.exceptions import LLMError
from company_llm_rag.llm.openai_provider import default_llm
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


def _ensure_list(value) -> list:
    """
    값이 list이면 그대로 반환하고, str(직렬화된 JSON)이면 파싱합니다.
    ChromaDB는 모든 메타데이터 값을 기본형(str/int/float/bool)로만
    저장하므로, 코멘트/답글 구조체는 JSON으로 직렬화되어 저장됩니다.
    retrieval_module의 JSON 파싱이 실패한 경우에도 안전하게 병 통과합니다.

    Args:
        value: 변환할 값 (list, str, 또는 None)

    Returns:
        list (파싱 실패 또는 None이면 빈 리스트)
    """
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

def _build_teams_url(meta: dict) -> str:
    """Teams 채널/채팅 메시지 딥링크 URL을 생성합니다."""
    tenant_id = settings.TENANT_ID
    if not tenant_id:
        return ""

    # source_id: teams_extractor에서 message.get('id')로 저장
    source_id = meta.get('source_id', '')
    if not source_id:
        # original_doc_id 에서 추출 (예: "teams-1a2b3c")
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
        return f"https://teams.microsoft.com/l/message/{chat_id}/{source_id}?tenantId={tenant_id}"
    return ""


def _doc_source_label(meta: dict) -> str:
    """프롬프트용 출처 한 줄 레이블을 생성합니다."""
    source = meta.get('source', 'unknown')
    title = meta.get('title', '')
    url = meta.get('url') or _build_teams_url(meta) or ''
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


def build_rag_prompt(user_query: str, retrieved_docs: List[Dict]) -> str:
    """
    Constructs a prompt for the LLM using the user's query and retrieved documents.
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
            f"--- 문서 {i+1} | {source_label}{(' | ' + extra) if extra else ''} ---\n"
            f"{doc['content']}\n"
            f"{comments_str}\n"
            f"----------------------------------------------------------"
        )

    context = "\n\n".join(context_parts)

    instructions = _load_prompt(settings.RAG_INSTRUCTIONS_FILE, "rag_instructions.txt")
    prompt = (
        f"{instructions}\n\n"
        "Company Knowledge Base:\n"
        f"{context}\n\n"
        f"User Query: {user_query}\n\n"
        "Answer:"
    )
    return prompt

_MAX_HISTORY_TURNS = 10  # 최대 유지할 대화 턴 수 (초과 시 오래된 것부터 제거)


def _is_listing_query(query: str) -> bool:
    """목록/현황/집계를 요청하는 쿼리인지 감지합니다."""
    q = query.lower()
    return any(k in q for k in [
        '목록', '리스트', '현황', '전체', '모두', '몇 개', '몇개',
        '진행중', '진행 중', '완료된', '대기중', '대기 중',
        '최근', '이번 주', '이번달', '이번 분기',
    ])


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
_MAX_REFERENCES = 5  # 참고 링크 최대 표시 수 (RRF 순위 기준 상위 N개)


def _build_references(retrieved_docs: List[Dict], listing: bool = False) -> List[Dict]:
    """retrieved_docs에서 참고 링크 목록을 생성합니다."""
    max_refs = _MAX_REFERENCES * (2 if listing else 1)
    seen = set()
    references = []
    for doc in retrieved_docs:
        if len(references) >= max_refs:
            break
        meta = doc["metadata"]
        url = meta.get("url", "") or ""
        if not url or url == "None":
            url = _build_teams_url(meta)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        source = meta.get("source", "")
        title = meta.get("title", "")
        author = meta.get("author", "") or ""
        issue_key = meta.get("jira_issue_key", "") if source == "jira" else ""
        space_name = ""
        ancestors = ""
        if source == "confluence":
            space_name = meta.get("confluence_space_name") or meta.get("confluence_space_key") or ""
            ancestors = meta.get("confluence_ancestors", "") or ""
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
        references.append({
            "title": title,
            "url": url,
            "source": source,
            "content_type": meta.get("content_type", ""),
            "issue_key": issue_key,
            "space_name": space_name,
            "ancestors": ancestors,
            "team_name": team_name,
            "channel_name": channel_name,
            "chat_topic": chat_topic,
            "author": author,
            "created_at": created_at,
            "snippet": snippet,
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
    effective_n = (n_results or settings.RETRIEVAL_TOP_K) * (3 if listing else 1)
    retrieved_docs = retrieve_documents(
        user_query,
        n_results=effective_n,
        source_filter=filters['sources'] or None,
        url_extensions=filters['extensions'] or None,
    )
    t_retrieval = time.monotonic()
    retrieval_ms = int((t_retrieval - t0) * 1000)

    if not retrieved_docs:
        answer = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
        timing = {"retrieval_ms": retrieval_ms, "llm_ms": 0, "total_ms": retrieval_ms, "doc_count": 0}
        return (answer, [], timing) if return_refs else answer

    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt, conversation_history=conversation_history)
    t_llm = time.monotonic()
    llm_ms = int((t_llm - t_retrieval) * 1000)
    total_ms = int((t_llm - t0) * 1000)

    logger.info(
        f"[RAG 성능] 검색={retrieval_ms}ms | LLM={llm_ms}ms | "
        f"총={total_ms}ms | 문서={len(retrieved_docs)}개"
    )
    timing = {"retrieval_ms": retrieval_ms, "llm_ms": llm_ms, "total_ms": total_ms, "doc_count": len(retrieved_docs)}

    if not return_refs:
        return llm_response

    # 답변에 정보 없음 문구가 포함된 경우 참고 링크 없음
    if _NO_ANSWER_PHRASE in llm_response:
        return llm_response, [], timing

    references = _build_references(retrieved_docs, listing)
    return llm_response, references, timing


def rag_query_stream(
    user_query: str,
    conversation_history: Optional[List[Dict]] = None,
    n_results: Optional[int] = None,
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
    effective_n = (n_results or settings.RETRIEVAL_TOP_K) * (3 if listing else 1)
    retrieved_docs = retrieve_documents(
        user_query,
        n_results=effective_n,
        source_filter=filters['sources'] or None,
        url_extensions=filters['extensions'] or None,
    )
    t_retrieval = time.monotonic()
    retrieval_ms = int((t_retrieval - t0) * 1000)

    if not retrieved_docs:
        answer = _NO_ANSWER_PHRASE
        timing = {"retrieval_ms": retrieval_ms, "llm_ms": 0, "total_ms": retrieval_ms, "doc_count": 0}
        yield {"type": "done", "answer": answer, "references": [], "timing": timing, "is_no_answer": True}
        return

    prompt = build_rag_prompt(user_query, retrieved_docs)
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
    timing = {"retrieval_ms": retrieval_ms, "llm_ms": llm_ms, "total_ms": total_ms, "doc_count": len(retrieved_docs)}
    logger.info(
        f"[RAG 스트리밍 성능] 검색={retrieval_ms}ms | LLM={llm_ms}ms | "
        f"총={total_ms}ms | 문서={len(retrieved_docs)}개"
    )

    is_no_answer = _NO_ANSWER_PHRASE in full_answer
    references = [] if is_no_answer else _build_references(retrieved_docs, listing)
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

import json
from typing import List, Dict, Optional
from urllib.parse import quote

import openai

from company_llm_rag.config import settings
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


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

    prompt = (
        "너는 '오사장'이야. 슈퍼커넥트 직원들의 업무 궁금증을 해결해주는 역할을 하고 있어.\n"
        "아래 회사 지식베이스 문서를 바탕으로 질문에 답변해줘.\n\n"
        "답변 규칙:\n"
        "- 반드시 아래 제공된 문서 내용만 활용해서 답변해.\n"
        "- 출처를 구체적으로 밝혀줘:\n"
        "  · Jira: 'Jira [이슈키 제목](URL) 이슈에서 확인됩니다.'\n"
        "  · Confluence: 'Confluence [페이지 제목](URL) 문서에 따르면'\n"
        "  · SharePoint: 'SharePoint [문서 제목](URL)에서 확인됩니다.'\n"
        "  · Teams: '팀즈 [채널명] 채널에서 작성자님이 날짜에 언급했습니다.' URL이 있으면 채널명에 링크를 걸어줘.\n"
        "- 문서를 찾아달라는 요청('찾아줘', '있어?')이면 문서가 있음을 알리고 핵심 내용을 요약하고 URL을 제공해줘.\n"
        "- PPT/PPTX 파일 내용에는 [Slide N] 형식으로 슬라이드 번호가 표시되어 있어. 해당 내용을 인용할 때는 '(N번 슬라이드)'와 같이 슬라이드 번호를 함께 언급해줘.\n"
        "- '목록', '현황', '진행중', '최근' 등 목록성 질문이면 아래 문서들의 제목·상태·담당자를 항목별로 나열해줘. 제공된 문서가 전체 목록이 아닐 수 있으므로 '검색된 항목 기준' 임을 명시해줘.\n"
        "- 로컬 파일이어서 URL이 없으면 '로컬 파일로 저장되어 있으며 URL이 없습니다'라고 해줘.\n"
        "- 회사 자금, 비밀번호, 계정 정보 등 보안에 민감한 데이터는 문서에 있더라도 절대 답변하지 마.\n"
        "- 업무와 관련 없는 사적인 질문은 정중하게 거절해.\n"
        "- 문서에서 답을 찾을 수 없으면 정확히 이렇게만 답변해: '관련 정보를 회사 지식베이스에서 찾을 수 없습니다.'\n"
        "- 정보를 지어내지 마.\n"
        "- 항상 한국어로 답변해.\n\n"
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
    """쿼리 텍스트에서 소스 및 파일 타입 필터를 감지합니다."""
    q = query.lower()

    sources = []
    if any(k in q for k in ['지라', 'jira', '이슈에서', '이슈로']):
        sources.append('jira')
    if any(k in q for k in ['컨플루언스', '컨플에서', '컨플루', 'confluence']):
        sources.append('confluence')
    if any(k in q for k in ['팀즈에서', '팀즈 대화', '팀즈에', 'teams', '대화에서', '채팅에서', '채널에서']):
        sources.append('teams')
    if any(k in q for k in ['쉐어포인트', 'sharepoint']):
        sources.append('sharepoint')

    extensions = []
    if any(k in q for k in ['엑셀', 'excel', '.xlsx', '.xls']):
        extensions.extend(['.xlsx', '.xls'])
        if 'sharepoint' not in sources:
            sources.append('sharepoint')
    if any(k in q for k in ['ppt', '파워포인트', '기획서', '발표자료', '프레젠테이션']):
        extensions.extend(['.pptx', '.ppt'])
        if 'sharepoint' not in sources:
            sources.append('sharepoint')
    if any(k in q for k in ['.docx', '.doc', 'word 문서']):
        extensions.extend(['.docx', '.doc'])
    if '.pdf' in q or ' pdf ' in q:
        extensions.append('.pdf')

    return {'sources': sources, 'extensions': extensions}
_NO_ANSWER_PHRASE = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
_REFERENCE_DISTANCE_THRESHOLD = 0.5  # 이 값 미만인 문서만 참고 링크로 표시


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
    if model is None:
        model = settings.OPENAI_CHAT_MODEL
    if temperature is None:
        temperature = settings.OPENAI_TEMPERATURE

    messages = [{"role": "system", "content": (
        "너의 이름은 '오사장'이야. "
        "슈퍼커넥트는 배달앱을 만드는 회사이고, 넌 이 회사의 직원이야. "
        "회사의 업무효율을 높이기 위해 다른 직원들의 궁금증을 해결해주는 업무를 수행하고 있어. "
        "회사 업무에 충실하기 때문에 업무 외의 사적인 질문에는 정중하게 답변을 거절해. "
        "특히 회사 자금, 비밀번호, 계정 정보 등 보안에 민감한 데이터는 절대 누설하지 마. "
        "항상 한국어로 답변해."
    )}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    try:
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error getting response from LLM: {e}", exc_info=True)
        return f"Error getting response from LLM: {e}"


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
    filters = _detect_filters(user_query)
    listing = _is_listing_query(user_query)
    effective_n = (n_results or settings.RETRIEVAL_TOP_K) * (3 if listing else 1)
    retrieved_docs = retrieve_documents(
        user_query,
        n_results=effective_n,
        source_filter=filters['sources'] or None,
        url_extensions=filters['extensions'] or None,
    )

    if not retrieved_docs:
        answer = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
        return (answer, []) if return_refs else answer

    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt, conversation_history=conversation_history)

    if not return_refs:
        return llm_response

    # 답변에 정보 없음 문구가 포함된 경우 참고 링크 없음
    if _NO_ANSWER_PHRASE in llm_response:
        return llm_response, []

    # 목록 쿼리는 임계값 완화, 일반 쿼리는 0.5
    ref_threshold = 0.8 if listing else _REFERENCE_DISTANCE_THRESHOLD

    # URL이 있는 문서만 중복 제거하여 참고 링크 구성 (Teams는 딥링크 생성)
    seen = set()
    references = []
    for doc in retrieved_docs:
        if doc.get("_distance", 1.0) >= ref_threshold:
            continue
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

        # Jira: jira_issue_key 메타데이터 사용 (예: "WMPO-1234")
        issue_key = ""
        if source == "jira":
            issue_key = meta.get("jira_issue_key", "")

        # Confluence: 스페이스 표시 이름 + 조상 경로
        space_name = ""
        ancestors = ""
        if source == "confluence":
            space_name = meta.get("confluence_space_name") or meta.get("confluence_space_key") or ""
            ancestors = meta.get("confluence_ancestors", "") or ""

        # Teams: 팀명/채널명 또는 채팅방명, 날짜, 작성자, 스니펫
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

    return llm_response, references


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

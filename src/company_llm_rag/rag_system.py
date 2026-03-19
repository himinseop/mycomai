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
        context_parts.append(
            f"--- 문서 {i+1} | {source_label} ---\n"
            f"{doc['content']}\n"
            f"{comments_str}\n"
            f"----------------------------------------------------------"
        )

    context = "\n\n".join(context_parts)

    prompt = (
        "You are an AI assistant for a company. Your task is to answer questions based on the provided company knowledge base.\n"
        "Guidelines:\n"
        "- Use only the information from the documents provided below.\n"
        "- When citing information, always mention the specific source explicitly in Korean:\n"
        "  · Jira: 'Jira [이슈키 제목](URL)에서 확인됩니다.' (예: 'Jira [WMPO-123 결제 오류 수정](https://...) 이슈에서 언급됩니다.')\n"
        "  · Confluence: 'Confluence [페이지 제목](URL) 문서에 따르면'\n"
        "  · SharePoint: 'SharePoint [문서 제목](URL)에서 확인됩니다.'\n"
        "  · Teams: '팀즈 [채널명] 채널에서 작성자님이 날짜에 언급했습니다.' URL이 있으면 채널명에 링크를 걸어주세요.\n"
        "- If the user is looking for a document (e.g. '찾아줘', '있어?'), tell them the document exists, summarize its key contents, and provide the URL if available.\n"
        "- If no URL is available for a local file, say '로컬 파일로 저장되어 있으며 URL이 없습니다'.\n"
        "- If the answer truly cannot be found in the documents, respond with exactly: '관련 정보를 회사 지식베이스에서 찾을 수 없습니다.'\n"
        "- Do not make up any information.\n"
        "- Always respond in Korean.\n\n"
        "Company Knowledge Base:\n"
        f"{context}\n\n"
        f"User Query: {user_query}\n\n"
        "Answer:"
    )
    return prompt

_MAX_HISTORY_TURNS = 10  # 최대 유지할 대화 턴 수 (초과 시 오래된 것부터 제거)


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

    messages = [{"role": "system", "content": "You are a helpful assistant. Always respond in Korean."}]
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
    retrieved_docs = retrieve_documents(user_query, n_results=n_results)

    if not retrieved_docs:
        answer = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
        return (answer, []) if return_refs else answer

    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt, conversation_history=conversation_history)

    if not return_refs:
        return llm_response

    # URL이 있는 문서만 중복 제거하여 참고 링크 구성 (Teams는 딥링크 생성)
    seen = set()
    references = []
    for doc in retrieved_docs:
        meta = doc["metadata"]
        url = meta.get("url", "") or ""
        if not url or url == "None":
            url = _build_teams_url(meta)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        references.append({
            "title": meta.get("title", ""),
            "url": url,
            "source": meta.get("source", ""),
            "content_type": meta.get("content_type", ""),
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

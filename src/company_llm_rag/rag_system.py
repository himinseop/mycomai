import json
from typing import List, Dict, Optional

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

def build_rag_prompt(user_query: str, retrieved_docs: List[Dict]) -> str:
    """
    Constructs a prompt for the LLM using the user's query and retrieved documents.
    """
    context_parts = []
    for i, doc in enumerate(retrieved_docs):
        # Extract metadata for better context
        source = doc['metadata'].get('source', 'unknown')
        title = doc['metadata'].get('title', 'Untitled')
        url = doc['metadata'].get('url', 'No URL')
        
        # Include comments/replies if available
        # _ensure_list()로 감싸서 str(JSON 직렬화)로 남아있는 경우도 안전하게 처리
        comments_or_replies = []
        if source in ("jira", "confluence"):
            for c in _ensure_list(doc['metadata'].get('comments')):
                if not isinstance(c, dict):
                    continue
                comments_or_replies.append(
                    f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}"
                )
        elif source == "teams":
            for r in _ensure_list(doc['metadata'].get('replies')):
                if not isinstance(r, dict):
                    continue
                comments_or_replies.append(
                    f"Reply by {r.get('sender') or r.get('author')} on {r.get('created_at')}: {r.get('content')}"
                )

        comments_or_replies_str = "\n".join(comments_or_replies) if comments_or_replies else ""

        context_parts.append(f"--- Document {i+1} (Source: {source}, Title: {title}, URL: {url}) ---\n"
                             f"{doc['content']}\n"
                             f"{comments_or_replies_str}\n"
                             f"----------------------------------------------------------")

    context = "\n\n".join(context_parts)

    prompt = (
        "You are an AI assistant for a company. Your task is to answer questions based on the provided company knowledge base.\n"
        "Guidelines:\n"
        "- Use only the information from the documents provided below.\n"
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
) -> str:
    """
    RAG 쿼리 실행: 문서를 검색하고 LLM 응답을 생성합니다.

    Args:
        user_query: 사용자 질문
        conversation_history: 이전 대화 히스토리
        n_results: 검색할 문서 개수 (기본값: settings.RETRIEVAL_TOP_K)

    Returns:
        LLM 응답
    """
    retrieved_docs = retrieve_documents(user_query, n_results=n_results)

    if not retrieved_docs:
        return "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."

    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt, conversation_history=conversation_history)

    return llm_response


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

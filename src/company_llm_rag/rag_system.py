from typing import List, Dict

import openai

from company_llm_rag.config import settings
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

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
        comments_or_replies = []
        if source == "jira" and doc['metadata'].get('comments'):
            comments_or_replies = [f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}" for c in doc['metadata']['comments']]
        elif source == "confluence" and doc['metadata'].get('comments'):
            comments_or_replies = [f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}" for c in doc['metadata']['comments']]
        elif source == "teams" and doc['metadata'].get('replies'):
            comments_or_replies = [f"Reply by {r.get('sender')} on {r.get('created_at')}: {r.get('content')}" for r in doc['metadata']['replies']]

        comments_or_replies_str = "\n".join(comments_or_replies) if comments_or_replies else ""

        context_parts.append(f"--- Document {i+1} (Source: {source}, Title: {title}, URL: {url}) ---\n"
                             f"{doc['content']}\n"
                             f"{comments_or_replies_str}\n"
                             f"----------------------------------------------------------")

    context = "\n\n".join(context_parts)

    prompt = (
        "You are an AI assistant for a company. Your task is to answer questions based on the provided company knowledge base. "
        "Use only the information from the documents provided below to answer the question. "
        "If the answer cannot be found in the documents, state that you don't have enough information. "
        "Do not make up any information.\n\n"
        "Company Knowledge Base:\n"
        f"{context}\n\n"
        f"User Query: {user_query}\n\n"
        "Answer:"
    )
    return prompt

def get_llm_response(prompt: str, model: str = None, temperature: float = None) -> str:
    """
    LLM으로부터 응답을 가져옵니다.

    Args:
        prompt: LLM에 전달할 프롬프트
        model: 사용할 모델 (기본값: settings.OPENAI_CHAT_MODEL)
        temperature: 생성 온도 (기본값: settings.OPENAI_TEMPERATURE)

    Returns:
        LLM 응답 텍스트
    """
    if model is None:
        model = settings.OPENAI_CHAT_MODEL
    if temperature is None:
        temperature = settings.OPENAI_TEMPERATURE

    try:
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error getting response from LLM: {e}", exc_info=True)
        return f"Error getting response from LLM: {e}"

def rag_query(user_query: str, n_results: int = None) -> str:
    """
    RAG 쿼리 실행: 문서를 검색하고 LLM 응답을 생성합니다.

    Args:
        user_query: 사용자 질문
        n_results: 검색할 문서 개수 (기본값: settings.RETRIEVAL_TOP_K)

    Returns:
        LLM 응답
    """
    retrieved_docs = retrieve_documents(user_query, n_results=n_results)

    if not retrieved_docs:
        return "I could not find any relevant information in the company knowledge base for your query."

    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt)

    return llm_response

if __name__ == "__main__":
    logger.info("Company LLM RAG System ready. Type 'exit' to quit.")
    while True:
        try:
            query = input("\nEnter your query: ")
            if query.lower() == 'exit':
                break

            logger.debug(f"Processing query: {query}")
            response = rag_query(query)
            print("\nLLM Response:")
            print(response)
        except EOFError:
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)

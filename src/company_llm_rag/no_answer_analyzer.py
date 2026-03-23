"""
답변없음 원인 자동 조사 모듈

is_no_answer=True인 질문에 대해 백그라운드에서 LLM 조사를 수행합니다.
설정의 analyze_no_answer 플래그가 ON일 때만 트리거됩니다.
"""

from company_llm_rag.history_store import save_analysis, set_analysis_pending
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.llm.openai_provider import default_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_ANALYSIS_PROMPT = """\
사용자가 다음 질문을 했지만 AI가 답변을 찾지 못했습니다.

[사용자 질문]
{question}

[검색된 관련 문서 {n}건]
{docs}

위 검색 결과를 바탕으로 다음을 간결하게 분석해주세요:

1. **답변 불가 이유**: 검색된 문서들이 질문에 답하기에 충분하지 않은 이유
2. **부족한 정보**: 어떤 정보가 데이터베이스에 없거나 부족한지
3. **개선 제안**: 향후 답변 품질 개선을 위해 어떤 데이터를 추가 수집하면 좋을지

분석은 한국어로 작성해주세요.
"""


async def analyze_no_answer(record_id: int, question: str) -> None:
    """
    백그라운드에서 답변없음 원인을 LLM으로 조사하고 DB에 저장합니다.

    Args:
        record_id: query_history 레코드 ID
        question: 답변하지 못한 사용자 질문
    """
    try:
        set_analysis_pending(record_id)
        logger.info(f"[NoAnswerAnalyzer] 조사 시작 record_id={record_id}")

        # 실제 검색 수행 (더 많은 문서 확인)
        docs = retrieve_documents(question, n_results=10)

        if docs:
            doc_lines = []
            for i, doc in enumerate(docs, 1):
                meta = doc.get("metadata", {})
                source = meta.get("source", "unknown")
                title = meta.get("title", "제목 없음")
                preview = (doc.get("content") or "")[:200].replace("\n", " ")
                doc_lines.append(f"{i}. [{source}] {title}\n   {preview}")
            docs_text = "\n\n".join(doc_lines)
        else:
            docs_text = "관련 문서를 찾을 수 없습니다."

        prompt = _ANALYSIS_PROMPT.format(
            question=question,
            n=len(docs),
            docs=docs_text,
        )

        analysis = default_llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

        save_analysis(record_id, analysis, status="done")
        logger.info(f"[NoAnswerAnalyzer] 조사 완료 record_id={record_id}")

    except Exception as e:
        logger.error(f"[NoAnswerAnalyzer] 조사 실패 record_id={record_id}: {e}", exc_info=True)
        save_analysis(record_id, f"조사 중 오류 발생: {e}", status="error")

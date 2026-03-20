import json
from typing import List, Dict

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

def _source_boost(metadata: Dict) -> float:
    """
    소스 타입에 따라 distance에 곱할 부스트 가중치를 반환합니다.
    값이 낮을수록 순위가 높아집니다 (distance 기반).
    가중치는 config.SOURCE_BOOST_WEIGHTS에서 읽습니다.
    """
    source = metadata.get("source", "")
    mime_type = metadata.get("mime_type", "")
    weights = settings.SOURCE_BOOST_WEIGHTS

    # SharePoint: 문서 파일(PDF/PPTX/DOCX)은 sharepoint 가중치 적용
    if source == "sharepoint" and mime_type not in _DOCUMENT_MIME_TYPES:
        return 1.0  # 일반 파일은 부스트 없음
    return weights.get(source, 1.0)


def retrieve_documents(
    query: str,
    n_results: int = None,
    source_filter: List[str] = None,
    url_extensions: List[str] = None,
) -> List[Dict]:
    """
    ChromaDB에서 사용자 쿼리와 관련된 문서 청크를 검색합니다.

    Args:
        query: 검색 쿼리
        n_results: 반환할 결과 개수 (기본값: settings.RETRIEVAL_TOP_K)
        source_filter: 검색할 소스 목록 (예: ['jira', 'confluence'])
        url_extensions: URL 확장자 필터 (예: ['.xlsx', '.pptx'])

    Returns:
        검색된 문서 리스트
    """
    if n_results is None:
        n_results = settings.RETRIEVAL_TOP_K

    try:
        collection = db_manager.get_collection()
        # 재정렬을 위해 더 많은 후보를 가져옴
        fetch_n = n_results * 3

        # ChromaDB where 절 구성 (소스 필터)
        where = None
        if source_filter and len(source_filter) == 1:
            where = {"source": source_filter[0]}
        elif source_filter and len(source_filter) > 1:
            where = {"$or": [{"source": s} for s in source_filter]}

        query_kwargs = dict(
            query_texts=[query],
            n_results=fetch_n,
            include=['documents', 'metadatas', 'distances'],
        )
        if where:
            query_kwargs["where"] = where

        results = collection.query(**query_kwargs)

        candidates = []
        if results and results['documents'] and len(results['documents']) > 0:
            for i in range(len(results['documents'][0])):
                doc_content = results['documents'][0][i]
                metadata = results['metadatas'][0][i]
                distance = results['distances'][0][i]

                # Reconstruct comments/replies if they were stringified in metadata
                if "comments" in metadata and isinstance(metadata["comments"], str):
                    try:
                        metadata["comments"] = json.loads(metadata["comments"])
                    except json.JSONDecodeError:
                        pass
                if "replies" in metadata and isinstance(metadata["replies"], str):
                    try:
                        metadata["replies"] = json.loads(metadata["replies"])
                    except json.JSONDecodeError:
                        pass

                boosted_distance = distance * _source_boost(metadata)
                candidates.append({
                    "content": doc_content,
                    "metadata": metadata,
                    "_distance": distance,
                    "_boosted_distance": boosted_distance,
                })

        # 부스트 적용 후 재정렬하여 상위 n_results 반환
        # URL 확장자 후처리 필터
        if url_extensions:
            candidates = [
                c for c in candidates
                if any((c["metadata"].get("url") or "").lower().split("?")[0].endswith(ext)
                       for ext in url_extensions)
            ]

        candidates.sort(key=lambda x: x["_boosted_distance"])
        retrieved_docs = []
        for c in candidates[:n_results]:
            retrieved_docs.append({
                "content": c["content"],
                "metadata": c["metadata"],
                "_distance": c["_boosted_distance"],
            })
        return retrieved_docs
    except Exception as e:
        logger.error(f"Error during document retrieval: {e}", exc_info=True)
        return []

if __name__ == "__main__":
    stats = db_manager.get_collection_stats()
    logger.info(f"Retrieval module connected to ChromaDB collection: {stats['name']}")
    logger.info(f"  - Path: {stats['path']}")
    logger.info(f"  - Documents: {stats['count']}")

    while True:
        try:
            user_query = input("\nEnter your query (or 'exit' to quit): ")
            if user_query.lower() == 'exit':
                break

            results = retrieve_documents(user_query)
            if results:
                print("\n--- Retrieved Documents ---")
                for i, doc in enumerate(results):
                    print(f"Document {i+1}:")
                    print(f"  Content (chunk): {doc['content'][:200]}...")
                    print(f"  Source: {doc['metadata'].get('source')}")
                    print(f"  Title: {doc['metadata'].get('title')}")
                    print(f"  URL: {doc['metadata'].get('url')}")
                    print(f"  Original Doc ID: {doc['metadata'].get('original_doc_id')}")
                    print("-" * 30)
            else:
                logger.warning("No relevant documents found.")
        except EOFError:
            break
    logger.info("Exiting retrieval module.")

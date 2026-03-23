import json
import re
from typing import List, Dict, Set

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# 검색 의미 없는 불용어
_STOPWORDS: Set[str] = {
    "찾아줘", "알려줘", "보여줘", "찾아봐", "알아봐",
    "있어", "없어", "어떻게", "어디서", "어디에", "언제",
    "뭐야", "뭔가", "무엇", "어떤", "어디",
    "관련해서", "관련된", "관련", "대해서", "대한",
    "연동건", "이슈", "건", "것", "거",
}


def _extract_keywords(query: str) -> List[str]:
    """쿼리에서 검색에 유효한 핵심 키워드를 추출합니다."""
    words = re.findall(r'[가-힣A-Za-z0-9]+', query)
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def _keyword_search(
    collection,
    keywords: List[str],
    n: int,
    where: Dict = None,
) -> List[Dict]:
    """
    ChromaDB $contains 를 이용한 키워드 매칭 검색.
    각 키워드로 검색 후 합산, 중복 제거하여 반환합니다.
    """
    seen_ids: Set[str] = set()
    results: List[Dict] = []

    for kw in keywords:
        try:
            get_kwargs = dict(
                where_document={"$contains": kw},
                limit=n,
                include=['documents', 'metadatas'],
            )
            if where:
                get_kwargs["where"] = where
            res = collection.get(**get_kwargs)
            for i, doc_id in enumerate(res['ids']):
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    results.append({
                        '_id': doc_id,
                        'content': res['documents'][i],
                        'metadata': res['metadatas'][i],
                    })
        except Exception as e:
            logger.debug(f"키워드 검색 실패 ({kw}): {e}")

    return results


def _rrf_score(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion 점수. 순위가 낮을수록 높은 점수."""
    return 1.0 / (k + rank + 1)


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


def _fix_metadata(metadata: Dict) -> Dict:
    """JSON 문자열로 저장된 comments/replies 필드를 복원합니다."""
    for key in ("comments", "replies"):
        if key in metadata and isinstance(metadata[key], str):
            try:
                metadata[key] = json.loads(metadata[key])
            except json.JSONDecodeError:
                pass
    return metadata


def retrieve_documents(
    query: str,
    n_results: int = None,
    source_filter: List[str] = None,
    url_extensions: List[str] = None,
) -> List[Dict]:
    """
    ChromaDB에서 하이브리드 검색(벡터 + 키워드 RRF)으로 관련 문서를 검색합니다.

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
        fetch_n = n_results * 3

        # ChromaDB where 절 구성 (소스 필터)
        where = None
        if source_filter and len(source_filter) == 1:
            where = {"source": source_filter[0]}
        elif source_filter and len(source_filter) > 1:
            where = {"$or": [{"source": s} for s in source_filter]}

        # ── 1. 벡터 검색 ──────────────────────────────────────────
        query_kwargs = dict(
            query_texts=[query],
            n_results=fetch_n,
            include=['documents', 'metadatas', 'distances'],
        )
        if where:
            query_kwargs["where"] = where

        vector_results = collection.query(**query_kwargs)

        # id → {content, metadata, vector_rank} 맵
        doc_map: Dict[str, Dict] = {}
        vector_rank_map: Dict[str, int] = {}

        if vector_results and vector_results['documents']:
            for rank, i in enumerate(range(len(vector_results['documents'][0]))):
                doc_id = vector_results['ids'][0][i]
                metadata = _fix_metadata(vector_results['metadatas'][0][i])
                distance = vector_results['distances'][0][i]
                boosted = distance * _source_boost(metadata)
                doc_map[doc_id] = {
                    'content': vector_results['documents'][0][i],
                    'metadata': metadata,
                    '_distance': boosted,
                }
                vector_rank_map[doc_id] = rank

        # ── 2. 키워드 검색 ────────────────────────────────────────
        keywords = _extract_keywords(query)
        keyword_results = _keyword_search(collection, keywords, fetch_n, where)

        keyword_rank_map: Dict[str, int] = {}
        for rank, item in enumerate(keyword_results):
            doc_id = item['_id']
            keyword_rank_map[doc_id] = rank
            if doc_id not in doc_map:
                doc_map[doc_id] = {
                    'content': item['content'],
                    'metadata': _fix_metadata(item['metadata']),
                    '_distance': 1.0,  # 벡터 점수 없으면 최대 distance
                }

        if keywords:
            logger.debug(f"하이브리드 검색 키워드: {keywords} | 벡터 후보: {len(vector_rank_map)} | 키워드 후보: {len(keyword_rank_map)}")

        # ── 3. RRF 융합 ───────────────────────────────────────────
        all_ids = set(vector_rank_map) | set(keyword_rank_map)
        scored: List[Dict] = []
        for doc_id in all_ids:
            v_score = _rrf_score(vector_rank_map[doc_id]) if doc_id in vector_rank_map else 0.0
            k_score = _rrf_score(keyword_rank_map[doc_id]) if doc_id in keyword_rank_map else 0.0
            rrf = v_score + k_score
            scored.append({**doc_map[doc_id], '_rrf': rrf})

        scored.sort(key=lambda x: x['_rrf'], reverse=True)

        # ── 4. 후처리 필터 & 반환 ─────────────────────────────────
        if url_extensions:
            scored = [
                c for c in scored
                if any((c["metadata"].get("url") or "").lower().split("?")[0].endswith(ext)
                       for ext in url_extensions)
            ]

        return [
            {"content": c["content"], "metadata": c["metadata"], "_distance": c["_distance"]}
            for c in scored[:n_results]
        ]

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
                    print("-" * 30)
            else:
                logger.warning("No relevant documents found.")
        except EOFError:
            break
    logger.info("Exiting retrieval module.")

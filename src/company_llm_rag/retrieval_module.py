import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

# 검색 의미 없는 불용어 (일반적이거나 검색 가치가 낮은 단어)
_STOPWORDS: Set[str] = {
    # 요청 동사
    "찾아줘", "알려줘", "보여줘", "찾아봐", "알아봐", "정리해줘",
    "확인해줘", "알려주세요", "찾아주세요", "보여주세요",
    # 일반 의문사
    "있어", "없어", "어떻게", "어디서", "어디에", "언제",
    "뭐야", "뭔가", "무엇", "어떤", "어디",
    # 관계어
    "관련해서", "관련된", "관련", "대해서", "대한",
    "이슈", "것", "거",
    # 빈출 일반명사 (검색 노이즈)
    "내용", "관련내용", "정보", "자료", "문서",
    "진행", "진행된", "진행했던", "진행중인",
    "관련자료", "내역", "현황", "결과",
    # 문법/서술 표현
    "하게된거야", "하게된", "된거야", "왜",
}


_MAX_KEYWORDS = 3  # 키워드 검색 최대 개수 (많을수록 $contains 풀스캔 반복)

# 한국어 조사/어미 (길이 긴 것 먼저 매칭해야 올바르게 제거됨)
_KO_SUFFIXES = sorted([
    "에서부터", "에서는", "에서도", "에서의", "에서만",
    "이었던", "이라는", "이라고", "이지만", "이므로", "이면서",
    "으로서", "으로의", "대로의",
    "에게서", "에서",
    "부터", "까지", "라는", "라고", "지만", "므로", "면서",
    "로서", "로의",
    "았던", "었던",
    "이야", "이에요", "이에서",
    "를", "을", "은", "는", "이", "가", "의", "에", "도", "와", "과", "건",
], key=len, reverse=True)

_KEYWORD_ONLY_DISCOUNT = 0.5  # 키워드 전용 결과(벡터 미매칭) RRF 할인율

# 최신성 부스트 설정
_RECENCY_HALF_LIFE_DAYS = 30   # 30일마다 점수 절반 감소 (일반)
_RECENCY_JIRA_SCALE = 0.5      # Jira는 절반 반감기 → 더 빠르게 감소 (오래된 이슈 억제)


def _recency_score(metadata: Dict, jira_scale: bool = False) -> float:
    """created_at 기반 최신성 점수 (0~1, 최신일수록 높음).
    날짜 없는 문서는 중립값(0.5) 반환.
    """
    date_str = metadata.get("created_at") or metadata.get("updated_at") or ""
    if not date_str:
        return 0.5
    try:
        dt = datetime.fromisoformat(date_str.rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_ago = max((now - dt).days, 0)
        half_life = _RECENCY_HALF_LIFE_DAYS * (_RECENCY_JIRA_SCALE if jira_scale else 1.0)
        return math.exp(-days_ago * math.log(2) / half_life)
    except Exception:
        return 0.5


def _strip_ko_suffix(word: str) -> str:
    """한국어 조사/어미를 제거합니다. 제거 후 2자 미만이면 원본 반환."""
    for suffix in _KO_SUFFIXES:
        if word.endswith(suffix):
            stem = word[:-len(suffix)]
            return stem if len(stem) >= 2 else word
    return word


def _extract_keywords(query: str) -> List[str]:
    """쿼리에서 검색에 유효한 핵심 키워드를 추출합니다. 최대 _MAX_KEYWORDS개."""
    # Jira 이슈 키 패턴 우선 추출 (예: WMPO-10564, ERROR-42) — 하이픈 포함 통째로 유지
    jira_keys = re.findall(r'[A-Z]+-\d+', query)
    jira_key_set = set(jira_keys)

    words = re.findall(r'[가-힣A-Za-z0-9]+', query)
    # 조사 제거 (Jira 키는 건드리지 않음)
    stripped = []
    for w in words:
        if w in jira_key_set:
            continue
        # 한글/영문으로 시작하지 않는 토큰 스킵 (버전 파편 "0의" 등)
        if not re.match(r'[가-힣a-zA-Z]', w):
            continue
        s = _strip_ko_suffix(w)
        # 조사 제거 후 한글/영문 없으면 스킵 (숫자 파편 등)
        if len(s) >= 2 and s not in _STOPWORDS and re.search(r'[가-힣a-zA-Z]', s):
            stripped.append(s)

    # 긴 단어일수록 고유성이 높음 → 길이 내림차순으로 상위 N개만 사용
    stripped.sort(key=len, reverse=True)

    # Jira 키 우선, 나머지는 일반 키워드로 채움 (Jira 키 구성 파편 제외)
    jira_parts = {part for key in jira_keys for part in key.split('-')}
    remaining = [w for w in stripped if w not in jira_parts and w not in jira_keys]
    result = jira_keys + remaining
    return result[:_MAX_KEYWORDS]


def _keyword_search(
    collection,
    keywords: List[str],
    n: int,
    where: Dict = None,
) -> List[Dict]:
    """
    SQLite FTS5 역인덱스를 이용한 키워드 검색 (O(log N)).
    FTS 인덱스가 비어있으면 ChromaDB $contains로 fallback합니다.
    """
    if not keywords:
        return []

    from company_llm_rag.fts_store import fts_search, fts_exists

    if not fts_exists():
        logger.warning("FTS 인덱스가 비어있음 — $contains 폴백. 데이터 재수집 후 FTS가 자동 구축됩니다.")
        return _contains_keyword_search(collection, keywords, n, where)

    # FTS5로 BM25 점수 순 chunk_id 획득
    chunk_ids = fts_search(keywords, limit=n)
    if not chunk_ids:
        return []

    # ChromaDB에서 해당 ID의 content + metadata 조회
    try:
        get_kwargs = dict(ids=chunk_ids, include=['documents', 'metadatas'])
        if where:
            get_kwargs['where'] = where
        res = collection.get(**get_kwargs)
        return [
            {'_id': doc_id, 'content': res['documents'][i], 'metadata': res['metadatas'][i]}
            for i, doc_id in enumerate(res['ids'])
        ]
    except Exception as e:
        logger.debug(f"FTS 후 ChromaDB 조회 실패: {e}")
        return []


def _contains_keyword_search(
    collection,
    keywords: List[str],
    n: int,
    where: Dict = None,
) -> List[Dict]:
    """$contains 기반 키워드 검색 (FTS 미구축 시 fallback). 병렬 실행."""
    seen_ids: Set[str] = set()
    results: List[Dict] = []

    def _search_one(kw: str) -> List[Dict]:
        try:
            get_kwargs = dict(where_document={"$contains": kw}, limit=n, include=['documents', 'metadatas'])
            if where:
                get_kwargs["where"] = where
            res = collection.get(**get_kwargs)
            return [
                {'_id': doc_id, 'content': res['documents'][i], 'metadata': res['metadatas'][i]}
                for i, doc_id in enumerate(res['ids'])
            ]
        except Exception as e:
            logger.debug(f"$contains 검색 실패 ({kw}): {e}")
            return []

    with ThreadPoolExecutor(max_workers=len(keywords)) as executor:
        for items in executor.map(_search_one, keywords):
            for item in items:
                if item['_id'] not in seen_ids:
                    seen_ids.add(item['_id'])
                    results.append(item)

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
    return_timing: bool = False,
    return_scores: bool = False,
    recency_boost: bool = False,
):
    """
    ChromaDB에서 하이브리드 검색(벡터 + 키워드 RRF)으로 관련 문서를 검색합니다.

    Args:
        query: 검색 쿼리
        n_results: 반환할 결과 개수 (기본값: settings.RETRIEVAL_TOP_K)
        source_filter: 검색할 소스 목록 (예: ['jira', 'confluence'])
        url_extensions: URL 확장자 필터 (예: ['.xlsx', '.pptx'])
        return_timing: True이면 (docs, timing) 튜플 반환

    Returns:
        검색된 문서 리스트, 또는 return_timing=True이면 (리스트, timing dict)

    Note:
        recency_boost=True이면 RRF 점수에 created_at 기반 최신성 가중치를 곱합니다.
        Jira 소스는 반감기가 절반으로 더 강하게 적용됩니다.
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

        t0 = time.monotonic()

        # ── 1. 벡터 검색 ──────────────────────────────────────────
        query_kwargs = dict(
            query_texts=[query],
            n_results=fetch_n,
            include=['documents', 'metadatas', 'distances'],
        )
        if where:
            query_kwargs["where"] = where

        vector_results = collection.query(**query_kwargs)
        t_vector = time.monotonic()

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
        t_keyword = time.monotonic()

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

        vector_ms  = int((t_vector - t0) * 1000)
        keyword_ms = int((t_keyword - t_vector) * 1000)
        logger.info(
            f"[검색 성능] 벡터={vector_ms}ms | "
            f"키워드({len(keywords)}개)={keyword_ms}ms | "
            f"벡터후보={len(vector_rank_map)} 키워드후보={len(keyword_rank_map)}"
        )

        # ── 3. RRF 융합 ───────────────────────────────────────────
        hub_teams = set(settings.TEAMS_KNOWLEDGE_HUB_TEAMS)
        hub_rrf_boost = settings.BOOST_KNOWLEDGE_HUB_RRF

        all_ids = set(vector_rank_map) | set(keyword_rank_map)
        scored: List[Dict] = []
        for doc_id in all_ids:
            v_rank = vector_rank_map.get(doc_id)
            k_rank = keyword_rank_map.get(doc_id)
            v_score = _rrf_score(v_rank) if v_rank is not None else 0.0
            k_score = _rrf_score(k_rank) if k_rank is not None else 0.0
            if v_rank is None and k_rank is not None:
                rrf = k_score * _KEYWORD_ONLY_DISCOUNT
            else:
                rrf = v_score + k_score
            # Knowledge Hub 팀 문서 우선순위 부스트
            if hub_teams:
                team = doc_map[doc_id].get('metadata', {}).get('teams_team_name', '')
                if team in hub_teams:
                    rrf *= hub_rrf_boost
            # 최신성 부스트: created_at 기반 가중치 적용
            if recency_boost:
                meta = doc_map[doc_id].get('metadata', {})
                is_jira = meta.get('source', '') == 'jira'
                r_score = _recency_score(meta, jira_scale=is_jira)
                rrf = rrf * (1.0 + r_score)
            scored.append({**doc_map[doc_id], '_rrf': rrf, '_doc_id': doc_id,
                           '_vector_rank': v_rank, '_keyword_rank': k_rank})

        scored.sort(key=lambda x: x['_rrf'], reverse=True)

        # ── 4. 후처리 필터 & 반환 ─────────────────────────────────
        if url_extensions:
            scored = [
                c for c in scored
                if any((c["metadata"].get("url") or "").lower().split("?")[0].endswith(ext)
                       for ext in url_extensions)
            ]

        if return_scores:
            docs = [
                {
                    "content": c["content"],
                    "metadata": c["metadata"],
                    "_distance": c["_distance"],
                    "_rrf": round(c["_rrf"], 6),
                    "_vector_rank": c["_vector_rank"],
                    "_keyword_rank": c["_keyword_rank"],
                }
                for c in scored[:n_results]
            ]
        else:
            docs = [
                {"content": c["content"], "metadata": c["metadata"], "_distance": c["_distance"]}
                for c in scored[:n_results]
            ]

        if return_timing:
            timing = {"vector_ms": vector_ms, "keyword_ms": keyword_ms}
            return docs, timing
        return docs

    except Exception as e:
        logger.error(f"Error during document retrieval: {e}", exc_info=True)
        return ([], {"vector_ms": 0, "keyword_ms": 0}) if return_timing else []


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

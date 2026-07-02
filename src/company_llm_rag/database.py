"""
ChromaDB 데이터베이스 관리 모듈

ChromaDB 초기화 및 컬렉션 관리를 담당합니다.
"""

import threading
import time

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils import embedding_functions
from chromadb.api.models.Collection import Collection

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

# ChromaDB segment 캐시 메모리 한도 (Issue #45)
# - 한도 초과 시 LRU로 가장 오래된 segment 제거 후 재로드
# - 쿼리/분석 시 RSS 스파이크 방지
_CHROMA_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB


class ChromaDBManager:
    """ChromaDB 관리 클래스"""

    def __init__(self):
        """ChromaDB 클라이언트 초기화"""
        self._client = None
        self._collection = None
        self._embedding_function = None
        self._keepalive_started = False

    @property
    def client(self) -> chromadb.PersistentClient:
        """ChromaDB 클라이언트 (Lazy initialization)"""
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=settings.CHROMA_DB_PATH,
                settings=ChromaSettings(
                    chroma_segment_cache_policy="LRU",
                    chroma_memory_limit_bytes=_CHROMA_MEMORY_LIMIT_BYTES,
                ),
            )
        return self._client

    @property
    def embedding_function(self) -> embedding_functions.OpenAIEmbeddingFunction:
        """OpenAI 임베딩 함수 (Lazy initialization)"""
        if self._embedding_function is None:
            if not settings.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY is not set")

            self._embedding_function = embedding_functions.OpenAIEmbeddingFunction(
                api_key=settings.OPENAI_API_KEY,
                model_name=settings.OPENAI_EMBEDDING_MODEL
            )
        return self._embedding_function

    def get_collection(self, create_if_not_exists: bool = True) -> Collection:
        """
        ChromaDB 컬렉션 가져오기

        Args:
            create_if_not_exists: 컬렉션이 없으면 생성할지 여부

        Returns:
            ChromaDB Collection 객체
        """
        if self._collection is None:
            try:
                if create_if_not_exists:
                    self._collection = self.client.get_or_create_collection(
                        name=settings.COLLECTION_NAME,
                        embedding_function=self.embedding_function
                    )
                else:
                    self._collection = self.client.get_collection(
                        name=settings.COLLECTION_NAME,
                        embedding_function=self.embedding_function
                    )
            except Exception as e:
                raise RuntimeError(f"Failed to get ChromaDB collection: {e}")

        return self._collection

    def reset_collection(self) -> Collection:
        """컬렉션 초기화 (기존 컬렉션 삭제 후 재생성)"""
        try:
            self.client.delete_collection(name=settings.COLLECTION_NAME)
        except Exception:
            pass  # 컬렉션이 없는 경우 무시

        self._collection = None
        return self.get_collection(create_if_not_exists=True)

    def get_collection_stats(self) -> dict:
        """컬렉션 통계 정보 조회"""
        collection = self.get_collection()
        count = collection.count()

        return {
            "name": settings.COLLECTION_NAME,
            "count": count,
            "path": settings.CHROMA_DB_PATH
        }

    def start_keepalive(self) -> None:
        """
        벡터 인덱스 keep-alive 데몬 스레드를 시작합니다 (#54).

        유휴 후 HNSW 인덱스(data_level0.bin)가 OS page cache/LRU에서 축출되어
        첫 질의에서 콜드 page-in(최대 수십 초)이 발생하는 것을 방지합니다.
        주기마다 query_embeddings(캐시된 실벡터)로 top-1 탐색을 수행해 hot-path
        페이지를 상주 유지하며, OpenAI 임베딩은 호출하지 않습니다(비용 0).
        web 프로세스에서 1회만 호출하세요.
        """
        interval = settings.INDEX_KEEPALIVE_SECONDS
        if interval <= 0 or self._keepalive_started:
            return
        self._keepalive_started = True
        t = threading.Thread(
            target=self._keepalive_loop, args=(interval,),
            daemon=True, name="chroma-keepalive",
        )
        t.start()
        logger.info(f"[Keepalive] 벡터 인덱스 keep-alive 시작 (주기 {interval}s)")

    def _keepalive_loop(self, interval: int) -> None:
        warm_vec = None
        while True:
            time.sleep(interval)
            try:
                collection = self.get_collection()
                if warm_vec is None:
                    # 최초 1회: 저장된 실제 임베딩 벡터 확보 (OpenAI 호출 없음)
                    res = collection.get(limit=1, include=["embeddings"])
                    embs = res.get("embeddings")
                    if embs is not None and len(embs) > 0:
                        warm_vec = [float(x) for x in embs[0]]
                if warm_vec is not None:
                    collection.query(query_embeddings=[warm_vec], n_results=1)
                    logger.debug("[Keepalive] HNSW 인덱스 워밍 완료")
            except Exception as e:
                logger.debug(f"[Keepalive] 워밍 실패(무시): {e}")


# 싱글톤 인스턴스
db_manager = ChromaDBManager()

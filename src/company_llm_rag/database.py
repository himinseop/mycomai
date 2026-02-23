"""
ChromaDB 데이터베이스 관리 모듈

ChromaDB 초기화 및 컬렉션 관리를 담당합니다.
"""

import chromadb
from chromadb.utils import embedding_functions
from chromadb.api.models.Collection import Collection

from company_llm_rag.config import settings


class ChromaDBManager:
    """ChromaDB 관리 클래스"""

    def __init__(self):
        """ChromaDB 클라이언트 초기화"""
        self._client = None
        self._collection = None
        self._embedding_function = None

    @property
    def client(self) -> chromadb.PersistentClient:
        """ChromaDB 클라이언트 (Lazy initialization)"""
        if self._client is None:
            self._client = chromadb.PersistentClient(path=settings.CHROMA_DB_PATH)
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


# 싱글톤 인스턴스
db_manager = ChromaDBManager()

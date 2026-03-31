"""
BAAI/bge-reranker-v2-m3 기반 Reranker 구현체

sentence-transformers CrossEncoder로 동작합니다.
모델은 첫 rerank 호출 시 lazy하게 로드됩니다.
"""

from typing import Dict, List

from company_llm_rag.logger import get_logger
from company_llm_rag.reranker.base import RerankerProvider

logger = get_logger(__name__)


class BGEReranker(RerankerProvider):
    """BAAI/bge-reranker-v2-m3 구현체 (sentence-transformers CrossEncoder)."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self._model_name = model_name
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):
        if self._model is not None:
            return
        logger.info(f"[Reranker] 모델 로딩 중: {self._model_name}")
        from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(self._model_name)
        logger.info(f"[Reranker] 모델 로딩 완료")

    def rerank(self, query: str, docs: List[Dict], top_n: int) -> List[Dict]:
        if not docs:
            return docs
        self._load()
        pairs = [(query, doc.get('content', '')) for doc in docs]
        scores = self._model.predict(pairs).tolist()
        for doc, score in zip(docs, scores):
            doc['_rerank_score'] = round(score, 6)
        docs.sort(key=lambda x: x.get('_rerank_score', 0), reverse=True)
        logger.debug(
            f"[Reranker] {len(pairs)}개 문서 재정렬 완료 | "
            f"top1={docs[0].get('_rerank_score', 0):.4f} "
            f"topN={docs[min(top_n-1, len(docs)-1)].get('_rerank_score', 0):.4f}"
        )
        return docs[:top_n]

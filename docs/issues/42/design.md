# Issue #42: Reranker 도입으로 검색 품질 개선

GitHub Issue: https://github.com/himinseop/mycomai/issues/42

## 배경

### 현재 문제

현재 RAG 파이프라인은 벡터 검색(bi-encoder) + FTS5 키워드 검색 → RRF 융합으로 결과를 정렬한다.

bi-encoder는 질문과 문서를 **독립적으로** 임베딩한 뒤 거리를 비교하므로, 다음 상황에서 품질 한계가 있다:

- 질문과 문서의 **표현이 다르지만 의미가 같은** 경우 (예: "지역 타겟 푸시" ↔ "특정 지역 알림 발송")
- **짧은 질문**에서 핵심 의도를 파악하기 어려운 경우
- 동일 주제의 여러 문서 중 **가장 관련 있는 문서**를 구분해야 하는 경우

실제 사례: "지역 타겟 푸시 발송의 개발 내용을 알려줘"
- 핵심 문서 WMPO-6225가 distance 0.50으로 5위에 밀림
- 관련도 75~82% 범위의 문서 10개가 유사한 거리에 몰려 변별력 부족

### 해결 방향

cross-encoder 기반 reranker를 1차 검색 후에 삽입하여, 질문-문서 쌍을 동시에 읽고 관련도를 정밀 판단한다.

```
현재:  벡터(30개) + FTS5(30개) → RRF 융합 → 상위 N개 → LLM
변경:  벡터(30개) + FTS5(30개) → RRF 융합 → 상위 30개 → Reranker → 상위 N개 → LLM
```

## 모델 선정

### 후보 비교

| 모델 | 크기 | 한국어 | CPU 속도 | 비용 |
|------|------|--------|----------|------|
| **BAAI/bge-reranker-v2-m3** | 568MB | 우수 | 100-300ms/쌍 | 무료 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 80MB | 미흡 | 50-100ms/쌍 | 무료 |
| Cohere Rerank API | 0MB | 양호 | 빠름 | 1,000건/월 무료 |
| dragonkue/bge-reranker-v2-m3-ko | 568MB | 양호 | 100-300ms/쌍 | 무료 |

### 선정: BAAI/bge-reranker-v2-m3

선정 이유:
- **한국어 품질**: 다국어 데이터로 학습, MTEB 한국어 벤치마크 상위
- **ONNX 지원**: PyTorch 없이 ONNX Runtime만으로 동작 → Docker 이미지 증가 최소화
- **커뮤니티**: 가장 널리 사용되는 오픈소스 reranker, 문서/사례 풍부
- **비용**: 완전 무료, 외부 API 의존 없음

### 리소스 영향

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| Docker 이미지 | ~1.5GB | ~2.3GB (+800MB) |
| 메모리 | ~500MB | ~1.2GB (+700MB) |
| 응답 지연 | 2-4초 | 3-7초 (+1-3초) |
| 외부 의존성 | OpenAI API | OpenAI API (변경 없음) |

## 아키텍처 설계

### 파이프라인

```
[사용자 질문]
    ↓
[retrieval_module.py]
    ├─ 벡터 검색: ChromaDB query (fetch_n = n_results * 3)
    ├─ 키워드 검색: FTS5 BM25 (같은 fetch_n)
    ├─ RRF 융합: 벡터+키워드 순위 합산
    ├─ Knowledge Hub 부스트 (5.0x)
    ├─ 최신성 부스트 (선택)
    ├─ ★ Reranker: 상위 후보를 cross-encoder로 재정렬
    └─ 최종 상위 N개 반환
    ↓
[rag_system.py]
    ├─ Hub 직접 응답 판정
    ├─ 프롬프트 조립
    ├─ LLM 호출
    └─ 인용 치환 + 참고문서 구성
```

### 삽입 위치

`retrieval_module.py` — RRF 정렬 직후, 최종 슬라이싱 전 (현재 line 350 부근)

```python
# 기존: RRF 정렬 후 바로 슬라이싱
scored.sort(key=lambda x: x['_rrf'], reverse=True)

# 변경: RRF 정렬 → Reranker → 슬라이싱
scored.sort(key=lambda x: x['_rrf'], reverse=True)
if reranker_enabled:
    scored = rerank(query, scored[:reranker_top_n])
```

### 환경변수

```bash
# Reranker 설정
RERANKER_ENABLED=true                          # 활성화 여부 (기본: false)
RERANKER_MODEL=BAAI/bge-reranker-v2-m3         # 모델명
RERANKER_TOP_N=20                              # Rerank 대상 후보 수 (기본: 20)
```

- `RERANKER_ENABLED=false`면 기존 RRF 파이프라인 그대로 동작 (무변경)
- `RERANKER_TOP_N`은 reranker에 넣을 후보 수 (많을수록 정확, 느림)

### 모듈 설계

LLM 추상화(`llm/base.py` → `llm/openai_provider.py`)와 동일한 패턴으로 설계.
향후 다른 reranker 라이브러리(Cohere, sentence-transformers 등)로 교체 가능.

```
src/company_llm_rag/reranker/
├── __init__.py
├── base.py              # RerankerProvider 추상 인터페이스
├── bge_provider.py      # BAAI/bge-reranker-v2-m3 구현체 (FlagEmbedding + ONNX)
└── factory.py           # 환경변수 기반 provider 생성
```

#### `reranker/base.py` — 추상 인터페이스

```python
from abc import ABC, abstractmethod
from typing import Dict, List


class RerankerProvider(ABC):
    """Reranker 추상 인터페이스. 구현체 교체 시 이 계약만 따르면 됨."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """현재 사용 중인 모델명."""

    @abstractmethod
    def rerank(self, query: str, docs: List[Dict], top_n: int) -> List[Dict]:
        """질문-문서 쌍의 관련도를 계산하여 재정렬합니다.

        Args:
            query: 사용자 질문
            docs: 검색 결과 리스트 (content, metadata 포함)
            top_n: 반환할 상위 문서 수

        Returns:
            재정렬된 상위 top_n개 문서 (_rerank_score 필드 추가됨)
        """
```

#### `reranker/bge_provider.py` — BGE 구현체

```python
from company_llm_rag.reranker.base import RerankerProvider


class BGEReranker(RerankerProvider):
    """BAAI/bge-reranker-v2-m3 기반 구현체 (FlagEmbedding + ONNX)."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self._model_name = model_name
        self._model = None  # lazy init

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):
        if self._model is None:
            from FlagEmbedding import FlagReranker
            self._model = FlagReranker(self._model_name, use_fp16=False)

    def rerank(self, query, docs, top_n):
        self._load()
        pairs = [[query, doc['content']] for doc in docs]
        scores = self._model.compute_score(pairs, normalize=True)
        for doc, score in zip(docs, scores):
            doc['_rerank_score'] = score
        docs.sort(key=lambda x: x['_rerank_score'], reverse=True)
        return docs[:top_n]
```

#### `reranker/factory.py` — Provider 생성

```python
from company_llm_rag.config import settings
from company_llm_rag.reranker.base import RerankerProvider


_instance: RerankerProvider | None = None


def get_reranker() -> RerankerProvider | None:
    """환경변수 기반으로 reranker 인스턴스를 반환합니다. 비활성화 시 None."""
    global _instance
    if not settings.RERANKER_ENABLED:
        return None
    if _instance is None:
        # 향후 RERANKER_PROVIDER 환경변수로 구현체 선택 가능
        from company_llm_rag.reranker.bge_provider import BGEReranker
        _instance = BGEReranker(model_name=settings.RERANKER_MODEL)
    return _instance
```

#### `retrieval_module.py` 변경

```python
# RRF 정렬 후 (현재 line 350)
scored.sort(key=lambda x: x['_rrf'], reverse=True)

# Reranker 적용 (비활성화 시 None → 스킵)
from company_llm_rag.reranker.factory import get_reranker
reranker = get_reranker()
if reranker:
    rerank_candidates = scored[:settings.RERANKER_TOP_N]
    scored = reranker.rerank(query, rerank_candidates, n_results)
```

#### `config.py` 변경

```python
# Reranker 설정
RERANKER_ENABLED: bool = os.getenv("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_MODEL: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_TOP_N: int = int(os.getenv("RERANKER_TOP_N", "20"))
```

#### `requirements.txt` 변경

```
FlagEmbedding>=1.2
onnxruntime>=1.17
```

#### `docker/Dockerfile` 변경

ONNX Runtime은 pip으로 설치되므로 Dockerfile 변경 최소화.
모델 다운로드는 첫 실행 시 자동 (HuggingFace 캐시).

사전 다운로드가 필요하면:
```dockerfile
RUN python -c "from FlagEmbedding import FlagReranker; FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=False)"
```

### 교체 시나리오

다른 reranker로 교체할 때:

1. `reranker/` 하위에 새 구현체 파일 추가 (예: `cohere_provider.py`)
2. `RerankerProvider` 추상 인터페이스 구현
3. `factory.py`에서 환경변수 기반 분기 추가
4. **retrieval_module.py 변경 없음** — factory가 알아서 적절한 구현체 반환

## 성능 예측

### 응답 시간 영향

10개 문서 rerank 기준 (CPU, ONNX):

| 단계 | 현재 | Reranker 추가 후 |
|------|------|-----------------|
| 벡터 검색 | ~2초 | ~2초 |
| FTS5 검색 | ~0.05초 | ~0.05초 |
| RRF 융합 | <0.01초 | <0.01초 |
| Reranker | - | **~1-3초** |
| LLM 응답 | ~2-4초 | ~2-4초 |
| **총 응답** | **~4-6초** | **~5-9초** |

### 검색 품질 기대 효과

- 질문과 의미적으로 가장 관련 높은 문서가 상위에 올라옴
- 참고문서 거리 기준치(_MAX_REF_DISTANCE) 의존도 감소
- LLM 프롬프트에 더 관련성 높은 컨텍스트 제공 → 답변 품질 향상

## sLLM과의 연계

Issue #38 (sLLM 전환)과 병행 시:

```
1단계: 벡터 + FTS5 → RRF → Reranker → GPT 답변       (현재 + reranker)
2단계: 벡터 + FTS5 → RRF → Reranker → sLLM 답변      (GPT → sLLM 전환)
3단계: 로컬 임베딩 + FTS5 → RRF → Reranker → sLLM    (OpenAI 완전 제거)
```

Reranker와 sLLM은 독립적이므로 순서 무관하게 도입 가능.
다만 sLLM의 컨텍스트 윈도우가 GPT보다 작을 수 있으므로, reranker로 상위 문서의 관련도를 높이면 sLLM 전환 시에도 답변 품질 유지에 유리.

## 리스크

| 리스크 | 영향 | 완화 |
|--------|------|------|
| 응답 지연 증가 (+1-3초) | UX 저하 | RERANKER_TOP_N 조절, 비동기 처리 검토 |
| Docker 이미지 +800MB | 배포 시간 증가 | ONNX 사용으로 PyTorch 대비 70% 절감 |
| 모델 다운로드 실패 | 첫 실행 실패 | Dockerfile에서 사전 다운로드 |
| 한국어 성능이 기대 이하 | 개선 미미 | bge-reranker-v2-m3-ko 대안 |
| RERANKER_ENABLED=false 시 회귀 | 기존 파이프라인 영향 | 기능 플래그로 완전 분리 |

## 구현 순서

1. `reranker.py` 모듈 작성 (lazy init + rerank 메서드)
2. `config.py`에 RERANKER_* 환경변수 추가
3. `retrieval_module.py`에 reranker 호출 삽입
4. `requirements.txt`에 FlagEmbedding, onnxruntime 추가
5. Docker 이미지 빌드 테스트
6. 검색 품질 비교 테스트 (같은 질문으로 reranker ON/OFF 비교)
7. `.env.sample` 업데이트

## 검증 기준

- `RERANKER_ENABLED=false`일 때 기존 동작과 완전 동일
- `RERANKER_ENABLED=true`일 때 "지역 타겟 푸시 발송" 질문에서 WMPO-6225가 상위 2위 이내
- Knowledge Hub 직접 응답 경로에 영향 없음
- 응답 시간 증가가 3초 이내

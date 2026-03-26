# Issue #38 OpenAI 생성 모델을 sLLM(`skt/A.X-4.0-Light`)로 전환 가능한 구조 설계

GitHub Issue: https://github.com/himinseop/mycomai/issues/38

## 배경

현재 시스템은 답변 생성과 요약 생성에 OpenAI API를 사용합니다. 이 구조는 구현이 단순하지만, 질의응답 트래픽이 증가할수록 외부 API 비용이 누적되고 운영 환경 제약도 커집니다.

이번 변경의 1차 목표는 "답변 생성 모델"을 sLLM으로 전환해 반복 질의 비용을 낮추는 것입니다. 다만 단순히 특정 모델 이름만 교체하면 이후 다른 로컬 모델, 사내 모델, OpenAI-compatible 서버로 옮길 때 다시 코드를 수정해야 합니다. 따라서 `skt/A.X-4.0-Light`를 기본 타깃으로 삼되, 향후 변경이 쉬운 추상화 구조를 함께 도입합니다.

## 목표

- 답변 생성 경로를 OpenAI에서 sLLM으로 전환할 수 있게 한다.
- 1차 기본 모델은 `skt/A.X-4.0-Light`로 한다.
- 추후 모델명, 엔드포인트, 런타임이 바뀌어도 코드 수정 없이 환경변수 중심으로 전환 가능하게 한다.
- 기존 `LLMProvider` 추상화와 스트리밍 흐름을 유지한다.
- 임베딩은 이번 범위에서 유지하여 벡터 재생성 없이 단계적으로 전환할 수 있게 한다.

## 비목표

- 문서 임베딩 모델 교체
- ChromaDB 재색인
- 프롬프트 전면 재설계
- GPU 서버 구축 자동화
- 여러 sLLM 런타임 동시 지원의 완전 구현

## 현재 구조 요약

### 생성 모델 호출

- [src/company_llm_rag/llm/openai_provider.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/llm/openai_provider.py)
- [src/company_llm_rag/rag_system.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/rag_system.py)
- [src/company_llm_rag/no_answer_analyzer.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/no_answer_analyzer.py)
- [src/company_llm_rag/teams_sender.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/teams_sender.py)

현재 애플리케이션 레이어는 `default_llm.chat()` / `default_llm.stream_chat()` 형태로 LLM 추상화에 의존하고 있어, 상위 사용처 변경 범위는 크지 않습니다.

### 임베딩

- [src/company_llm_rag/database.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/database.py)

임베딩은 OpenAI 전용으로 결합되어 있습니다. 하지만 이번 작업은 생성 모델만 바꾸는 단계이므로 이 부분은 유지합니다.

### 설정

- [src/company_llm_rag/config.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/config.py)

현재는 OpenAI 관련 환경변수가 사실상 전역 기본값 역할을 하며, `validate()`도 `OPENAI_API_KEY`를 필수로 간주합니다.

## 핵심 설계 방향

### 1. `모델 공급자`와 `모델 런타임`을 분리한다

이번 작업의 본질은 "OpenAI에서 SKT 모델로 교체"가 아니라 "생성 모델 호출 대상을 설정으로 바꿀 수 있게 만드는 것"입니다.

권장 구조:

- `LLM_PROVIDER=openai | openai_compatible`
- `LLM_MODEL=<모델명>`
- `LLM_BASE_URL=<엔드포인트>`
- `LLM_API_KEY=<선택>`

여기서 `openai_compatible`은 vLLM, LM Studio, 사내 게이트웨이처럼 OpenAI Chat Completions 호환 API를 제공하는 서버를 의미합니다.

이 방식을 택하면 `skt/A.X-4.0-Light`를 지금은 vLLM 같은 런타임으로 붙이고, 나중에는 다른 모델이나 다른 OpenAI-compatible 서버로 옮겨도 애플리케이션 코드는 거의 건드리지 않아도 됩니다.

### 2. `skt/A.X-4.0-Light`는 기본 모델값으로 둔다

초기 기본값은 아래처럼 둡니다.

```env
LLM_PROVIDER=openai_compatible
LLM_MODEL=skt/A.X-4.0-Light
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=dummy
```

설명:

- `skt/A.X-4.0-Light`는 기본 생성 모델
- `LLM_BASE_URL`은 로컬 또는 사내 추론 서버 주소
- `LLM_API_KEY`는 OpenAI-compatible 서버가 요구하지 않으면 더미값 허용

이 기본값은 "모델 이름은 SKT, 호출 프로토콜은 OpenAI-compatible"이라는 의미입니다. 즉 특정 런타임에 종속되지 않습니다.

### 3. 앱 내부에서는 `provider factory`만 바라보게 한다

현재 코드에는 `OpenAIProvider`를 직접 import하는 위치가 있습니다.

- [src/company_llm_rag/rag_system.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/rag_system.py)
- [src/company_llm_rag/no_answer_analyzer.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/no_answer_analyzer.py)
- [src/company_llm_rag/teams_sender.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/teams_sender.py)

이 직접 의존을 아래처럼 바꿉니다.

- `llm/factory.py`에서 기본 provider 생성
- 각 사용처는 `default_llm` 또는 `create_llm(...)`만 사용
- 요약 전용 모델도 같은 factory를 통해 생성

핵심은 "OpenAI냐 아니냐"가 아니라 "설정에 따라 어떤 provider 인스턴스를 만들 것인가"를 한 곳에 모으는 것입니다.

### 4. OpenAI 전용 설정을 일반 설정으로 승격한다

현재:

- `OPENAI_CHAT_MODEL`
- `OPENAI_SUMMARIZE_MODEL`
- `OPENAI_API_KEY`
- `OPENAI_TEMPERATURE`

변경 방향:

- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_SUMMARIZE_MODEL`
- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_TEMPERATURE`

OpenAI 유지 호환을 위해 기존 환경변수는 당분간 fallback으로 읽을 수 있습니다.

예:

- `LLM_MODEL` 없으면 `OPENAI_CHAT_MODEL` fallback
- `LLM_SUMMARIZE_MODEL` 없으면 `OPENAI_SUMMARIZE_MODEL` fallback
- `LLM_API_KEY` 없으면 `OPENAI_API_KEY` fallback

이렇게 해야 기존 운영 환경에 대한 파급을 줄이면서 점진 전환이 가능합니다.

## 아키텍처 변경안

### 신규/변경 모듈

- `src/company_llm_rag/llm/base.py`
  - `stream_chat()`를 인터페이스 수준으로 명시
- `src/company_llm_rag/llm/openai_provider.py`
  - OpenAI 전용 구현 유지
- `src/company_llm_rag/llm/openai_compatible_provider.py`
  - base_url configurable provider
- `src/company_llm_rag/llm/factory.py`
  - provider 선택 및 기본 인스턴스 생성
- `src/company_llm_rag/config.py`
  - 일반화된 LLM 설정 추가

### 권장 클래스 구조

```python
class LLMProvider(ABC):
    def chat(...)
    def stream_chat(...)

class OpenAIProvider(LLMProvider):
    ...

class OpenAICompatibleProvider(LLMProvider):
    ...
```

`OpenAICompatibleProvider`는 구현 자체는 OpenAI Python client를 재사용할 수 있습니다. 차이는 `base_url`, `api_key`, 기본 모델값이 설정에서 들어온다는 점입니다.

### Factory 규칙

```python
if settings.LLM_PROVIDER == "openai":
    return OpenAIProvider(...)
if settings.LLM_PROVIDER == "openai_compatible":
    return OpenAICompatibleProvider(...)
raise ValueError(...)
```

요약 모델도 같은 방식으로 생성합니다.

```python
default_llm = create_default_llm()
summarizer_llm = create_llm(default_model=settings.LLM_SUMMARIZE_MODEL, default_temperature=0.3)
```

## 설정 설계

### 신규 환경변수

| 변수 | 의미 | 예시 |
|---|---|---|
| `LLM_PROVIDER` | 생성 provider 종류 | `openai_compatible` |
| `LLM_MODEL` | 기본 생성 모델 | `skt/A.X-4.0-Light` |
| `LLM_SUMMARIZE_MODEL` | 요약용 모델 | `skt/A.X-4.0-Light` |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | `http://localhost:8000/v1` |
| `LLM_API_KEY` | provider 인증키 | `dummy` |
| `LLM_TEMPERATURE` | 기본 temperature | `0.7` |

### 하위 호환

아래 fallback을 유지합니다.

- `LLM_MODEL` -> 없으면 `OPENAI_CHAT_MODEL`
- `LLM_SUMMARIZE_MODEL` -> 없으면 `OPENAI_SUMMARIZE_MODEL`
- `LLM_API_KEY` -> 없으면 `OPENAI_API_KEY`
- `LLM_TEMPERATURE` -> 없으면 `OPENAI_TEMPERATURE`

### 검증 규칙

- `LLM_PROVIDER=openai`이면 `LLM_API_KEY` 또는 `OPENAI_API_KEY` 필수
- `LLM_PROVIDER=openai_compatible`이면 `LLM_BASE_URL` 필수
- `LLM_MODEL`은 항상 필수
- 임베딩이 OpenAI인 동안은 임베딩 경로에서만 `OPENAI_API_KEY`가 필요

여기서 중요한 점은 "생성 모델을 sLLM으로 바꿔도 임베딩이 OpenAI면 여전히 OpenAI 키가 필요할 수 있다"는 사실을 설정 검증에 명확히 반영하는 것입니다.

## 런타임 전략

### 1차 기본 전략: OpenAI-compatible 서버

권장 런타임은 OpenAI-compatible API를 제공하는 형태입니다. 예를 들어:

- vLLM
- LM Studio server mode
- 사내 LLM gateway

이 방식의 장점:

- 현재 코드와 인터페이스 차이가 가장 작음
- 스트리밍 처리 재사용 가능
- 모델 교체 시 `LLM_MODEL`, `LLM_BASE_URL` 변경만으로 대응 가능

### 왜 Ollama 전용 설계를 기본으로 두지 않는가

Ollama는 운영이 편하지만, `skt/A.X-4.0-Light` 적용 경로는 OpenAI-compatible 서버 쪽이 더 일반적이고 향후 교체 자유도도 높습니다. 이번 요구사항의 핵심이 "SKT 모델 사용"과 "추후 변경 용이성"이므로, 런타임 종속성을 줄이는 설계가 더 적합합니다.

## 적용 대상 코드

### 직접 변경 대상

- [src/company_llm_rag/config.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/config.py)
- [src/company_llm_rag/llm/base.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/llm/base.py)
- [src/company_llm_rag/llm/openai_provider.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/llm/openai_provider.py)
- `src/company_llm_rag/llm/openai_compatible_provider.py` 신규
- `src/company_llm_rag/llm/factory.py` 신규
- [src/company_llm_rag/rag_system.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/rag_system.py)
- [src/company_llm_rag/no_answer_analyzer.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/no_answer_analyzer.py)
- [src/company_llm_rag/teams_sender.py](/Users/we/Dev/lab/mycomai/src/company_llm_rag/teams_sender.py)
- [tests/test_config.py](/Users/we/Dev/lab/mycomai/tests/test_config.py)

### 문서 변경 대상

- [README.md](/Users/we/Dev/lab/mycomai/README.md)
- [docs/02_operations_guide.md](/Users/we/Dev/lab/mycomai/docs/02_operations_guide.md)
- [docs/03_project_structure.md](/Users/we/Dev/lab/mycomai/docs/03_project_structure.md)

## 단계별 작업 계획

### Phase 1. 설정 일반화

- 일반화된 LLM 환경변수 추가
- 기존 OpenAI 환경변수 fallback 유지
- `validate()`를 provider-aware 방식으로 변경

### Phase 2. provider factory 도입

- `default_llm` 생성 위치를 factory로 이동
- OpenAI provider 직접 import 제거
- 요약용 provider 생성 경로도 factory로 일원화

### Phase 3. `skt/A.X-4.0-Light` 연결

- OpenAI-compatible provider 구현
- 기본 모델을 `skt/A.X-4.0-Light`로 설정
- base URL, timeout, 인증 동작 점검

### Phase 4. 테스트 및 문서 정리

- 설정 테스트 갱신
- provider 선택 단위 테스트 추가
- 운영 문서에 예시 `.env` 추가

## 리스크 및 대응

### 1. 한국어 답변 품질 차이

SKT 모델은 한국어에 강점을 기대할 수 있지만, 현재 프롬프트와 완전히 같은 품질을 보장하지는 않습니다.

대응:

- 생성 모델만 먼저 교체
- 회귀 질문셋으로 품질 확인
- 필요 시 system prompt와 temperature 재조정

### 2. 응답 속도 저하

sLLM은 로컬 GPU/CPU 상태에 따라 OpenAI보다 느릴 수 있습니다.

대응:

- 스트리밍 유지
- 운영 문서에 권장 하드웨어와 timeout 설정 명시

### 3. 설정 혼선

OpenAI와 일반 LLM 설정이 동시에 존재하면 운영자가 헷갈릴 수 있습니다.

대응:

- 문서에서 "생성용"과 "임베딩용" 설정을 분리 표기
- 향후 임베딩도 일반화할 때 명확히 재정리

## 수용 기준

- `LLM_PROVIDER` 설정만으로 생성 provider를 선택할 수 있다.
- 기본 sLLM 모델명이 `skt/A.X-4.0-Light`로 설정된다.
- 모델 변경 시 코드 수정 없이 환경변수 변경만으로 대응할 수 있다.
- `rag_system`, `no_answer_analyzer`, `teams_sender`가 특정 provider 구현체를 직접 import하지 않는다.
- 기존 OpenAI 경로도 fallback으로 유지된다.

## 운영 예시

### sLLM 사용 예시

```env
LLM_PROVIDER=openai_compatible
LLM_MODEL=skt/A.X-4.0-Light
LLM_SUMMARIZE_MODEL=skt/A.X-4.0-Light
LLM_BASE_URL=http://llm-inference:8000/v1
LLM_API_KEY=dummy
LLM_TEMPERATURE=0.7
OPENAI_API_KEY=...   # 임베딩 유지 시 필요
```

### OpenAI 복귀 예시

```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
LLM_SUMMARIZE_MODEL=gpt-4o-mini
LLM_API_KEY=...
LLM_TEMPERATURE=0.7
```

## 결론

이번 작업은 "SKT 모델 1회성 연결"이 아니라 "생성 모델 공급자를 설정으로 교체 가능한 구조로 바꾸는 작업"으로 정의해야 합니다. `skt/A.X-4.0-Light`는 그 구조 위에서 동작하는 첫 기본 모델이며, 런타임은 OpenAI-compatible endpoint를 기준으로 설계하는 것이 추후 교체 비용을 가장 낮춥니다.

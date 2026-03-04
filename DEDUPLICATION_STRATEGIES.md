# 🔄 중복 데이터 수집 최소화 전략

**작성일**: 2026-02-23
**목적**: API 호출 최소화 및 수집 효율성 향상

---

## 📊 현재 상황

### 구현된 기능 ✓
- **LOOKBACK_DAYS**: 모든 extractor에 구현됨
  ```python
  # Jira: updated >= "-7d"
  # Confluence: lastModified >= "-7d"
  # SharePoint/Teams: lastModifiedDateTime >= ISO date
  ```
- **ChromaDB Upsert**: ID 기반 중복 방지

### 문제점 ❌
1. **기본값 None**: 설정하지 않으면 매번 전체 데이터 수집
2. **수동 관리**: .env에 직접 설정해야 함
3. **일회성**: 한 번 설정하면 계속 동일한 기간만 수집

---

## 💡 개선 방안

### 방법 1: 상태 파일 시스템 ⭐⭐⭐ (강력 추천)

**개념**: 마지막 수집 시간을 파일에 저장하고 자동으로 다음 수집 시 사용

**구현**:
```python
# state_manager.py
import json
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("data/.collection_state.json")

def load_state(source: str) -> dict:
    """마지막 수집 상태 로드"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
            return state.get(source, {})
    return {}

def save_state(source: str, last_updated: datetime):
    """수집 완료 후 상태 저장"""
    state = {}
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)

    state[source] = {
        "last_updated": last_updated.isoformat(),
        "collected_at": datetime.now(timezone.utc).isoformat()
    }

    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# 사용 예시
def main():
    state = load_state("jira")

    if state:
        # 증분 업데이트
        last_updated = state["last_updated"]
        jql = f"project = X AND updated >= '{last_updated}'"
    else:
        # 초기 전체 수집
        jql = f"project = X"

    # ... 데이터 수집 ...

    save_state("jira", datetime.now(timezone.utc))
```

**장점**:
- ✅ 자동 증분 업데이트
- ✅ 각 소스별 독립적 관리
- ✅ 실패 시에도 복구 가능
- ✅ 간단한 구현

**단점**:
- 파일 기반이라 분산 환경에서 동기화 필요

**예상 효과**:
- 초기: 전체 수집 (1000개)
- 2회차: 최근 변경분만 (10-50개) → **95% 감소**

---

### 방법 2: 환경변수 기반 모드 전환 ⭐⭐

**개념**: --full-sync / --incremental 플래그로 수집 모드 선택

**구현**:
```python
# .env
SYNC_MODE=incremental  # or 'full'
INCREMENTAL_DAYS=7     # 증분 수집 시 기간

# extractor
sync_mode = os.getenv('SYNC_MODE', 'full')

if sync_mode == 'incremental':
    days = int(os.getenv('INCREMENTAL_DAYS', 7))
    jql += f" AND updated >= '-{days}d'"
```

**사용법**:
```bash
# 초기 전체 수집
SYNC_MODE=full docker-compose up data-loader

# 이후 증분 수집
SYNC_MODE=incremental docker-compose up data-loader
```

**장점**:
- ✅ 명시적 제어
- ✅ 간단한 구현
- ✅ 스케줄러 통합 쉬움

**단점**:
- 수동으로 모드 변경 필요
- 마지막 수집 시간을 정확히 모름

---

### 방법 3: ChromaDB 메타데이터 활용 ⭐⭐

**개념**: ChromaDB의 최신 updated_at를 조회하여 그 이후만 수집

**구현**:
```python
def get_last_updated_from_db(source: str) -> Optional[datetime]:
    """ChromaDB에서 해당 소스의 가장 최근 updated_at 조회"""
    collection = db_manager.get_collection()

    # 해당 소스의 모든 메타데이터 조회
    results = collection.get(
        where={"source": source},
        include=["metadatas"]
    )

    if not results["metadatas"]:
        return None

    # 가장 최근 updated_at 찾기
    latest = max(
        m.get("updated_at", "") for m in results["metadatas"]
    )

    return datetime.fromisoformat(latest) if latest else None

# 사용
last_updated = get_last_updated_from_db("jira")
if last_updated:
    jql = f"project = X AND updated > '{last_updated.isoformat()}'"
```

**장점**:
- ✅ DB와 동기화
- ✅ 별도 상태 파일 불필요
- ✅ 실제 저장된 데이터 기준

**단점**:
- ChromaDB 쿼리 오버헤드
- 메타데이터 조회 시간 소요

---

### 방법 4: 해시 기반 변경 감지 ⭐

**개념**: 콘텐츠 해시를 저장하고 변경된 경우만 업데이트

**구현**:
```python
import hashlib

def content_hash(content: str) -> str:
    """콘텐츠 해시 생성"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]

# 메타데이터에 해시 저장
metadata = {
    "content_hash": content_hash(content),
    "updated_at": updated_at
}

# 업데이트 시 해시 비교
existing = collection.get(ids=[doc_id])
if existing and existing["metadatas"][0]["content_hash"] == new_hash:
    logger.debug(f"Skipping unchanged document: {doc_id}")
    continue
```

**장점**:
- ✅ 정확한 변경 감지
- ✅ 불필요한 임베딩 생성 방지

**단점**:
- 모든 문서를 먼저 가져와야 함 (API 호출 동일)
- 임베딩 생성은 줄지만 수집은 동일

**효과**:
- API 호출: 동일
- 임베딩 생성: 50-70% 감소

---

### 방법 5: 배치 크기 최적화 ⭐

**개념**: 대량 데이터를 작은 배치로 나누어 처리

**구현**:
```python
def process_in_batches(items, batch_size=100):
    """배치 단위로 처리"""
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]

        # 배치 처리
        process_batch(batch)

        # 중간 저장 (실패 시 재시작 가능)
        save_checkpoint(i + len(batch))
```

**장점**:
- ✅ 메모리 효율적
- ✅ 중간 실패 시 재시작 가능
- ✅ 진행 상황 추적 용이

**단점**:
- 전체 수집 시간은 동일
- 중복 감소 효과는 없음

---

## 🎯 추천 조합

### 시나리오 1: 최소 구현 (빠른 개선)
```python
# .env
LOOKBACK_DAYS=7  # 최근 7일만 수집
```

**효과**: 80-90% 감소 (설정만으로)

---

### 시나리오 2: 자동화 (권장 ⭐⭐⭐)
1. **상태 파일 시스템** 구현
2. **크론잡**에서 자동 증분 수집
3. **수동 전체 수집** 옵션 유지

**구현 순서**:
```bash
# 1. 상태 관리 모듈 추가
src/company_llm_rag/state_manager.py

# 2. 각 extractor에 통합
- load_state() at start
- save_state() at end

# 3. 크론잡 설정
# 매일 새벽 2시 증분 수집
0 2 * * * docker-compose up data-loader
```

**효과**:
- 초기: 1000개 수집
- 이후: 10-50개 수집 (95% 감소)

---

### 시나리오 3: 최적화 (고급)
1. 상태 파일 시스템
2. 해시 기반 변경 감지
3. 배치 처리

**효과**:
- API 호출: 95% 감소
- 임베딩 생성: 97% 감소
- 처리 시간: 90% 단축

---

## 📈 비교표

| 방법 | 구현 난이도 | API 호출 감소 | 임베딩 감소 | 자동화 | 추천도 |
|------|------------|--------------|------------|--------|--------|
| 상태 파일 | 중 | 95% | 95% | ✓ | ⭐⭐⭐ |
| 환경변수 모드 | 하 | 80% | 80% | △ | ⭐⭐ |
| ChromaDB 조회 | 중 | 90% | 90% | ✓ | ⭐⭐ |
| 해시 감지 | 하 | 0% | 70% | ✓ | ⭐ |
| 배치 처리 | 하 | 0% | 0% | ✓ | ⭐ |

---

## 🚀 즉시 적용 가능

### 옵션 A: 간단한 설정 (5분)
```bash
# .env에 추가
LOOKBACK_DAYS=7

# 재실행
docker-compose up data-loader
```

### 옵션 B: 상태 파일 구현 (30분)
```bash
# 1. state_manager.py 생성
# 2. 각 extractor 수정
# 3. 테스트
```

---

## 💰 비용 절감 효과

**현재 (전체 수집)**:
- API 호출: 1000회
- 임베딩 생성: 48,485개
- OpenAI 비용: ~$0.50/회
- 처리 시간: 10분

**개선 후 (증분 수집)**:
- API 호출: 50회 (95% 감소)
- 임베딩 생성: 500개 (99% 감소)
- OpenAI 비용: ~$0.01/회 (98% 감소)
- 처리 시간: 30초 (95% 감소)

**월간 효과** (매일 수집 기준):
- 현재: $15/월
- 개선: $0.30/월
- **절감: $14.70/월 (98%)**

---

## 📝 다음 단계

어떤 방법을 적용하시겠습니까?

1. **즉시**: `.env`에 LOOKBACK_DAYS=7 추가
2. **단기**: 상태 파일 시스템 구현
3. **장기**: 완전 자동화 시스템

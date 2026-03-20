# 🔧 Mycomai RAG 시스템 리팩토링 계획

**작성일**: 2026-02-23
**작성자**: CTO
**목적**: 프로덕션 레벨의 안정적이고 확장 가능한 RAG 시스템 구축

---

## 🚨 Critical: 즉시 해결 필요

### 1. ✅ 패키지 구조 및 모듈화 [완료]
**문제점:**
- `__init__.py` 파일이 전혀 없음 → Python 패키지로 인식되지 않음
- 상대 import 사용 (`from retrieval_module import`) → 실행 경로 의존성 문제
- 공통 코드가 각 파일에 중복됨 (ChromaDB 초기화, 환경변수 로드 등)

**영향**: 코드 재사용 불가, 테스트 작성 어려움, 배포 복잡도 증가

**해결 방안:**
- [x] 모든 디렉토리에 `__init__.py` 추가
- [x] 공통 모듈 생성: `config.py`, `database.py`
- [x] 절대 import로 변경 (`from company_llm_rag.config import settings`)
- [x] 중복 코드 제거 및 단일 책임 원칙 적용
- [x] Docker PYTHONPATH 설정 추가

**완료일**: 2026-02-23

**변경 사항:**
- 5개의 `__init__.py` 파일 추가
- `config.py` 생성 - 중앙화된 설정 관리
- `database.py` 생성 - ChromaDB 관리 클래스
- `retrieval_module.py`, `rag_system.py`, `data_loader.py` 리팩토링
- `jira_extractor.py`, `confluence_extractor.py` 리팩토링
- Dockerfile에 PYTHONPATH 추가

**테스트 결과:**
- ✅ Config 모듈 로드 성공
- ✅ Database 모듈 로드 성공 (48,485개 문서 확인)
- ✅ Retrieval 모듈 정상 작동

---

### 2. 설정 관리 (Configuration)
**문제점:**
- 환경변수가 각 파일에서 개별적으로 로드됨
- 하드코딩된 값들 (chunk_size=100, model="gpt-4o", n_results=3)
- 검증 로직이 분산되어 있음

**영향**: 설정 변경 시 여러 파일 수정 필요, 운영 환경별 설정 관리 어려움

**해결 방안:**
- [ ] Pydantic Settings 도입
- [ ] 중앙화된 설정 파일 (`config.py`)
- [ ] 환경별 설정 분리 (dev, staging, prod)
- [ ] 설정 검증 로직 통합

---

### 3. ✅ 에러 핸들링 및 로깅 [완료]
**문제점:**
- `print()`로 로깅 → 프로덕션 환경 부적합
- 일관성 없는 에러 처리 (일부는 exit(1), 일부는 continue)
- 재시도 로직 없음 (네트워크 오류에 취약)

**영향**: 장애 추적 어려움, 복구 불가능, 운영 모니터링 불가

**해결 방안:**
- [x] 구조화된 로깅 (Python logging with custom formatter)
- [x] 로그 레벨 표준화 (DEBUG, INFO, WARNING, ERROR)
- [x] 컬러 출력 지원 (터미널 환경)
- [x] 모든 모듈에 로거 적용
- [ ] 재시도 로직 추가 (tenacity 라이브러리) - Phase 2
- [ ] 커스텀 예외 클래스 정의 - Phase 2

**완료일**: 2026-02-23

**변경 사항:**
- `logger.py` 생성 - 구조화된 로깅 모듈
- ColoredFormatter - 컬러 터미널 출력
- 모든 print 문을 logger로 교체
- config.py에 LOG_LEVEL 설정 추가
- .env.sample에 LOG_LEVEL 문서화

**테스트 결과:**
- ✅ 로거 모듈 정상 작동
- ✅ 컬러 출력 정상 작동
- ✅ 로그 레벨별 필터링 정상 작동

---

## ⚠️ High Priority: 빠른 시일 내 개선

### 4. ✅ 의존성 관리 [완료] / 타입 안정성 [부분 완료]
**문제점:**
- `requirements.txt`에 버전 명시 없음 → 재현 불가능한 빌드
- 타입 힌트가 일부만 적용됨
- `google-generativeai`가 사용되지 않는데 포함됨

**영향**: 배포 안정성 저하, 팀 협업 시 버그 증가

**해결 방안:**
- [x] 의존성 버전 고정
- [x] 미사용 의존성 제거 (google-generativeai)
- [x] requirements.txt 문서화 및 주석 추가
- [x] .gitignore 확장 (Python, IDE 관련)
- [ ] 타입 힌트 완성 (모든 함수/메서드) - Phase 2
- [ ] mypy strict mode 적용 - Phase 2

**완료일**: 2026-02-23

**변경 사항:**
- requirements.txt 버전 명시 및 주석 추가:
  - requests==2.32.5
  - msal==1.34.0
  - chromadb==1.4.1
  - openai==2.17.0
  - python-dotenv==1.2.1
- google-generativeai 제거
- .gitignore 대폭 확장 (Python, IDE, .claude/)

---

### 5. 보안
**문제점:**
```python
# jira_extractor.py:24
Authorization: f"Basic {base64.b64encode(...)}"  # 로그에 노출 가능
```
- API 키가 코드 전체에 분산되어 관리됨
- 입력 검증 부족
- 민감 정보 로깅 가능성

**영향**: 보안 취약점, 컴플라이언스 리스크

**해결 방안:**
- [ ] 민감 정보 필터링 (로그에서 API 키 마스킹)
- [ ] 입력 검증 강화
- [ ] Secret 관리 도구 검토 (AWS Secrets Manager, HashiCorp Vault)
- [ ] 보안 스캔 도구 도입 (bandit, safety)

---

### 6. 성능 최적화
**문제점:**
- 동기 처리만 지원 (async/await 없음)
- API 호출 시 배치 처리 미흡
- 캐싱 전략 없음
- 페이징 로직에 비효율적인 루프

**영향**: 처리 속도 느림, 비용 증가, 확장성 제한

**해결 방안:**
- [ ] 비동기 처리 도입 (asyncio, httpx)
- [ ] API 호출 배치 처리
- [ ] Redis 캐싱 전략
- [ ] 페이징 로직 최적화
- [ ] 병렬 처리 (concurrent.futures)

---

## 📝 Medium Priority: 점진적 개선

### 7. 테스트 부재
**문제점:**
- 유닛 테스트 0개
- 통합 테스트 0개
- Mock 전략 없음

**영향**: 리팩토링 리스크 증가, 회귀 버그 발생 가능성

**해결 방안:**
- [ ] pytest 설정
- [ ] 유닛 테스트 작성 (목표: 70% 커버리지)
- [ ] Mock 전략 수립 (pytest-mock)
- [ ] 통합 테스트 작성
- [ ] CI에서 테스트 자동 실행

---

### 8. 코드 품질
**문제점:**
```python
# data_loader.py:54 - 매직 넘버
def chunk_content(content: str, chunk_size: int = 100, chunk_overlap: int = 50)

# 복잡한 조건문들 (confluence_extractor.py:93-98)
```

**영향**: 유지보수 어려움, 버그 발생 가능성

**해결 방안:**
- [ ] 매직 넘버를 상수로 정의
- [ ] 복잡한 조건문 리팩토링
- [ ] Black, isort, flake8 적용
- [ ] pre-commit hooks 설정
- [ ] 코드 리뷰 체크리스트 작성

---

### 9. 관찰성 (Observability)
**문제점:**
- 메트릭 수집 없음
- 분산 추적 없음
- 성능 프로파일링 불가

**영향**: 성능 병목 파악 불가, 비용 최적화 어려움

**해결 방안:**
- [ ] OpenTelemetry 통합
- [ ] Prometheus 메트릭 수집
- [ ] 성능 프로파일링 도구 (py-spy, cProfile)
- [ ] 대시보드 구축 (Grafana)

---

## 💡 리팩토링 로드맵

### Phase 1: 기반 다지기 (1-2주)
1. ✅ 패키지 구조 정리 및 `__init__.py` 추가 [진행중]
2. 중앙화된 설정 관리 (Pydantic Settings)
3. 구조화된 로깅 (structlog)
4. 의존성 버전 고정 (poetry/pip-tools)

**완료 조건:**
- 모든 모듈이 절대 import로 동작
- 설정이 단일 파일에서 관리됨
- 구조화된 로그 출력
- 재현 가능한 빌드

---

### Phase 2: 안정성 강화 (2-3주)
5. 에러 핸들링 표준화 및 재시도 로직
6. 타입 힌트 완성 및 mypy 적용
7. 유닛 테스트 작성 (pytest)
8. CI/CD 파이프라인 구축

**완료 조건:**
- 모든 외부 API 호출에 재시도 로직
- mypy strict 통과
- 70% 이상 테스트 커버리지
- CI에서 자동 테스트 및 린트

---

### Phase 3: 성능 및 확장성 (3-4주)
9. 비동기 처리 (asyncio)
10. 캐싱 전략 (Redis)
11. 배치 처리 최적화
12. 관찰성 도구 통합 (OpenTelemetry)

**완료 조건:**
- 데이터 수집 시간 50% 단축
- 쿼리 응답 시간 p95 < 2초
- 실시간 메트릭 대시보드
- 비용 30% 절감

---

## 📊 진행 상황 트래킹

| Phase | Task | Status | 담당자 | 완료일 |
|-------|------|--------|--------|--------|
| 1 | 패키지 구조 개선 | 🟢 완료 | CTO | 2026-02-23 |
| 1 | 설정 관리 | 🟢 완료 | CTO | 2026-02-23 |
| 1 | 로깅 개선 | 🟢 완료 | CTO | 2026-02-23 |
| 1 | 의존성 관리 | 🟢 완료 | CTO | 2026-02-23 |

**상태**: ⚪ 대기 | 🟡 진행중 | 🟢 완료 | 🔴 블로킹

---

## 📌 참고 자료
- [Python 패키징 가이드](https://packaging.python.org/)
- [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [structlog](https://www.structlog.org/)
- [pytest 베스트 프랙티스](https://docs.pytest.org/en/stable/goodpractices.html)

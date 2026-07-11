# Issue #60 ChromaDB 내부 장비 이관 (192.168.0.18, M1 36GB) — client-server 전환

GitHub Issue: https://github.com/himinseop/mycomai/issues/60

## 배경

| 항목 | 현재 | 문제 |
|---|---|---|
| ChromaDB 모드 | embedded (web 컨테이너 내 PersistentClient) | 인덱스 캐시(2GB 제한)와 Python 힙이 4g cgroup에서 경합 |
| 운영 장비 | M1 16GB, Docker VM 5GB | Ollama RAM 확보를 위해 축소 → 상시 빠듯 |
| 데이터 | 165,289 청크 / 디스크 3.5GB / 활성 HNSW ~1GB | 수집 정상화로 증가 추세 (+4.5k/회) |

이관 대상: **192.168.0.18 — MacBook M1 36GB** (LAN RTT 1.5ms 확인).

## 목표

- ChromaDB를 .18 장비의 Docker 서버 모드로 분리, 운영 장비 web은 HttpClient로 접속
- 재임베딩 없이 데이터 이관 (디렉토리 복사)
- `.env` 한 줄로 롤백 가능한 전환 (embedded ↔ http)

## 비목표

- ChromaDB 외 구성요소 이전 (FTS/sqlite/web은 운영 장비 유지)
- Qdrant 등 벡터 DB 교체 (별도 검토 — 기존 백로그)
- .18 장비의 Ollama 활용 (시너지 크지만 별도 이슈로)

## 아키텍처 변화

```
[현재]                                [전환 후]
web 컨테이너                           web 컨테이너 (운영 장비)
├─ FastAPI + RAG                      ├─ FastAPI + RAG
├─ ChromaDB (in-process, mmap 2GB)    ├─ FTS(sqlite) · 리랭커 · app_data.db
├─ FTS(sqlite) · 리랭커                └─ chromadb.HttpClient ──LAN──┐
└─ app_data.db                                                       │
                                      chroma 서버 컨테이너 (.18, 36GB)│
                                      ├─ chromadb/chroma:1.4.x  <────┘
                                      ├─ persist volume (복사된 db/chroma_db)
                                      └─ 토큰 인증, 캐시 넉넉히 (인덱스 전체 RAM)
```

- 질의 흐름: web이 쿼리 임베딩(OpenAI) 생성 → HTTP로 .18에 벡터 질의 → 결과 수신.
  LAN RTT 1.5ms + 직렬화로 질의당 수~수십 ms 추가 예상 (현재 벡터 180~500ms 대비 미미)
- 쓰기 흐름: data-loader도 HttpClient로 .18에 적재 (코드 동일, 클라이언트만 교체)
- FTS·리랭킹·RRF는 지금처럼 web 로컬에서 수행 — 하이브리드 검색 로직 무변경

## 설정·코드 변경 (Phase 1)

### config.py
```
CHROMA_MODE: embedded(기본) | http
CHROMA_SERVER_HOST / CHROMA_SERVER_PORT (기본 9000 — 8000은 web과 혼동 방지)
CHROMA_SERVER_TOKEN (서버와 동일 값)
```

### database.py (유일한 실질 변경점)
```python
if settings.CHROMA_MODE == "http":
    self._client = chromadb.HttpClient(
        host=..., port=...,
        settings=CS(chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
                    chroma_client_auth_credentials=settings.CHROMA_SERVER_TOKEN),
    )
else:
    self._client = chromadb.PersistentClient(...)  # 기존 그대로
```
- keep-alive(#54)는 collection API 경유라 무변경 동작 (원격에도 워밍 효과)
- embedded 전용 캐시 설정(LRU/limit)은 embedded 분기에만 적용
- 헬스체크: startup 시 http 모드면 `client.heartbeat()` 확인, 실패 시 명확한 에러 로그

### 부수 변경
- `scripts/rebuild_collection.py` 등 PersistentClient 직접 사용 스크립트: db_manager 경유로 통일
- 클라이언트-서버 **버전 일치 필수**: 서버 이미지 `chromadb/chroma:1.4.x` = 클라이언트 1.4.1

## .18 장비 구축 (Phase 2)

`deploy/chroma-server/docker-compose.yml` (레포에 포함, .18에서 사용):
```yaml
services:
  chroma:
    image: chromadb/chroma:1.4.1
    ports: ["9000:8000"]
    volumes: ["./data:/data"]
    environment:
      - CHROMA_SERVER_AUTHN_PROVIDER=chromadb.auth.token_authn.TokenAuthenticationServerProvider
      - CHROMA_SERVER_AUTHN_CREDENTIALS=${CHROMA_SERVER_TOKEN}
    restart: unless-stopped
    mem_limit: 8g
```

체크리스트:
1. Docker Desktop 설치 + 리소스 (메모리 10GB+, 여유 충분)
2. **절전 금지 설정** — 맥북 특성상 필수: 전원 연결 + `caffeinate`/에너지 설정(디스플레이 꺼져도 잠자기 안 함). 잠들면 검색 전체 중단
3. 방화벽: Docker 수신 허용 (지난 사내망 이슈에서 확인한 절차)
4. 데이터 복사: 운영 장비에서 loader/web 중지 → `rsync -a db/chroma_db/ .18:/path/data/` (3.5GB, LAN 수 분)
5. 서버 기동 → 원격 스모크: heartbeat + count=165,289 + 코사인 질의 1건

## 전환 (Phase 3)

1. 쓰기 동결 (data-loader 실행 금지, cron 일시 중지)
2. 최종 rsync (차분) → .18 서버 재기동
3. 운영 `.env`: `CHROMA_MODE=http` + 호스트/토큰 → web 재생성
4. 검증: 검색 지연(벡터/총), 검색 결과 동일성(대표 질의 5건 결과 비교), keep-alive 로그, 관리자 db-stats
5. 24시간 모니터링 후 안정 판정

**롤백**: `.env`를 embedded로 되돌리고 재기동 (로컬 db 디렉토리 보존 — 전환 기간 쓰기 동결이라 데이터 정합 유지)

## 안정화 후 (Phase 4)

- web `mem_limit` 4g → 2.5~3g 하향 (인덱스 캐시·mmap 소멸), 확보분은 호스트 Ollama 여유로
- 로컬 `db/chroma_db`는 스냅샷 백업으로 강등 (주기 백업은 .18에서 수행 — 절차 문서화)
- (선택) .18에 Ollama도 배치해 36GB에서 7B+ 재검증 — 별도 이슈

## 검증 시나리오 (TC)

| # | 시나리오 | 기대 |
|---|---|---|
| 1 | embedded 기본값 배포 | 기존과 완전 동일 동작 (스위치 전 무변화) |
| 2 | http 모드 + 서버 정상 | count 일치, 대표 질의 결과 embedded와 동일 |
| 3 | 벡터 검색 지연 | 원격 오버헤드 ≤ +50ms (RTT 1.5ms 기준) |
| 4 | 잘못된 토큰 | 인증 실패 명확 로그, 500 아닌 제어된 에러 |
| 5 | 서버 다운 시 질의 | 명확한 에러 + FTS-only 폴백 여부 결정(설계 검토) |
| 6 | data-loader 원격 적재 | upsert 정상, count 증가 반영 |
| 7 | .18 재부팅 | restart 정책으로 자동 복구, web 재접속 정상 |
| 8 | 롤백 | .env 원복만으로 embedded 복귀 |

## 리스크

| 리스크 | 대응 |
|---|---|
| .18 절전/종료 → 검색 전면 중단 | 절전 금지 설정 + restart 정책 + (TC5) FTS-only 폴백 검토 + 관리자 헬스 표시 |
| 버전 불일치 API 오류 | 서버 이미지 태그 = 클라이언트 버전 고정, 업그레이드는 동시 |
| 노트북 장비의 가용성 한계 | 중기적으로 상시 장비/서버로 재이관 전제 (이번 구성은 그대로 재사용 가능) |
| 전환 중 쓰기 유실 | 쓰기 동결 절차 + 최종 차분 rsync |
| LAN 대역/단절 | 동일 스위치 구간, 실패 시 롤백 1줄 |

## 일정 추정

Phase 1 코드 0.5일 → Phase 2 구축 0.5일(.18 접근 필요) → Phase 3 전환+검증 0.5일 → Phase 4 상시 모니터링.

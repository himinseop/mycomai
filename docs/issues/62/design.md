# #62 플랫폼 문서 S3 수집 — 설계

> Docs CI가 비공개 S3에 게시한 플랫폼 문서 아티팩트를 지식허브가 독립적으로 읽어 색인한다.
> 서비스 간 API·인증·직접 네트워크 연결을 추가하지 않는다. staging/production 경로 구분 없음.

## 1. 개발환경 (조사 결과)

- Python 3.11(Docker) / 3.9(로컬), 의존성 `src/requirements.txt`(pip, 버전 고정), 테스트 pytest(`src/requirements-dev.txt`)
- 수집 파이프라인: 추출기 스크립트 → JSONL(stdout) → `data_loader.py`(stdin) → ChromaDB + FTS
- 상태 저장: `app_data.db`(SQLite) — `digest_store.py`/`sqlite_utils.create_connection` 패턴
- 설정: `config.py` 싱글톤 + `.env` (`.env.example` 없음 — CLAUDE.md에 환경변수 문서화)
- boto3·LocalStack 미사용 → **boto3 신규 추가**, LocalStack 연동은 하지 않음 (기존 미사용)
- FTS(`fts_store.py`)에 delete 없음 → 삭제 함수 추가 필요
- 청크 메타데이터 `original_doc_id`로 문서 단위 삭제 가능 (`collection.delete(where=...)`)

## 2. S3 데이터 계약

- Bucket: `o2olab-devops`, Region: `ap-northeast-2`
- 현재 버전 포인터: `docs/current.json`
- Release artifact: `docs/releases/{sourceCommit}/platform.tar.gz`
- 필요 IAM 권한: 아래 두 리소스에 대한 `s3:GetObject`만. 정확한 Object Key를 알고 있으므로 `s3:ListBucket` 불필요.
  - `arn:aws:s3:::o2olab-devops/docs/current.json`
  - `arn:aws:s3:::o2olab-devops/docs/releases/*`

### current.json (schemaVersion 1)

```json
{
  "schemaVersion": 1,
  "sourceCommit": "40자리 소문자 SHA",
  "buildNumber": 1,
  "documentCount": 671,
  "artifact": {
    "key": "docs/releases/{sourceCommit}/platform.tar.gz",
    "sha256": "64자리 SHA-256",
    "size": 123456
  }
}
```

검증 규칙 (하나라도 실패하면 수집 중단, 기존 색인 유지):
- `schemaVersion == 1`
- `sourceCommit`은 40자리 소문자 hex SHA
- `artifact.key`는 정확히 `docs/releases/{sourceCommit}/platform.tar.gz`
- `artifact.sha256`은 64자리 hex SHA-256
- `artifact.size`는 0 이상의 정수
- 알 수 없는 추가 필드는 허용(무시). `documentCount`는 검증·로그 비교용 — **고정 문서 개수를 코드에 넣지 않는다**

### Artifact 구조 (tar.gz 루트)

- `catalog.json` — 문서 목록. 항목: `id`(안정적 외부 식별자), `path`(Docs 저장소 원본 경로), `markdownPath`, `htmlPath`, `title` (+추가 필드 허용)
- `manifest.json` — 파일별 size·sha256, `version`(=sourceCommit)
- `markdown/**`, `html/**`, `assets/**`
- 수집 원문은 catalog의 `markdownPath`가 가리키는 Markdown
- `catalog.json`·`manifest.json`의 `version`이 `sourceCommit`과 같아야 함
- **contentHash는 지식허브가 markdown 파일 바이트의 SHA-256으로 직접 계산** (카탈로그 제공 여부에 의존하지 않음 — 계약 최소화)

## 3. 아키텍처

기존 파이프라인(추출→JSONL→loader)에 맞추되, 삭제·상태 갱신이 필요하므로 in-process 오케스트레이터로 구현.

```
data_extraction/docs_repo/
├── s3_client.py       # S3Client 인터페이스(get_object(bucket, key) → bytes/stream)
│                      #   + Boto3S3Client + FakeS3Client(테스트용, dict 기반)
├── s3_release.py      # current.json 파싱·검증, artifact 다운로드·sha256/size 검증,
│                      #   안전 tar 해제, catalog/manifest 검증
└── s3_docs_ingest.py  # CLI 오케스트레이터 (아래 절차)
company_llm_rag/
└── platform_docs_store.py  # app_data.db 상태 테이블 (digest_store 패턴)
```

### 수집 절차 (s3_docs_ingest.py)

1. `current.json` 조회 → 스키마 검증
2. 마지막 성공 `sourceCommit`(platform_docs_state)과 비교
3. 같은 버전이면 다운로드·재색인 생략 (로그 남기고 정상 종료). `--force`로 무시 가능(전체 재수집)
4. 새 버전이면 artifact를 **임시 작업 디렉터리**(tempfile)에 다운로드
5. 파일 크기·SHA-256을 current.json과 대조 — 불일치 시 중단
6. 검증된 파일만 안전 해제 (아래 §4)
7. `catalog.json`·`manifest.json`의 `version == sourceCommit` 확인
8. manifest에 기록된 파일 크기·SHA-256 검증 (catalog가 참조하는 markdown 파일은 필수 검증)
9. catalog `markdownPath`로 문서 읽기 → **기존 docs_extractor 파싱 재사용** (digests 카테고리는
   digest_parser, 부스트·라벨 등 #61 동작 그대로). 브랜치 가드는 미적용 — CI 검증본이므로
10. `id + contentHash`로 신규·변경·삭제 판별 (platform_docs_docs 테이블 대조)
11. 신규·변경 문서만 JSONL 라인으로 만들어 `load_data_to_chromadb(iter)` in-process 호출
    (청킹·임베딩·색인 — 기존 로더의 해시 스킵·그래프 재구축·digest_guids 갱신 그대로 동작)
12. 새 catalog에서 사라진 `id`의 문서는 Chroma(`where={"original_doc_id": ...}`)와 FTS에서 삭제
13. **모든 처리가 성공한 뒤에만** 마지막 성공 sourceCommit + 문서 해시 스냅샷 갱신
14. 임시 파일은 성공·실패와 무관하게 정리 (try/finally)

### 문서 ID 매핑

- Chroma `doc_id`는 기존 관례 유지: `docs-{catalog.path}` (relpath 기반) — 기존 로컬 체크아웃
  수집분과 동일 키가 되어 S3 전환 시 자연스럽게 upsert됨
- catalog `id`는 판별 키(안정적 식별자): 상태 테이블에 `catalog_id → doc_id, content_hash` 보관
- path가 바뀌고 id가 유지되는 경우: doc_id가 달라지므로 삭제+신규로 처리 (재임베딩 1회, 허용)

### 기존 로컬 체크아웃 수집과의 관계

- `PLATFORM_DOCS_S3_BUCKET`이 설정되면 S3 수집이 문서 소스의 단일 소스.
  `docs_extractor.py`(로컬 체크아웃)는 이 설정이 있으면 수집을 건너뛴다(로그 후 빈 출력) —
  이중 적재·삭제 로직 충돌 방지
- 미설정 시 기존 로컬 체크아웃 방식 유지 (개발·폴백)
- 야간 수집: compose data-loader 커맨드에 s3_docs_ingest 단계 추가 (S3 미설정 시 자동 스킵)

## 4. Tar 보안 (해제 전 entry 검사)

다음 항목은 거부하고 수집을 중단한다:
- 절대 경로, `..` 경로 이동(정규화 후 루트 이탈 포함)
- Symbolic link, Hard link
- Device·FIFO 등 특수 파일 (regular file/directory만 허용)
- 허용된 루트 밖으로 나가는 파일

압축 폭탄 방지 상한 (설정 가능, 기본값):
- 파일 개수 ≤ 20,000
- 단일 파일 ≤ 20MB
- 전체 해제 크기 ≤ 1GB

## 5. 설정 (config.py + .env)

```bash
PLATFORM_DOCS_S3_BUCKET=o2olab-devops     # 비우면 S3 수집 비활성
PLATFORM_DOCS_S3_CURRENT_KEY=docs/current.json
AWS_REGION=ap-northeast-2
```

- AWS Access Key/Secret은 설정 예시에도 넣지 않는다 — 표준 AWS 자격증명 체인(환경변수·IAM 역할) 사용
- boto3는 `src/requirements.txt`에 버전 고정으로 추가

## 6. 로그·운영 정보

수집 결과 로그에 기록:
- 조회한 sourceCommit / 이전 성공 sourceCommit
- 신규·변경·삭제 문서 수, documentCount와 실제 catalog 문서 수 비교(불일치 시 경고)
- 스킵 사유(동일 버전), 실패 사유(검증 단계 명시)

## 7. 테스트 (FakeS3Client, AWS 불필요)

| TC | 검증 |
|---|---|
| S1 | current.json 스키마 위반 각 케이스(schemaVersion·SHA 형식·key 불일치·sha256 형식·size 음수) → 중단 |
| S2 | artifact sha256/size 불일치 → 중단, 상태 미변경 |
| S3 | manifest 파일 누락·해시 불일치 → 중단 |
| S4 | 위험 tar entry(절대경로·`..`·symlink·hardlink·device·루트 이탈) 각각 차단 |
| S5 | 압축 폭탄 상한(개수·단일·전체 크기) 초과 → 중단 |
| S6 | 신규·변경·삭제 판별 정확성 (id+contentHash) |
| S7 | 동일 sourceCommit → 다운로드·재색인 생략 |
| S8 | 수집 중 실패(색인 도중 예외) → 마지막 성공 버전·기존 상태 유지 |
| S9 | S3 Object 없음·접근 권한 없음 → 명확한 오류 처리, 상태 유지 |
| S10 | 임시 파일 정리 (성공·실패 모두) |
| S11 | catalog/manifest version ≠ sourceCommit → 중단 |

- AWS 연결이 필요한 통합 테스트는 pytest 마커(`integration`)로 분리, 기본 실행에서 제외
- 색인·삭제 경로는 ChromaDB 컬렉션을 mock/fake로 대체해 단위 테스트

## 7.5. 검증에서 발견·수정 (교차 검증)

- **증분 적재 ↔ 전체-교체형 마무리 충돌**: `load_data_to_chromadb`는 적재 끝에
  그래프 재구축·digest_guids 스냅샷을 "스트림 = 소스 전체" 가정으로 **교체**한다.
  변경분만 흘리는 S3 증분 적재에서 그대로 두면 534건 GUID 스냅샷이 변경분 몇 건으로
  truncate되고 다이제스트 그래프 노드 대부분이 소실된다. → loader에 `finalize_graph=False`
  파라미터 추가, 오케스트레이터가 전체 카탈로그 기준으로 rebuild_entities·replace_guids를
  직접 수행 (TC S12로 고정)
- **축소 문서 잔존**: 내용 부족(<50자)으로 파싱 스킵된 문서가 스냅샷에는 등록되어,
  이전에 색인된 문서가 임계치 미만으로 줄면 색인에 낡은 내용이 남는 문제 → 스킵 문서는
  스냅샷에서 제외해 '삭제'로 판별되도록 수정
- **네트워크 오류 정규화**: Boto3S3Client가 botocore `BotoCoreError`(엔드포인트·연결 오류)를
  S3ClientError로 매핑하도록 보강
- **회귀 검증**: 리팩터링된 docs_extractor를 구버전(HEAD)과 실제 master 체크아웃 637건으로
  비교 — 출력 바이트 동일. 실데이터 스모크(FakeS3 + 실제 master 아티팩트): 637건 파싱,
  digest 606, GUID 534(운영 스냅샷과 일치), 동일 버전 재실행 스킵 확인

## 8. 완료 조건

- [ ] 지식허브 실제 개발환경·기존 수집 구조에 맞는 구현
- [ ] S3에서 최신 플랫폼 문서를 가져와 색인
- [ ] 같은 sourceCommit 중복 수집 없음
- [ ] 신규·변경·삭제 정확 반영
- [ ] 다운로드·해제 무결성 및 보안 검증
- [ ] 실패 시 기존 정상 색인·마지막 성공 버전 유지
- [ ] 설정값·실행 방법·테스트 방법·필요 AWS 권한 문서화 (CLAUDE.md + 본 문서)
- [ ] 기존 문서 수집 기능(#61 다이제스트 포함)과 충돌 없음

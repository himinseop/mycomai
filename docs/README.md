# 문서 목차

이 프로젝트의 운영/개발 문서는 모두 이 디렉토리에 모아두었습니다.

## 문서 목록

| 문서 | 용도 |
|------|------|
| `01_system_overview.md` | 시스템 목적, 아키텍처, 데이터 흐름, 핵심 기능 |
| `02_operations_guide.md` | 환경 변수, 실행 명령, 점검/백업/장애 대응 |
| `03_project_structure.md` | 디렉토리 구조와 주요 모듈 분석 |
| `04_docker_deployment_guide.md` | Dockerfile, Compose 서비스, 스케줄러 운영 방식 |

## 설계 문서 (Issues)

`issues/<번호>/` 디렉토리는 GitHub 이슈 번호와 1:1 대응합니다 (예: `issues/41/` → Issue #41). 각 이슈의 설계/체크리스트/TC를 보관합니다.

| 문서 | 용도 |
|------|------|
| `issues/41/design.md` | Knowledge Hub 직접 응답 시스템 설계 |

## 작업 이력 (History)

| 문서 | 용도 |
|------|------|
| `history/` | 일자별(`YYYY-MM-DD.md`) 주요 작업·의사결정·조사 결과 기록 |

## 읽는 순서

1. 시스템을 이해하려면 `01_system_overview.md`
2. 실행하거나 운영하려면 `02_operations_guide.md`
3. 코드를 수정하려면 `03_project_structure.md`
4. 컨테이너 배포가 필요하면 `04_docker_deployment_guide.md`

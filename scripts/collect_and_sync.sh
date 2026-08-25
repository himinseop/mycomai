#!/usr/bin/env bash
# 데이터 수집 표준 진입점 (#60 후속)
# 수집+적재(.18 원격 chroma) → 성공 시 로컬 동기화 백업까지 한 번에.
#
# 사용: bash scripts/collect_and_sync.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "========================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 수집 시작"

echo "=== 0) 플랫폼매뉴얼 저장소 pull ==="
# .env의 DOCS_REPO_HOST_PATH 우선, 없으면 기본 체크아웃 경로 (docker-compose 마운트와 동일 규칙)
DOCS_DIR="$(grep -E '^DOCS_REPO_HOST_PATH=' .env 2>/dev/null | cut -d= -f2- || true)"
DOCS_DIR="${DOCS_DIR:-../../o2olab/docs}"
if [ -d "$DOCS_DIR/.git" ]; then
  # 실패해도 수집은 계속 (기존 체크아웃 사용) — 브랜치 가드는 extractor가 수행
  git -C "$DOCS_DIR" pull --ff-only \
    && echo "docs pull 완료: $(git -C "$DOCS_DIR" log --oneline -1)" \
    || echo "docs pull 실패 — 기존 체크아웃으로 수집 진행"
else
  echo "docs 체크아웃 없음($DOCS_DIR) — 매뉴얼 수집은 extractor가 건너뜀"
fi

echo "=== 1) 수집 + 적재 (data-loader → .18 chroma) ==="
docker-compose -f docker/docker-compose.yml up --exit-code-from data-loader data-loader

echo "=== 2) 로컬 동기화 백업 ==="
bash scripts/sync_chroma_backup.sh
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 전체 완료"

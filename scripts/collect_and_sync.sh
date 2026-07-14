#!/usr/bin/env bash
# 데이터 수집 표준 진입점 (#60 후속)
# 수집+적재(.18 원격 chroma) → 성공 시 로컬 동기화 백업까지 한 번에.
#
# 사용: bash scripts/collect_and_sync.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "========================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 수집 시작"
echo "=== 1) 수집 + 적재 (data-loader → .18 chroma) ==="
docker-compose -f docker/docker-compose.yml up --exit-code-from data-loader data-loader

echo "=== 2) 로컬 동기화 백업 ==="
bash scripts/sync_chroma_backup.sh
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 전체 완료"

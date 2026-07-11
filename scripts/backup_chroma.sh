#!/usr/bin/env bash
# ChromaDB 데이터 백업 (#60 Stage 2 이관·복원용)
#
# 안전 절차: 쓰기 프로세스(web embedded 모드/data-loader/chroma 서버)를 멈춘 뒤 실행해야
# 일관된 스냅샷이 됩니다. tar.gz + sha256 체크섬 생성.
#
# 사용:
#   bash scripts/backup_chroma.sh                # db/backups/에 생성
#   bash scripts/backup_chroma.sh /Volumes/ext   # 지정 위치에 생성
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="db/chroma_db"
OUT_DIR="${1:-db/backups}"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$OUT_DIR/chroma_db-$STAMP.tar.gz"

[ -d "$SRC" ] || { echo "오류: $SRC 없음"; exit 1; }

# 쓰기 프로세스 경고
RUNNING=$(docker ps --format '{{.Names}}' | grep -E 'web-1|data-loader|chroma' || true)
if [ -n "$RUNNING" ]; then
  echo "⚠ 실행 중인 컨테이너가 있습니다 (쓰기 중이면 스냅샷 불일치 가능):"
  echo "$RUNNING"
  read -r -p "계속할까요? [y/N] " ans
  [ "$ans" = "y" ] || exit 1
fi

mkdir -p "$OUT_DIR"
echo "백업 중: $SRC ($(du -sh "$SRC" | cut -f1)) → $OUT"
tar -czf "$OUT" -C db chroma_db
shasum -a 256 "$OUT" > "$OUT.sha256"
echo "완료:"
ls -lh "$OUT" "$OUT.sha256"
echo
echo "복원(대상 장비): deploy/chroma-server/README.md 참고"

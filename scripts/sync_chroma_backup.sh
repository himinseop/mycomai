#!/usr/bin/env bash
# .18(원격 ChromaDB) 데이터를 로컬로 동기화 백업 (#60 후속)
#
# 수집(data-loader)이 .18에 적재한 뒤 실행하면:
#   1) 원격 chroma 잠시 정지 (일관 스냅샷 — proxy는 유지, 중단 1~3분)
#   2) rsync로 로컬 db/chroma_db 에 미러링  ← embedded 롤백 데이터가 항상 최신 유지
#   3) 원격 chroma 재기동 (실패해도 trap으로 보장)
#   4) 로컬 tar.gz 백업 생성 + 최근 3개만 유지 (db/backups/)
#
# 사용: bash scripts/sync_chroma_backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="we@192.168.0.18"
RDIR="~/Dev/labs/chroma-server"
RDOCKER="/usr/local/bin/docker"
KEEP_BACKUPS=3

# 로더 실행 중엔 스냅샷 불일치 — 중단
if docker ps --format '{{.Names}}' | grep -q "data-loader"; then
  echo "오류: data-loader 실행 중입니다. 적재 완료 후 다시 실행하세요."
  exit 1
fi

echo "[1/4] 원격 chroma 정지 (일관 스냅샷)..."
ssh "$REMOTE" "cd $RDIR && $RDOCKER compose stop chroma" >/dev/null
trap 'ssh "$REMOTE" "cd $RDIR && $RDOCKER compose start chroma" >/dev/null && echo "[trap] 원격 chroma 재기동 완료"' EXIT

echo "[2/4] rsync → 로컬 db/chroma_db ..."
rsync -a --delete "$REMOTE:$RDIR/data/" db/chroma_db/
du -sh db/chroma_db | awk '{print "  동기화 완료: "$1}'

echo "[3/4] 원격 chroma 재기동..."
ssh "$REMOTE" "cd $RDIR && $RDOCKER compose start chroma" >/dev/null
trap - EXIT
# 재기동 검증 (컨테이너 내부 무인증 포트가 아닌 프록시 상태만 확인 — 토큰은 web이 검증)
sleep 3
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "http://192.168.0.18:9000/api/v2/heartbeat" || true)
[ "$CODE" = "401" ] || [ "$CODE" = "200" ] || { echo "경고: 원격 heartbeat 응답 이상 (HTTP $CODE)"; exit 1; }
echo "  원격 정상 (HTTP $CODE)"

echo "[4/4] 로컬 tar 백업 + 회전 (최근 ${KEEP_BACKUPS}개 유지)..."
mkdir -p db/backups
STAMP=$(date +%Y%m%d-%H%M%S)
tar -czf "db/backups/chroma_db-$STAMP.tar.gz" -C db chroma_db
shasum -a 256 "db/backups/chroma_db-$STAMP.tar.gz" > "db/backups/chroma_db-$STAMP.tar.gz.sha256"
ls -t db/backups/chroma_db-*.tar.gz | tail -n +$((KEEP_BACKUPS + 1)) | while read -r f; do
  rm -f "$f" "$f.sha256"; echo "  회전 삭제: $f"
done
ls -lh db/backups/chroma_db-*.tar.gz | awk '{print "  "$9" "$5}'
echo "완료."

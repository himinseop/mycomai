#!/usr/bin/env bash
# web 이미지를 빌드 버전(v빌드날짜.해시7)과 함께 빌드하고 재기동합니다.
# 버전 규칙: v(빌드한 날짜 YYYYMMDD).(마지막 커밋 해시 7자리)
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD_VERSION="v$(date +%Y%m%d).$(git rev-parse --short=7 HEAD)"
export BUILD_VERSION
echo "BUILD_VERSION=${BUILD_VERSION}"

COMPOSE="docker-compose -f docker/docker-compose.yml"
$COMPOSE build web
$COMPOSE up -d web
echo "완료: web 재기동 (${BUILD_VERSION})"

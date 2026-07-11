# ChromaDB 서버 배포 가이드 (#60 Stage 2)

이관 장비(예: 192.168.0.18, M1 36GB)에서 ChromaDB 서버를 Docker로 운영하기 위한 패키지입니다.

## 사전 준비 (대상 장비)

1. Docker Desktop 설치, 리소스 메모리 10GB 이상
2. **절전 금지 설정 필수** — 잠들면 전사 검색이 중단됩니다
   - 시스템 설정 → 배터리/에너지: 전원 연결 시 "디스플레이 꺼져도 잠자지 않음"
   - 또는 터미널에서 `sudo pmset -c sleep 0 disablesleep 1`
3. 방화벽에서 Docker 수신 연결 허용

## 설치·복원 절차

```bash
# 1) 이 디렉토리(deploy/chroma-server)를 대상 장비로 복사
scp -r deploy/chroma-server user@192.168.0.18:~/chroma-server
cd ~/chroma-server

# 2) 토큰 설정 (운영 .env의 CHROMA_SERVER_TOKEN과 동일해야 함)
echo 'CHROMA_SERVER_TOKEN=<토큰>' > .env

# 3) 백업 파일 복원 (운영 장비에서 scripts/backup_chroma.sh 로 생성한 파일)
scp user@운영장비:mycomai/db/backups/chroma_db-YYYYMMDD-HHMMSS.tar.gz* .
shasum -a 256 -c chroma_db-*.tar.gz.sha256     # 체크섬 검증 — OK 확인
tar -xzf chroma_db-*.tar.gz                     # → chroma_db/ 풀림
mkdir -p data && mv chroma_db/* data/           # 서버 데이터 디렉토리로

# 4) 기동
docker compose up -d
docker compose logs -f   # "Application startup" 확인
```

## 인증 구조 (중요)

chroma 1.x Rust 서버는 서버측 토큰 인증을 지원하지 않습니다 (2026-07 확인).
이 패키지는 **nginx 프록시(9000)가 Bearer 토큰을 검증**하고 chroma(내부 전용)로 전달합니다.
클라이언트 쪽 `TokenAuthClientProvider`가 보내는 `Authorization: Bearer <토큰>` 헤더와 그대로 호환됩니다.

## 검증

```bash
# 하트비트 (대상 장비에서 — 토큰 필요)
curl -s -H "Authorization: Bearer <토큰>" http://localhost:9000/api/v2/heartbeat
# 무토큰은 401이어야 정상
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9000/api/v2/heartbeat

# 운영 장비에서 원격 확인 (컨테이너 안에서)
docker exec knowledge-hub-web-1 python3 -c "
import chromadb
from chromadb.config import Settings
c = chromadb.HttpClient(host='192.168.0.18', port=9000, settings=Settings(
    chroma_client_auth_provider='chromadb.auth.token_authn.TokenAuthClientProvider',
    chroma_client_auth_credentials='<토큰>'))
print('heartbeat:', c.heartbeat())
col = c.get_collection('company_llm_rag_collection')
print('count:', col.count())   # 운영 청크 수와 일치해야 함
"
```

## 운영 전환 (운영 장비 쪽)

1. 쓰기 동결: data-loader/cron 실행 금지
2. 최종 백업 → 위 3) 재복원 (차분 반영)
3. 운영 `.env`:
   ```
   CHROMA_MODE=http
   CHROMA_SERVER_HOST=192.168.0.18
   CHROMA_SERVER_PORT=9000
   CHROMA_SERVER_TOKEN=<토큰>
   ```
4. `docker-compose up -d web` → 관리자 대시보드 "🧠 ChromaDB 상태" 카드 🟢 확인
5. 로컬 chroma 컨테이너는 중지 (`--profile chroma-server` 미기동)

**롤백**: 운영 `.env`에서 `CHROMA_MODE=embedded`로 되돌리고 web 재생성.
로컬 `db/chroma_db`는 이관 후에도 삭제하지 말 것 (전환 검증 완료 시까지 롤백 대상).

## 주기 백업 (이관 후)

대상 장비에서 주기적으로:
```bash
docker compose stop && tar -czf backup-$(date +%Y%m%d).tar.gz data && docker compose start
```

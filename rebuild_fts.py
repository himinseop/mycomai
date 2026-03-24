"""FTS 인덱스 재구축 스크립트 — Docker 내부에서 실행"""
import sqlite3, json, time, sys, os

DB = os.environ.get('DB_PATH', '/workspace/query_history.db')
BACKUP = os.environ.get('BACKUP_PATH', '/workspace/qh_backup.json')

print("SQLite version:", sqlite3.sqlite_version)
print("DB path:", DB)

conn = sqlite3.connect(DB)
# WAL 대신 DELETE journal 사용 → 완료 시 단일 파일 보장
conn.execute("PRAGMA journal_mode=DELETE")
conn.execute("PRAGMA synchronous=NORMAL")

conn.executescript("""
CREATE TABLE IF NOT EXISTS query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    created_at TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
    references_json TEXT DEFAULT '[]', teams_sent INTEGER DEFAULT 0,
    response_time_ms INTEGER DEFAULT NULL, is_no_answer INTEGER DEFAULT 0,
    ref_count INTEGER DEFAULT 0, ref_sources_json TEXT DEFAULT '[]',
    feedback INTEGER DEFAULT 0, no_answer_analysis TEXT DEFAULT NULL,
    analysis_status TEXT DEFAULT NULL, perf_json TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_session ON query_history(session_id);
CREATE INDEX IF NOT EXISTS idx_created ON query_history(created_at);
CREATE INDEX IF NOT EXISTS idx_no_answer ON query_history(is_no_answer);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    tokenize='unicode61 remove_diacritics 1'
);
""")
conn.commit()
print("Schema created")

# query_history 복원
with open(BACKUP) as f:
    backup = json.load(f)
rows = backup['rows']
existing = conn.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
if existing == 0 and rows:
    cols = list(rows[0].keys())
    conn.executemany(
        f"INSERT INTO query_history ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        [tuple(r[c] for c in cols) for r in rows]
    )
    for s in backup.get('settings', []):
        conn.execute("INSERT OR REPLACE INTO app_settings VALUES (?,?)", s)
    conn.commit()
    print(f"Restored {len(rows)} history rows")

# FTS 구축
sys.path.insert(0, '/app')
from company_llm_rag.database import db_manager
col = db_manager.get_collection()
total = col.count()
print(f"ChromaDB: {total:,} docs")

BATCH, offset, inserted = 5000, 0, 0
start = time.time()
while offset < total:
    res = col.get(limit=BATCH, offset=offset, include=['documents'])
    if not res['ids']:
        break
    conn.executemany(
        "INSERT INTO doc_fts (chunk_id, content) VALUES (?,?)",
        [(cid, doc or '') for cid, doc in zip(res['ids'], res['documents'])]
    )
    conn.commit()
    inserted += len(res['ids'])
    print(f"  {inserted:,}/{total:,} ({inserted/total*100:.0f}%) | {time.time()-start:.0f}s")
    offset += BATCH

# 체크포인트 강제 실행 (WAL → 메인 파일)
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
fts_count = conn.execute("SELECT COUNT(*) FROM doc_fts").fetchone()[0]
print(f"\nFTS 완료: {fts_count:,}행 | 소요: {time.time()-start:.0f}s")
conn.close()
print("Done.")

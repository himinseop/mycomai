"""ChromaDB 컬렉션 재구축 — live 벡터만 새 컬렉션에 복사(재임베딩 X) 후 스왑.
삭제분(soft-delete)을 실제 회수해 HNSW/디스크 축소. HNSW cosine 설정 복제."""
import time
import chromadb
from chromadb.config import Settings as CS
from chromadb.utils import embedding_functions
from company_llm_rag.config import settings

client = chromadb.PersistentClient(
    path=settings.CHROMA_DB_PATH,
    settings=CS(chroma_segment_cache_policy="LRU", chroma_memory_limit_bytes=2 * 1024**3),
)
ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=settings.OPENAI_API_KEY, model_name=settings.OPENAI_EMBEDDING_MODEL,
)
NAME = settings.COLLECTION_NAME
TMP = NAME + "_rebuild"

old = client.get_collection(NAME, embedding_function=ef)
old_cfg = old.configuration_json.get("hnsw", {})
print("old hnsw:", old_cfg.get("space"), old_cfg.get("ef_construction"), old_cfg.get("max_neighbors"), flush=True)

try:
    client.delete_collection(TMP)
except Exception:
    pass

new = client.create_collection(
    TMP, embedding_function=ef,
    configuration={"hnsw": {
        "space": old_cfg.get("space", "cosine"),
        "ef_construction": old_cfg.get("ef_construction", 100),
        "ef_search": old_cfg.get("ef_search", 100),
        "max_neighbors": old_cfg.get("max_neighbors", 16),
    }},
)
print("new hnsw:", new.configuration_json.get("hnsw", {}).get("space"), flush=True)

all_ids = old.get(include=[])["ids"]
n = len(all_ids)
print(f"복사 대상: {n}", flush=True)
B = 2000
t0 = time.monotonic()
for i in range(0, n, B):
    bid = all_ids[i:i + B]
    d = old.get(ids=bid, include=["embeddings", "documents", "metadatas"])
    new.add(
        ids=d["ids"],
        embeddings=[list(e) for e in d["embeddings"]],
        documents=d["documents"],
        metadatas=d["metadatas"],
    )
    if (i // B) % 10 == 0:
        el = int(time.monotonic() - t0)
        print(f"  {i + len(bid)}/{n}  ({el}s)", flush=True)

nc = new.count()
print(f"new count: {nc}", flush=True)
assert nc == n, f"count mismatch {nc} != {n}"

# 스왑: 기존 삭제 → 새 것을 원래 이름으로 rename
client.delete_collection(NAME)
new.modify(name=NAME)
final = client.get_collection(NAME, embedding_function=ef).count()
print(f"REBUILD_DONE count={final}", flush=True)

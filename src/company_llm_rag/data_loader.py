import sys
import json
import re
import hashlib
import time
import threading
from datetime import timedelta
from typing import List

try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# SQL 제거
# ---------------------------------------------------------------------------
# SQL 코드 블록: ```sql ... ``` 또는 ```SQL ... ```
_RE_SQL_FENCE = re.compile(r'```\s*sql\b.*?```', re.IGNORECASE | re.DOTALL)
# 일반 코드 블록: ``` ... ```
_RE_CODE_FENCE = re.compile(r'```[^\n]*\n(.*?)```', re.DOTALL)
# SQL 구문 시작 키워드 (줄 단위)
_RE_SQL_LINE = re.compile(
    r'^\s*(SELECT\b|INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b'
    r'|CREATE\s+(TABLE|INDEX|VIEW|DATABASE)\b|DROP\s+(TABLE|INDEX|VIEW)\b'
    r'|ALTER\s+TABLE\b|TRUNCATE\b|EXPLAIN\s+SELECT\b|WITH\s+\w+\s+AS\s*\()',
    re.IGNORECASE,
)
# SQL 연속 줄 (FROM / WHERE / JOIN 등 SQL 절)
_RE_SQL_CONTINUATION = re.compile(
    r'^\s*(FROM|WHERE|JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN'
    r'|OUTER\s+JOIN|ON\b|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET'
    r'|AND\b|OR\b|SET\b|VALUES\b|INTO\b|UNION\b|EXCEPT\b|INTERSECT\b)\b',
    re.IGNORECASE,
)


def strip_sql(text: str) -> tuple:
    """
    텍스트에서 SQL 쿼리를 제거합니다.
    반환값: 제거된 SQL 블록 수

    제거 대상:
      1) ```sql ... ``` 명시적 SQL 코드 블록
      2) 일반 코드 블록(``` ```) 중 SQL 키워드를 포함한 것
      3) SELECT / INSERT / UPDATE / DELETE 등 SQL 구문으로 시작하는 연속 줄 블록
    """
    removed = 0

    # 1. 명시적 SQL 펜스 제거
    new_text, n = _RE_SQL_FENCE.subn('', text)
    removed += n

    # 2. 일반 코드 블록 중 SQL 포함된 것 제거
    def _drop_sql_fence(m):
        nonlocal removed
        if _RE_SQL_LINE.search(m.group(0)):
            removed += 1
            return ''
        return m.group(0)
    new_text = _RE_CODE_FENCE.sub(_drop_sql_fence, new_text)

    # 3. 인라인 SQL 구문 블록 제거 (연속된 SQL 줄)
    lines = new_text.split('\n')
    cleaned = []
    in_sql = False
    for line in lines:
        if _RE_SQL_LINE.match(line):
            in_sql = True
            removed += 1
            continue
        if in_sql and (not line.strip() or _RE_SQL_CONTINUATION.match(line) or line.strip().startswith('--')):
            continue
        in_sql = False
        cleaned.append(line)

    return '\n'.join(cleaned), removed


# ---------------------------------------------------------------------------
# tiktoken 인코더 싱글톤 컨테이너 (thread-safe lazy init)
_encoder = None
_encoder_lock = threading.Lock()

def _get_encoder():
    """
    tiktoken 인코더 반환 (Lazy initialization)

    Returns:
        tiktoken Encoding 객체, 또는 None (tiktoken 미설치 시)
    """
    global _encoder
    if not _TIKTOKEN_AVAILABLE:
        return None
    with _encoder_lock:
        if _encoder is None:
            try:
                _encoder = tiktoken.get_encoding(settings.TIKTOKEN_ENCODING)
            except Exception as e:
                logger.warning(f"Failed to load tiktoken encoding '{settings.TIKTOKEN_ENCODING}': {e}. Falling back to word-based chunking.")
                return None
    return _encoder


def _extract_text_from_adf_node(node):
    text_content = ""
    if isinstance(node, dict):
        if node.get('type') == 'text' and 'text' in node:
            text_content += node['text'] + " "
        if 'content' in node:
            for child_node in node['content']:
                text_content += _extract_text_from_adf_node(child_node)
    elif isinstance(node, list):
        for item in node:
            text_content += _extract_text_from_adf_node(item)
    return text_content.strip()

def convert_adf_to_plain_text(adf_json):
    if isinstance(adf_json, dict) and adf_json.get('type') == 'doc' and 'content' in adf_json:
        return _extract_text_from_adf_node(adf_json['content'])
    return str(adf_json) # Fallback if it's not a valid ADF doc

def chunk_content(content: str, chunk_size: int = None, chunk_overlap: int = None) -> List[str]:
    """
    텍스트 콘텐츠를 토큰 수 기준으로 청크로 분할합니다.

    tiktoken이 설치되어 있으면 토큰 수 기준으로 청크합니다.
    한국어 등 비영어권에서도 이모지/턼스트 토큰이 정확히 카운트됩니다.
    tiktoken이 없으면 공백(space) 기준 단어 분리로 fallback합니다.

    Args:
        content: 분할할 텍스트
        chunk_size: 청크 크기 (토큰 수, 기본값: settings.CHUNK_SIZE)
        chunk_overlap: 청크 중복 (토큰 수, 기본값: settings.CHUNK_OVERLAP)

    Returns:
        청크 리스트
    """
    if chunk_size is None:
        chunk_size = settings.CHUNK_SIZE
    if chunk_overlap is None:
        chunk_overlap = settings.CHUNK_OVERLAP

    if not content:
        return []

    encoder = _get_encoder()

    if encoder is not None:
        return _chunk_by_tokens(content, encoder, chunk_size, chunk_overlap)
    else:
        return _chunk_by_words(content, chunk_size, chunk_overlap)


def _chunk_by_tokens(content: str, encoder, chunk_size: int, chunk_overlap: int) -> List[str]:
    """
    tiktoken 토큰 ID 배열로 청크를 분할합니다.
    """
    token_ids = encoder.encode(content)
    total_tokens = len(token_ids)

    if total_tokens <= chunk_size:
        return [content]

    chunks = []
    step = max(chunk_size - chunk_overlap, 1)  # step이 0 이하가 되지 않도록
    for start in range(0, total_tokens, step):
        end = min(start + chunk_size, total_tokens)
        chunk_token_ids = token_ids[start:end]
        chunk_text = encoder.decode(chunk_token_ids)
        if chunk_text.strip():
            chunks.append(chunk_text)
        if end >= total_tokens:
            break
    return chunks


def _chunk_by_words(content: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    """
    공백 기준 단어로 청크를 분할합니다. (tiktoken fallback)
    """
    logger.debug("Using word-based chunking (tiktoken not available).")
    words = content.split()
    if len(words) <= chunk_size:
        return [" ".join(words)]

    chunks = []
    step = max(chunk_size - chunk_overlap, 1)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def _upsert_with_fallback(collection, chunk: str, metadata: dict, chunk_id: str, stats: dict, is_existing: bool):
    """
    청크를 ChromaDB에 upsert합니다.
    토큰 초과 오류 발생 시 절반 크기로 분할하여 sub-chunk ID로 재시도합니다.
    """
    try:
        collection.upsert(documents=[chunk], metadatas=[metadata], ids=[chunk_id])
        if is_existing:
            stats["updated"] += 1
            logger.debug(f"Updated chunk {chunk_id}.")
        else:
            stats["new"] += 1
            logger.debug(f"Added chunk {chunk_id}.")
    except Exception as e:
        err = str(e).lower()
        # 토큰/길이 초과는 결정론적 오류 → sub-chunk로 분할
        # rate limit 등 일시적 오류는 여기서 처리하지 않고 상위로 전파
        if any(k in err for k in ("token", "too long", "maximum", "context length")):
            words = chunk.split()
            half = max(len(words) // 2, 1)
            sub_chunks = [" ".join(words[j:j + half]) for j in range(0, len(words), half)]
            logger.warning(
                f"Chunk {chunk_id} failed ({len(words)} words). "
                f"Splitting into {len(sub_chunks)} sub-chunks and retrying."
            )
            for j, sub_chunk in enumerate(sub_chunks):
                sub_id = f"{chunk_id}-sub-{j}"
                sub_hash = hashlib.md5(sub_chunk.encode()).hexdigest()
                sub_metadata = {**metadata, "content_hash": sub_hash}
                try:
                    collection.upsert(documents=[sub_chunk], metadatas=[sub_metadata], ids=[sub_id])
                    stats["new"] += 1
                    logger.debug(f"Added sub-chunk {sub_id}.")
                except Exception as sub_e:
                    logger.error(f"Sub-chunk {sub_id} also failed: {sub_e}")
                    stats.setdefault("failed", 0)
                    stats["failed"] += 1
        else:
            raise


_PROGRESS_EVERY = 50  # 몇 문서마다 진행 현황을 출력할지


def _fmt_elapsed(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def load_data_to_chromadb(data_stream):
    """
    JSONL 데이터를 읽고, 청크로 분할하고, 임베딩을 생성하여 ChromaDB에 로드합니다.

    Args:
        data_stream: JSONL 라인의 iterable
    """
    collection = db_manager.get_collection()
    stats = {"new": 0, "updated": 0, "skipped": 0}

    logger.info("문서 로드 시작 (스트리밍 처리).")
    start_time = time.time()
    doc_count = 0
    chunk_count = 0

    for line in data_stream:
        if not line.strip():
            continue
        try:
            document = json.loads(line)
            doc_id = document.get("id")
            source = document.get("source")
            title = document.get("title", "")
            content = document.get("content", "")
            url = document.get("url", "")
            created_at = document.get("created_at", "")
            updated_at = document.get("updated_at", "")
            author = document.get("author", "")
            content_type = document.get("content_type", "")
            metadata_from_source = document.get("metadata", {})

            # Convert ADF content to plain text if necessary
            if isinstance(content, dict) and content.get('type') == 'doc':
                content = convert_adf_to_plain_text(content)
            
            if not doc_id or not content:
                logger.warning(f"Skipping document due to missing ID or content: {document.get('id')}")
                continue

            embed_text = f"{title}\n\n{content}" if title else content
            embed_text, sql_removed = strip_sql(embed_text)
            if sql_removed:
                logger.debug(f"[{doc_id}] SQL {sql_removed}개 블록 제거됨")
            chunks = chunk_content(embed_text)
            doc_count += 1
            chunk_count += len(chunks)

            if doc_count % _PROGRESS_EVERY == 0:
                elapsed = time.time() - start_time
                total_chunks_done = stats["new"] + stats["updated"] + stats["skipped"]
                logger.info(
                    f"[{doc_count}] "
                    f"청크: {total_chunks_done:,} "
                    f"(new {stats['new']:,} | updated {stats['updated']:,} | skipped {stats['skipped']:,}) "
                    f"| 경과: {_fmt_elapsed(elapsed)}"
                )

            for i, chunk in enumerate(chunks):
                chunk_id = f"{doc_id}-chunk-{i}"
                
                # Combine base metadata and source-specific metadata
                # Ensure all metadata values are primitive types (str, int, float, bool)
                # Nested structures or lists like 'comments' should be stringified if needed for metadata
                
                # Create a shallow copy to avoid modifying original metadata_from_source
                metadata_to_store = {k: v for k, v in metadata_from_source.items() if isinstance(v, (str, int, float, bool))}
                
                # Add top-level fields to metadata
                metadata_to_store.update({
                    "source": source,
                    "title": title,
                    "url": url,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "author": author,
                    "content_type": content_type,
                    "original_doc_id": doc_id # Reference to the original document ID
                })

                # Handle comments - stringify if they are lists or complex objects
                if "comments" in metadata_from_source:
                    metadata_to_store["comments"] = json.dumps(metadata_from_source["comments"], ensure_ascii=False)
                if "replies" in metadata_from_source:
                    metadata_to_store["replies"] = json.dumps(metadata_from_source["replies"], ensure_ascii=False)

                # ChromaDB requires all metadata values to be of type str, int, float, or bool.
                # Ensure complex objects within metadata_to_store are stringified.
                for key, value in metadata_to_store.items():
                    if not isinstance(value, (str, int, float, bool)):
                        metadata_to_store[key] = str(value)

                # 콘텐츠 해시: 동일한 내용이면 임베딩 재생성 스킵 (비용 절감)
                content_hash = hashlib.md5(chunk.encode()).hexdigest()
                metadata_to_store["content_hash"] = content_hash

                try:
                    existing = collection.get(ids=[chunk_id], include=["metadatas"])
                    if existing["ids"] and existing["metadatas"][0].get("content_hash") == content_hash:
                        logger.debug(f"Skipping unchanged chunk {chunk_id}.")
                        stats["skipped"] += 1
                        continue

                    _upsert_with_fallback(collection, chunk, metadata_to_store, chunk_id, stats, existing["ids"])
                except Exception as e:
                    logger.error(f"Error upserting chunk {chunk_id} to ChromaDB: {e}", exc_info=True)

        except json.JSONDecodeError as e:
            logger.warning(f"Skipping invalid JSONL line: {line.strip()[:100]}... - Error: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred while processing line: {line.strip()[:100]}... - Error: {e}", exc_info=True)

    elapsed = time.time() - start_time
    failed = stats.get("failed", 0)
    logger.info(
        f"완료 | 문서: {doc_count:,} | 청크: {chunk_count:,} "
        f"(new {stats['new']:,} | updated {stats['updated']:,} | skipped {stats['skipped']:,} | failed {failed:,}) "
        f"| 총 소요: {_fmt_elapsed(elapsed)}"
    )


if __name__ == "__main__":
    logger.info(f"Loading data into ChromaDB collection: {settings.COLLECTION_NAME} at path: {settings.CHROMA_DB_PATH}")
    # Read from stdin
    load_data_to_chromadb(iter(lambda: sys.stdin.readline().strip(), ''))
    logger.info("Data loading complete.")

    # Print stats
    stats = db_manager.get_collection_stats()
    logger.info(f"Total documents in collection: {stats['count']}")


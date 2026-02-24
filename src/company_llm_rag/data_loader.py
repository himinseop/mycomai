import sys
import json
import hashlib
from typing import List

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

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
    텍스트 콘텐츠를 작은 청크로 분할합니다.

    Args:
        content: 분할할 텍스트
        chunk_size: 청크 크기 (단어 수, 기본값: settings.CHUNK_SIZE)
        chunk_overlap: 청크 중복 (단어 수, 기본값: settings.CHUNK_OVERLAP)

    Returns:
        청크 리스트
    """
    if chunk_size is None:
        chunk_size = settings.CHUNK_SIZE
    if chunk_overlap is None:
        chunk_overlap = settings.CHUNK_OVERLAP

    chunks = []
    if not content:
        return chunks

    words = content.split()
    if len(words) <= chunk_size:
        return [" ".join(words)]

    for i in range(0, len(words), chunk_size - chunk_overlap):
        chunk = " ".join(words[i:i + chunk_size])
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
        if any(k in err for k in ("token", "too long", "maximum", "rate", "context length")):
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


def load_data_to_chromadb(data_stream):
    """
    JSONL 데이터를 읽고, 청크로 분할하고, 임베딩을 생성하여 ChromaDB에 로드합니다.

    Args:
        data_stream: JSONL 라인의 iterable
    """
    collection = db_manager.get_collection()
    stats = {"new": 0, "updated": 0, "skipped": 0}

    for line in data_stream:
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

            chunks = chunk_content(content)
            
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

    failed = stats.get("failed", 0)
    logger.info(
        f"Load complete — new: {stats['new']}, updated: {stats['updated']}, "
        f"skipped (unchanged): {stats['skipped']}, failed: {failed}"
    )


if __name__ == "__main__":
    logger.info(f"Loading data into ChromaDB collection: {settings.COLLECTION_NAME} at path: {settings.CHROMA_DB_PATH}")
    # Read from stdin
    load_data_to_chromadb(iter(lambda: sys.stdin.readline().strip(), ''))
    logger.info("Data loading complete.")

    # Print stats
    stats = db_manager.get_collection_stats()
    logger.info(f"Total documents in collection: {stats['count']}")


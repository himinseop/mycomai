import sys
import json
from typing import List

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager

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

def load_data_to_chromadb(data_stream):
    """
    JSONL 데이터를 읽고, 청크로 분할하고, 임베딩을 생성하여 ChromaDB에 로드합니다.

    Args:
        data_stream: JSONL 라인의 iterable
    """
    collection = db_manager.get_collection()

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
                print(f"Skipping document due to missing ID or content: {document.get('id')}", file=sys.stderr)
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


                try:
                    collection.upsert(
                        documents=[chunk],
                        metadatas=[metadata_to_store],
                        ids=[chunk_id]
                    )
                    # print(f"Loaded/Updated chunk {chunk_id} in ChromaDB.")
                except Exception as e:
                    print(f"Error upserting chunk {chunk_id} to ChromaDB: {e}", file=sys.stderr)

        except json.JSONDecodeError as e:
            print(f"Skipping invalid JSONL line: {line.strip()} - Error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"An unexpected error occurred while processing line: {line.strip()} - Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    print(f"Loading data into ChromaDB collection: {settings.COLLECTION_NAME} at path: {settings.CHROMA_DB_PATH}", file=sys.stderr)
    # Read from stdin
    load_data_to_chromadb(iter(lambda: sys.stdin.readline().strip(), ''))
    print("Data loading complete.", file=sys.stderr)

    # Print stats
    stats = db_manager.get_collection_stats()
    print(f"Total documents in collection: {stats['count']}", file=sys.stderr)


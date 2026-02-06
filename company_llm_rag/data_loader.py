import sys
import json
import os
from typing import List, Dict

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv # Added for loading .env file

load_dotenv() # Load environment variables from .env file

# Environment variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
CHROMA_DB_PATH = os.getenv('CHROMA_DB_PATH', "./chroma_db") # Path for persistent ChromaDB storage
COLLECTION_NAME = os.getenv('COLLECTION_NAME', "company_llm_rag_collection")

# Initialize OpenAI embedding function for ChromaDB
if not OPENAI_API_KEY:
    print("Please set the OPENAI_API_KEY environment variable.")
    exit(1)

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=OPENAI_API_KEY,
    model_name="text-embedding-ada-002"
)

# Initialize ChromaDB client
client = chromadb.PersistentClient(path=CHROMA_DB_PATH)

# Get or create collection
collection = client.get_or_create_collection(
    name=COLLECTION_NAME,
    embedding_function=openai_ef
)

def chunk_content(content: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
    """
    Splits text content into smaller chunks.
    A simple fixed-size chunking strategy. More advanced chunking can be implemented.
    """
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
    Reads JSONL data, chunks content, generates embeddings, and loads into ChromaDB.
    """
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

            if not doc_id or not content:
                print(f"Skipping document due to missing ID or content: {document.get('id')}", file=os.stderr)
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
                    collection.add(
                        documents=[chunk],
                        metadatas=[metadata_to_store],
                        ids=[chunk_id]
                    )
                    # print(f"Loaded chunk {chunk_id} into ChromaDB.")
                except Exception as e:
                    print(f"Error loading chunk {chunk_id} to ChromaDB: {e}", file=os.stderr)

        except json.JSONDecodeError as e:
            print(f"Skipping invalid JSONL line: {line.strip()} - Error: {e}", file=os.stderr)
        except Exception as e:
            print(f"An unexpected error occurred while processing line: {line.strip()} - Error: {e}", file=os.stderr)


if __name__ == "__main__":
    print(f"Loading data into ChromaDB collection: {COLLECTION_NAME} at path: {CHROMA_DB_PATH}")
    # Read from stdin
    load_data_to_chromadb(iter(lambda: sys.stdin.readline().strip(), ''))
    print("Data loading complete.")


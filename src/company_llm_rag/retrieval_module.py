import json
from typing import List, Dict

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager

def retrieve_documents(query: str, n_results: int = None) -> List[Dict]:
    """
    ChromaDB에서 사용자 쿼리와 관련된 문서 청크를 검색합니다.

    Args:
        query: 검색 쿼리
        n_results: 반환할 결과 개수 (기본값: settings.RETRIEVAL_TOP_K)

    Returns:
        검색된 문서 리스트
    """
    if n_results is None:
        n_results = settings.RETRIEVAL_TOP_K

    try:
        collection = db_manager.get_collection()
        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            include=['documents', 'metadatas']
        )

        retrieved_docs = []
        if results and results['documents'] and len(results['documents']) > 0:
            for i in range(len(results['documents'][0])):
                doc_content = results['documents'][0][i]
                metadata = results['metadatas'][0][i]

                # Reconstruct comments/replies if they were stringified in metadata
                if "comments" in metadata and isinstance(metadata["comments"], str):
                    try:
                        metadata["comments"] = json.loads(metadata["comments"])
                    except json.JSONDecodeError:
                        pass
                if "replies" in metadata and isinstance(metadata["replies"], str):
                    try:
                        metadata["replies"] = json.loads(metadata["replies"])
                    except json.JSONDecodeError:
                        pass

                retrieved_docs.append({
                    "content": doc_content,
                    "metadata": metadata
                })
        return retrieved_docs
    except Exception as e:
        print(f"Error during document retrieval: {e}")
        return []

if __name__ == "__main__":
    stats = db_manager.get_collection_stats()
    print(f"Retrieval module connected to ChromaDB collection: {stats['name']}")
    print(f"  - Path: {stats['path']}")
    print(f"  - Documents: {stats['count']}")

    while True:
        try:
            user_query = input("\nEnter your query (or 'exit' to quit): ")
            if user_query.lower() == 'exit':
                break

            results = retrieve_documents(user_query)
            if results:
                print("\n--- Retrieved Documents ---")
                for i, doc in enumerate(results):
                    print(f"Document {i+1}:")
                    print(f"  Content (chunk): {doc['content'][:200]}...")
                    print(f"  Source: {doc['metadata'].get('source')}")
                    print(f"  Title: {doc['metadata'].get('title')}")
                    print(f"  URL: {doc['metadata'].get('url')}")
                    print(f"  Original Doc ID: {doc['metadata'].get('original_doc_id')}")
                    print("-" * 30)
            else:
                print("No relevant documents found.")
        except EOFError:
            break
    print("Exiting retrieval module.")

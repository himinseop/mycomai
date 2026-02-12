import os
import json
from typing import List, Dict

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

load_dotenv()

# Environment variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
CHROMA_DB_PATH = os.getenv('CHROMA_DB_PATH', "./chroma_db")
COLLECTION_NAME = os.getenv('COLLECTION_NAME', "company_llm_rag_collection")

# Initialize OpenAI embedding function for ChromaDB
if not OPENAI_API_KEY:
    print("Please set the OPENAI_API_KEY environment variable.")
    exit(1)

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=OPENAI_API_KEY,
    model_name="text-embedding-3-small"
)

# Initialize ChromaDB client
client = chromadb.PersistentClient(path=CHROMA_DB_PATH)

# Get collection
try:
    collection = client.get_collection(
        name=COLLECTION_NAME,
        embedding_function=openai_ef
    )
except Exception as e:
    # If not found, try to get or create (for robust testing)
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=openai_ef
    )

def retrieve_documents(query: str, n_results: int = 5) -> List[Dict]:
    """
    Retrieves relevant document chunks from ChromaDB based on a user query.
    """
    try:
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
    print(f"Retrieval module connected to ChromaDB collection: {COLLECTION_NAME} at path: {CHROMA_DB_PATH}")
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

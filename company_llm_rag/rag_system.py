import os
import json
from typing import List, Dict

import openai # pip install openai
from retrieval_module import retrieve_documents # Assuming retrieval_module is in the same directory
from dotenv import load_dotenv # Added for loading .env file

load_dotenv() # Load environment variables from .env file

# Environment variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

if not OPENAI_API_KEY:
    print("Please set the OPENAI_API_KEY environment variable.")
    exit(1)

openai.api_key = OPENAI_API_KEY

def build_rag_prompt(user_query: str, retrieved_docs: List[Dict]) -> str:
    """
    Constructs a prompt for the LLM using the user's query and retrieved documents.
    """
    context_parts = []
    for i, doc in enumerate(retrieved_docs):
        # Extract metadata for better context
        source = doc['metadata'].get('source', 'unknown')
        title = doc['metadata'].get('title', 'Untitled')
        url = doc['metadata'].get('url', 'No URL')
        
        # Include comments/replies if available
        comments_or_replies = []
        if source == "jira" and doc['metadata'].get('comments'):
            comments_or_replies = [f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}" for c in doc['metadata']['comments']]
        elif source == "confluence" and doc['metadata'].get('comments'):
            comments_or_replies = [f"Comment by {c.get('author')} on {c.get('created_at')}: {c.get('content')}" for c in doc['metadata']['comments']]
        elif source == "teams" and doc['metadata'].get('replies'):
            comments_or_replies = [f"Reply by {r.get('sender')} on {r.get('created_at')}: {r.get('content')}" for r in doc['metadata']['replies']]

        comments_or_replies_str = "
".join(comments_or_replies) if comments_or_replies else ""

        context_parts.append(f"--- Document {i+1} (Source: {source}, Title: {title}, URL: {url}) ---
"
                             f"{doc['content']}
"
                             f"{comments_or_replies_str}"
                             f"----------------------------------------------------------")

    context = "

".join(context_parts)

    prompt = (
        "You are an AI assistant for a company. Your task is to answer questions based on the provided company knowledge base. "
        "Use only the information from the documents provided below to answer the question. "
        "If the answer cannot be found in the documents, state that you don't have enough information. "
        "Do not make up any information.

"
        "Company Knowledge Base:
"
        f"{context}

"
        f"User Query: {user_query}

"
        "Answer:"
    )
    return prompt

def get_llm_response(prompt: str, model: str = "gpt-3.5-turbo", temperature: float = 0.7) -> str:
    """
    Gets a response from the LLM.
    """
    try:
        response = openai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error getting response from LLM: {e}"

def rag_query(user_query: str) -> str:
    """
    Executes a RAG query: retrieves documents and generates an LLM response.
    """
    retrieved_docs = retrieve_documents(user_query)
    
    if not retrieved_docs:
        return "I could not find any relevant information in the company knowledge base for your query."
    
    prompt = build_rag_prompt(user_query, retrieved_docs)
    llm_response = get_llm_response(prompt)
    
    return llm_response

if __name__ == "__main__":
    print("Company LLM RAG System ready. Type 'exit' to quit.")
    while True:
        query = input("
Enter your query: ")
        if query.lower() == 'exit':
            break
        
        response = rag_query(query)
        print("
LLM Response:")
        print(response)

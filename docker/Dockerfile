# Use an official Python runtime as a parent image
FROM python:3.11-slim-bookworm

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY src/requirements.txt .

# Install any needed packages specified in requirements.txt
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY src/company_llm_rag/ company_llm_rag/
COPY .env.sample .env.sample

# Define environment variables (these will be overwritten by host .env or Docker run args)
# It's better to pass them at runtime using docker run -e KEY=VALUE or docker compose
ENV PYTHONPATH="/app"
ENV CHROMA_DB_PATH="./chroma_db"
ENV COLLECTION_NAME="company_llm_rag_collection"

# Set default command for the container
# This command will be overridden by passing a command to docker run
# Example: docker run --rm -it mycomai-rag python3 company_llm_rag/rag_system.py
# Example: docker run --rm -it mycomai-rag python3 company_llm_rag/data_loader.py
CMD ["python3"]

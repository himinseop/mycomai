도커 컨테이너 구성을 위한 `Dockerfile`이 현재 디렉토리(`/Users/himinseop/Dev/_docker/himinseop/mycomai/`)에 있습니다.

### **Docker 컨테이너 사용 방법:**

**1. Docker 이미지 빌드:**
   - 현재 디렉토리(`/Users/himinseop/Dev/_docker/himinseop/mycomai/`)에서 다음 명령어를 실행하여 Docker 이미지를 빌드합니다.
   - `mycomai-rag`는 이미지 이름이며, 원하시는 다른 이름으로 변경할 수 있습니다.
   ```bash
   docker build -t mycomai-rag .
   ```
   - 빌드 과정은 `requirements.txt`에 명시된 라이브러리들을 설치하므로 시간이 다소 소요될 수 있습니다.

**2. 대화형 컨테이너 실행 및 접속:**
   - 이제 빌드된 이미지로 컨테이너를 실행하고, 컨테이너 내부에 `bash` 셸로 접속할 수 있습니다.
   - 이때, 필요한 환경 변수 파일(`.env`)과 ChromaDB 데이터, 그리고 실제 프로젝트 코드 디렉토리(`company_llm_rag`)를 마운트하여 컨테이너 내부에서 접근할 수 있도록 합니다.

   ```bash
   docker run -it --rm \
     -v "$(pwd)/.env:/app/.env" \
     -v "$(pwd)/chroma_db:/app/chroma_db" \
     -v "$(pwd)/company_llm_rag:/app/company_llm_rag" \
     mycomai-rag \
     bash
   ```
   *   `-v "$(pwd)/company_llm_rag:/app/company_llm_rag"`: 호스트의 `company_llm_rag` 디렉토리를 컨테이너 내부의 `/app/company_llm_rag`로 마운트합니다. 이렇게 하면 호스트에서 코드를 수정해도 컨테이너에 즉시 반영됩니다.

**3. 컨테이너 내부에서 스크립트 실행:**
   - 위 명령어를 실행하면 컨테이너 내부의 `/app` 디렉토리에서 `bash` 셸 프롬프트가 나타날 것입니다 (예: `root@<container_id>:/app#`).
   - 이제 컨테이너 내부에서 다음 단계를 진행할 수 있습니다:

   **A. 환경 변수 로드:**
     ```bash
     source .env
     ```
     *   이제 `echo $OPENAI_API_KEY` 등으로 변수 로드 여부를 확인할 수 있습니다.

   **B. 데이터 추출 (예시: Jira):**
     ```bash
     python3 company_llm_rag/data_extraction/jira/jira_extractor.py 2> jira_errors.log > jira_data.jsonl
     ```
     *   `2>` 와 `>`를 사용한 리디렉션은 호스트 시스템의 현재 디렉토리(컨테이너 내부의 `/app/`)에 파일을 생성합니다.

   **C. 모든 데이터 추출 및 ChromaDB에 로드:**
     ```bash
     python3 company_llm_rag/data_extraction/jira/jira_extractor.py 2> jira_errors.log > jira_data.jsonl
     python3 company_llm_rag/data_extraction/confluence/confluence_extractor.py 2> confluence_errors.log > confluence_data.jsonl
     python3 company_llm_rag/data_extraction/m365/sharepoint_extractor.py 2> sharepoint_errors.log > sharepoint_data.jsonl
     python3 company_llm_rag/data_extraction/m365/teams_extractor.py 2> teams_errors.log > teams_data.jsonl
     cat jira_data.jsonl confluence_data.jsonl sharepoint_data.jsonl teams_data.jsonl | python3 company_llm_rag/data_loader.py
     ```
     *   이 명령어들은 컨테이너 내부에서 실행되므로, `.jsonl` 파일들도 컨테이너 내부의 `/app` 디렉토리 (호스트의 마운트된 디렉토리와 동기화됨)에 생성됩니다.

   **D. RAG 시스템 실행:**
     ```bash
     python3 company_llm_rag/rag_system.py
     ```

**4. 컨테이너 종료:**
   - 컨테이너 내부 셸에서 `exit`를 입력하거나 `Ctrl+D`를 누르면 컨테이너가 종료됩니다 (`--rm` 옵션 때문에 자동으로 제거됩니다).

이 방식을 사용하면 컨테이너 환경에서 자유롭게 스크립트를 실행하고 디버깅할 수 있습니다.

**먼저 Docker 이미지를 빌드해 주세요.** 다음 명령어를 실행하신 후, 빌드 결과와 이후 컨테이너 실행에 대해 다시 논의해 드리겠습니다.

```bash
docker build -t mycomai-rag .
```
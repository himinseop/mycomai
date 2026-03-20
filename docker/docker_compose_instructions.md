도커로 이 프로젝트를 배포하기 위한 가장 좋은 구성은 **Docker Compose**를 사용하는 것입니다. Docker Compose는 여러 컨테이너와 그들의 설정(네트워크, 볼륨 등)을 하나의 파일(`docker-compose.yml`)로 관리할 수 있게 해주어, 길고 복잡한 `docker run` 명령어를 매번 입력할 필요가 없게 해줍니다.

제가 방금 프로젝트의 루트 디렉토리(`/Users/himinseop/Dev/_docker/himinseop/mycomai/`)에 `docker-compose.yml` 파일을 생성했습니다.

이 파일은 다음과 같이 구성되어 있습니다:

*   **`base` 서비스:** 모든 서비스의 기본이 되는 설정입니다. `mycomai-rag` 이미지를 빌드하고, `.env` 파일과 `chroma_db` 볼륨을 마운트합니다.
*   **`data-loader` 서비스:** `base` 서비스를 상속받아, 데이터 추출 및 로딩 파이프라인을 실행하는 **일회성 작업**을 위한 서비스입니다.
*   **`rag-system` 서비스:** `base` 서비스를 상속받아, 사용자와 상호작용하는 RAG 시스템을 실행하는 **장기 실행 서비스**입니다.

### **Docker Compose 사용 방법:**

**1. Docker 이미지 빌드:**
   - `docker-compose.yml`이 있는 디렉토리(`/Users/himinseop/Dev/_docker/himinseop/mycomai/`)에서 다음 명령어를 실행하여 이미지를 빌드합니다.
   - `docker-compose`는 `docker-compose.yml` 파일을 자동으로 찾아 설정대로 이미지를 빌드합니다.
   ```bash
   docker-compose build
   ```

**2. 데이터 추출 및 로드 실행:**
   - 다음 명령어를 실행하여 `data-loader` 서비스를 **일회성**으로 실행합니다. 이 서비스는 모든 추출기 스크립트를 순서대로 실행하고, 그 결과를 `data_loader.py`로 파이프하여 ChromaDB에 데이터를 로드한 후 종료됩니다.
   - `--rm` 옵션은 작업 완료 후 컨테이너를 자동으로 삭제합니다.
   ```bash
   docker-compose run --rm data-loader
   ```

**3. RAG 시스템 실행:**
   - 데이터 로딩이 성공적으로 완료되면, 다음 명령어를 실행하여 RAG 시스템을 시작합니다.
   - `-d` 옵션은 컨테이너를 백그라운드에서 실행합니다.
   ```bash
   docker-compose up -d rag-system
   ```
   - RAG 시스템과 상호작용하려면, 다음 명령어를 사용하여 실행 중인 컨테이너에 접속합니다:
   ```bash
   docker-compose attach rag-system
   ```
   - 프롬프트에 질문을 입력하여 RAG 시스템과 상호작용할 수 있습니다.
   - 접속을 끊으려면 `Ctrl+P`를 누른 후 `Ctrl+Q`를 누릅니다 (컨테이너는 계속 실행됩니다).

**4. RAG 시스템 중지:**
   - 백그라운드에서 실행 중인 RAG 시스템을 중지하고 컨테이너를 제거하려면 다음 명령어를 실행합니다:
   ```bash
   docker-compose down
   ```

이 구성을 사용하면 데이터 로딩과 RAG 시스템 실행을 명확하게 분리하여 관리할 수 있으며, 다른 시스템에서도 동일한 명령어로 쉽게 프로젝트를 구동할 수 있습니다.

**먼저 `docker-compose build` 명령어를 실행하여 이미지를 빌드해 주세요.**
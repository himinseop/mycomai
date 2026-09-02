"""
개발 문서 저장소(git checkout) 마크다운 수집기

로컬에 체크아웃된 문서 저장소(예: bitbucket o2olab/docs)를 읽어
지정된 하위 디렉토리의 마크다운 문서를 표준 스키마 JSONL로 출력합니다.

- 컨테이너에는 git 바이너리가 없으므로 .git/HEAD, refs를 직접 파싱해
  브랜치·커밋을 확인합니다 (저장소를 변경하지 않음, read-only).
- DOCS_REPO_BRANCH가 설정된 경우 체크아웃 브랜치가 다르면 수집을 건너뜁니다
  (작업 중 다른 브랜치 내용이 인덱스를 오염시키는 것 방지).
- README.md는 작성 규칙/목차 문서이므로 수집에서 제외합니다.
"""

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from company_llm_rag.config import settings
from company_llm_rag.data_extraction.common import emit_document, fmt_elapsed
from company_llm_rag.data_extraction.docs_repo.digest_parser import parse_digest
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_MIN_CONTENT_CHARS = 50
_DIGEST_CATEGORY = "digests"  # platform/sharepoint-index/digests → category="digests"


def category_from_path(relpath: str) -> str:
    """저장소 루트 기준 상대 경로에서 문서 카테고리를 판별합니다.

    DOCS_REPO_SUBDIRS 중 relpath의 접두사가 되는 항목을 찾아 그 마지막 경로
    구성요소(예: "platform/features" → "features")를 카테고리로 사용합니다.
    로컬 체크아웃 수집(main())과 S3 수집(s3_docs_ingest.py)이 동일한 판별 규칙을
    공유해 카테고리 분류가 어긋나지 않도록 합니다.
    """
    for subdir in settings.DOCS_REPO_SUBDIRS:
        prefix = subdir.rstrip("/") + "/"
        if relpath.startswith(prefix):
            return Path(subdir).name
    return Path(relpath).parent.name


def read_git_info(repo_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """git 바이너리 없이 현재 브랜치명과 커밋 해시(7자리)를 읽습니다."""
    git_dir = repo_path / ".git"
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return None, None

    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        # detached HEAD: 내용이 곧 커밋 해시
        return None, head[:7]

    ref = head[len("ref: "):]  # 예: refs/heads/feature/INFRA-39
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    commit = None
    ref_file = git_dir / ref
    if ref_file.is_file():
        commit = ref_file.read_text(encoding="utf-8").strip()[:7]
    else:
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    commit = line.split(" ", 1)[0][:7]
                    break
    return branch, commit


def extract_title(content: str, fallback: str) -> str:
    """첫 번째 H1 헤딩을 제목으로 사용, 없으면 파일명."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def build_doc_url(branch: str, relpath: str) -> str:
    """bitbucket 소스 뷰 URL을 생성합니다."""
    base = settings.DOCS_REPO_URL_BASE.rstrip("/")
    if not base:
        return ""
    return f"{base}/{branch}/{relpath}"


def build_document(
    *, relpath: str, content: str, updated_at: str,
    branch: str = "", commit: str = "", doc_url: str = "",
) -> Optional[dict]:
    """마크다운 문서 1건을 표준 스키마 dict로 변환합니다.

    로컬 체크아웃 수집(main())과 S3 수집(s3_docs_ingest.py, #62)이 공유하는 파싱 로직 —
    카테고리 판별, 제목 추출, 다이제스트(#61) 헤더 파싱을 한 곳에서 수행합니다.
    내용이 _MIN_CONTENT_CHARS 미만이면 None을 반환합니다(호출부에서 스킵 로그 처리).
    """
    if len(content.strip()) < _MIN_CONTENT_CHARS:
        return None

    category = category_from_path(relpath)
    emit_content = content
    metadata = {
        "docs_repo": settings.DOCS_REPO_NAME,
        "docs_branch": branch,
        "docs_commit": commit,
        "docs_category": category,
        "docs_relpath": relpath,
    }

    # 다이제스트(#61 11-A): 헤더 계약 파싱 — 원본 SharePoint URL이 노출용 url을 대체하고,
    # 원본/위치 줄은 본문에서 제거된다. 필드 파싱 실패는 경고만 남기고 수집은 계속한다.
    if category == _DIGEST_CATEGORY:
        parsed = parse_digest(content, relpath)
        emit_content = parsed["content"]
        metadata.update(parsed["metadata"])
        if parsed["url"]:
            doc_url = parsed["url"]
        for w in parsed["warnings"]:
            logger.warning(f"[Docs][digest] {w}")

    return {
        "id": f"docs-{relpath}",
        "source": "docs",
        "source_id": relpath,
        "url": doc_url,
        "title": extract_title(content, Path(relpath).stem),
        "content": emit_content,
        "content_type": "markdown",
        "created_at": "",
        "updated_at": updated_at,
        "author": "",
        "metadata": metadata,
    }


def main():
    if settings.PLATFORM_DOCS_S3_BUCKET:
        logger.info(
            "[Docs] S3 수집 모드 — 로컬 체크아웃 수집 건너뜀 "
            f"(PLATFORM_DOCS_S3_BUCKET={settings.PLATFORM_DOCS_S3_BUCKET!r})"
        )
        return

    repo_path = Path(settings.DOCS_REPO_PATH) if settings.DOCS_REPO_PATH else None
    if not repo_path or not repo_path.is_dir():
        logger.warning(
            f"DOCS_REPO_PATH가 없거나 디렉토리가 아님 — 수집 건너뜀 (path={settings.DOCS_REPO_PATH!r})"
        )
        return

    branch, commit = read_git_info(repo_path)
    if settings.DOCS_REPO_BRANCH and branch != settings.DOCS_REPO_BRANCH:
        logger.error(
            f"체크아웃 브랜치({branch!r})가 설정 브랜치({settings.DOCS_REPO_BRANCH!r})와 다름 — "
            f"인덱스 오염 방지를 위해 수집 건너뜀"
        )
        return

    logger.info(
        f"[Docs] 수집 시작: repo={settings.DOCS_REPO_NAME} branch={branch} commit={commit} "
        f"subdirs={settings.DOCS_REPO_SUBDIRS}"
    )
    start_time = time.time()
    total = 0

    for subdir in settings.DOCS_REPO_SUBDIRS:
        base_dir = repo_path / subdir
        if not base_dir.is_dir():
            logger.warning(f"[Docs] 하위 디렉토리 없음 — 건너뜀: {subdir}")
            continue

        category = Path(subdir).name  # 예: features, sites
        md_files = sorted(p for p in base_dir.rglob("*.md") if p.name.lower() != "readme.md")
        logger.info(f"[Docs][{category}] {len(md_files)}개 문서 발견")

        for md_file in md_files:
            try:
                content = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"[Docs] 파일 읽기 실패 — 건너뜀: {md_file} ({e})")
                continue
            if len(content.strip()) < _MIN_CONTENT_CHARS:
                logger.debug(f"[Docs][{category}] 내용 부족 스킵: {md_file.name}")
                continue

            relpath = md_file.relative_to(repo_path).as_posix()
            updated_at = datetime.fromtimestamp(
                md_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

            doc_url = build_doc_url(branch or "", relpath)
            doc = build_document(
                relpath=relpath, content=content, updated_at=updated_at,
                branch=branch or "", commit=commit or "", doc_url=doc_url,
            )
            if doc is None:
                logger.debug(f"[Docs][{category}] 내용 부족 스킵: {md_file.name}")
                continue

            emit_document(doc)
            total += 1

    logger.info(f"[Docs] 완료: {total}개 | 소요: {fmt_elapsed(time.time() - start_time)}")


if __name__ == "__main__":
    main()

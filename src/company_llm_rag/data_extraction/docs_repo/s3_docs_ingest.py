"""
플랫폼 문서 S3 수집 오케스트레이터 (#62)

Docs CI가 비공개 S3(o2olab-devops)에 게시한 플랫폼 문서 아티팩트를 읽어
ChromaDB + FTS에 색인합니다. 기존 추출기(→ JSONL stdout → data_loader.py stdin)와
달리 삭제·상태 갱신이 필요해 in-process 오케스트레이터로 구현합니다.

절차(docs/issues/62/design.md §3):
  1. current.json 조회 → 스키마 검증
  2~3. 마지막 성공 sourceCommit과 비교, 동일하면 스킵(--force로 무시 가능)
  4~5. artifact를 임시 디렉터리에 다운로드 → 크기·sha256 검증
  6. 안전 tar 해제 (절대경로/'..'/symlink/hardlink/특수파일/루트이탈 거부, 크기 상한)
  7~8. catalog.json/manifest.json의 version==sourceCommit 검증, manifest 파일 해시 검증
  9. catalog가 가리키는 markdown 문서를 docs_extractor.build_document()로 파싱(파싱 로직 재사용)
  10. id+contentHash로 신규/변경/삭제 판별 (platform_docs_store 스냅샷 대조)
  11. 신규/변경 문서만 load_data_to_chromadb()로 in-process 적재
  12. 사라진 문서는 Chroma(where=original_doc_id) + FTS에서 삭제
  13. 모든 처리가 성공한 뒤에만 상태(source_commit + 문서 스냅샷) 갱신
  14. 임시 파일은 성공/실패 무관하게 정리 (try/finally)
"""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from company_llm_rag import platform_docs_store
from company_llm_rag.config import settings
from company_llm_rag.data_extraction.docs_repo.docs_extractor import build_document
from company_llm_rag.data_extraction.docs_repo.s3_client import (
    Boto3S3Client,
    S3AccessDeniedError,
    S3Client,
    S3ClientError,
    S3ObjectNotFoundError,
)
from company_llm_rag.data_extraction.docs_repo.s3_release import (
    ReleaseValidationError,
    TarSecurityError,
    download_artifact,
    safe_extract_tar,
    validate_current_json,
    verify_catalog_manifest_version,
    verify_manifest_files,
)
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _load_json_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _parse_catalog_documents(catalog: dict) -> List[dict]:
    documents = catalog.get("documents")
    if not isinstance(documents, list):
        raise ReleaseValidationError("catalog.json에 documents 배열이 없음")
    return documents


def _delete_docs(doc_ids: List[str]) -> int:
    """새 catalog에서 사라진 문서를 Chroma + FTS에서 삭제합니다. 삭제된 청크 수를 반환합니다."""
    from company_llm_rag.database import db_manager
    from company_llm_rag.fts_store import fts_delete

    collection = db_manager.get_collection()
    total_chunks = 0
    for doc_id in doc_ids:
        existing = collection.get(where={"original_doc_id": doc_id}, include=[])
        chunk_ids = existing.get("ids") or []
        if not chunk_ids:
            continue
        collection.delete(where={"original_doc_id": doc_id})
        fts_delete(chunk_ids)
        total_chunks += len(chunk_ids)
    logger.info(f"[DocsS3] 삭제 반영: 문서 {len(doc_ids)}개 | 청크 {total_chunks}개")
    return total_chunks


def run(client: Optional[S3Client] = None, *, force: bool = False) -> int:
    """S3 수집을 1회 실행합니다.

    Returns:
        0: 성공(또는 스킵), 1: 실패
    """
    bucket = settings.PLATFORM_DOCS_S3_BUCKET
    if not bucket:
        logger.info("[DocsS3] PLATFORM_DOCS_S3_BUCKET 미설정 — S3 수집 건너뜀")
        return 0

    client = client or Boto3S3Client(region=settings.AWS_REGION)
    tmp_dir: Optional[Path] = None

    try:
        # 1. current.json 조회 → 스키마 검증
        try:
            raw = client.get_object(bucket, settings.PLATFORM_DOCS_S3_CURRENT_KEY)
        except S3ObjectNotFoundError as e:
            logger.error(f"[DocsS3] current.json 조회 실패(없음) — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except S3AccessDeniedError as e:
            logger.error(f"[DocsS3] current.json 접근 거부 — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except S3ClientError as e:
            logger.error(f"[DocsS3] current.json 조회 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        try:
            current = json.loads(raw)
            info = validate_current_json(current)
        except (json.JSONDecodeError, ReleaseValidationError) as e:
            logger.error(f"[DocsS3] current.json 검증 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        source_commit = info["source_commit"]
        last_commit = platform_docs_store.get_last_source_commit()
        logger.info(
            f"[DocsS3] 조회 sourceCommit={source_commit} 이전 성공 sourceCommit={last_commit!r} "
            f"documentCount(신고값)={info['document_count']}"
        )

        # 2~3. 동일 버전 스킵
        if not force and last_commit is not None and last_commit == source_commit:
            logger.info("[DocsS3] 이전 성공 버전과 동일한 sourceCommit — 다운로드/재색인 생략 (--force로 무시 가능)")
            return 0

        # 4. 임시 작업 디렉터리
        tmp_dir = Path(tempfile.mkdtemp(prefix="docs_s3_"))
        archive_path = tmp_dir / "platform.tar.gz"
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        # 4~5. artifact 다운로드 → 크기·sha256 검증
        try:
            download_artifact(
                client, bucket, info["artifact_key"],
                expected_size=info["artifact_size"], expected_sha256=info["artifact_sha256"],
                dest_path=archive_path,
            )
        except S3ObjectNotFoundError as e:
            logger.error(f"[DocsS3] artifact 조회 실패(없음) — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except S3AccessDeniedError as e:
            logger.error(f"[DocsS3] artifact 접근 거부 — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except S3ClientError as e:
            logger.error(f"[DocsS3] artifact 다운로드 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except ReleaseValidationError as e:
            logger.error(f"[DocsS3] artifact 무결성 검증 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        # 6. 안전 tar 해제
        try:
            safe_extract_tar(
                archive_path, extract_dir,
                max_files=settings.PLATFORM_DOCS_MAX_FILES,
                max_file_mb=settings.PLATFORM_DOCS_MAX_FILE_MB,
                max_total_mb=settings.PLATFORM_DOCS_MAX_TOTAL_MB,
            )
        except TarSecurityError as e:
            logger.error(f"[DocsS3] tar 보안 검증 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1
        except Exception as e:
            logger.error(f"[DocsS3] tar 해제 실패(손상된 아카이브 등) — 수집 중단, 기존 색인 유지: {e}", exc_info=True)
            return 1

        # 7. catalog.json / manifest.json 로드 + version 검증
        try:
            catalog = _load_json_file(extract_dir / "catalog.json")
            manifest = _load_json_file(extract_dir / "manifest.json")
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[DocsS3] catalog.json/manifest.json 로드 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        try:
            verify_catalog_manifest_version(catalog, manifest, source_commit)
            documents = _parse_catalog_documents(catalog)
        except ReleaseValidationError as e:
            logger.error(f"[DocsS3] catalog/manifest 검증 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        declared_count = info["document_count"]
        if declared_count is not None and declared_count != len(documents):
            logger.warning(
                f"[DocsS3] documentCount(신고값 {declared_count}) != catalog 실제 문서 수({len(documents)})"
            )

        # 8. manifest 파일 검증 (catalog markdownPath 필수)
        required_paths = {d["markdownPath"] for d in documents if d.get("markdownPath")}
        try:
            verify_manifest_files(extract_dir, manifest, required_paths)
        except ReleaseValidationError as e:
            logger.error(f"[DocsS3] manifest 파일 검증 실패 — 수집 중단, 기존 색인 유지: {e}")
            return 1

        # 9~10. 문서 파싱 + 신규/변경/삭제 판별 (id+contentHash)
        # 모든 문서를 파싱한다 (파싱은 저렴, 임베딩만 비쌈): 변경분만 색인하되,
        # 그래프·digest_guids 마무리는 "전체 카탈로그" 기준이어야 하기 때문 (아래 13단계 참고).
        prev_snapshot = platform_docs_store.get_doc_snapshot()
        new_snapshot: Dict[str, Tuple[str, str]] = {}
        jsonl_lines: List[str] = []
        digest_graph_docs_full: List[dict] = []  # 전체 다이제스트 doc 노드 (data_loader 수집 형식과 동일)
        digest_guids_full: set = set()           # 전체 다이제스트 sp_guid 스냅샷
        new_count = changed_count = unchanged_count = skip_count = parse_error_count = 0
        scope_skip_count = 0

        for entry in documents:
            catalog_id = entry.get("id")
            path = entry.get("path")
            markdown_path = entry.get("markdownPath")
            title = entry.get("title", "")
            if not catalog_id or not markdown_path:
                logger.warning(f"[DocsS3] catalog 항목 필드 누락(id/markdownPath) — 건너뜀: {entry}")
                parse_error_count += 1
                continue

            md_file = extract_dir / markdown_path
            try:
                content_bytes = md_file.read_bytes()
                content = content_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"[DocsS3] 문서 읽기 실패 — 건너뜀: {markdown_path} ({e})")
                parse_error_count += 1
                continue

            # contentHash는 카탈로그 제공 여부에 의존하지 않고 markdown 바이트로 직접 계산 (설계 §2)
            content_hash = hashlib.sha256(content_bytes).hexdigest()

            # 실 계약: catalog.path는 platform/ 이하 상대 경로 (예: 'sharepoint-index/digests/x.md').
            # 저장소 루트 기준으로 정규화해 로컬 체크아웃 수집의 doc_id(`docs-platform/...`)와 일치시킨다.
            relpath = path or markdown_path
            if not relpath.startswith("platform/"):
                relpath = f"platform/{relpath}"

            # 수집 범위: 로컬 체크아웃 수집과 동일하게 DOCS_REPO_SUBDIRS 이내 + README 제외
            # (catalog에는 README·sharepoint-index 루트 색인 문서 등 범위 밖 항목도 포함됨)
            in_scope = any(
                relpath.startswith(sub.rstrip("/") + "/") for sub in settings.DOCS_REPO_SUBDIRS
            )
            if not in_scope or Path(relpath).name.lower() == "readme.md":
                scope_skip_count += 1
                continue

            doc_id = f"docs-{relpath}"

            doc = build_document(
                relpath=relpath, content=content, updated_at="",
                branch="", commit=source_commit, doc_url="",
            )
            if doc is None:
                # 내용 부족 문서는 스냅샷에도 넣지 않는다 — 이전에 색인된 문서가
                # 임계치 미만으로 줄어든 경우 '삭제'로 판별되어 색인에서 제거된다.
                logger.debug(f"[DocsS3] 내용 부족 스킵: {markdown_path}")
                skip_count += 1
                continue
            if title and not doc.get("title"):
                doc["title"] = title
            new_snapshot[catalog_id] = (doc_id, content_hash)

            meta = doc.get("metadata", {})
            if meta.get("docs_category") == "digest":
                digest_graph_docs_full.append({
                    "id": doc["id"], "title": doc.get("title", ""), "url": doc.get("url", ""),
                    "updated_at": doc.get("updated_at", ""),
                    "digest_date": meta.get("digest_date", ""),
                    "digest_kind": meta.get("digest_kind", ""),
                    "not_implemented": bool(meta.get("not_implemented", False)),
                    "digest_topics": meta.get("digest_topics", ""),
                    "digest_issues": meta.get("digest_issues", "[]"),
                })
                if meta.get("sp_guid"):
                    digest_guids_full.add(meta["sp_guid"])

            prev = prev_snapshot.get(catalog_id)
            # --force는 "전체 재수집"이므로 콘텐츠 해시가 같아도 diff 스킵을 적용하지 않는다.
            if not force and prev is not None and prev == (doc_id, content_hash):
                unchanged_count += 1
                continue

            jsonl_lines.append(json.dumps(doc, ensure_ascii=False))
            if prev is None:
                new_count += 1
            else:
                changed_count += 1

        # 삭제 판별: 이전 스냅샷에는 있었지만 새 catalog에는 없는 catalog_id
        deleted_doc_ids = [
            doc_id for cid, (doc_id, _hash) in prev_snapshot.items() if cid not in new_snapshot
        ]

        logger.info(
            f"[DocsS3] 판별 결과: 신규 {new_count} | 변경 {changed_count} | 동일(스킵) {unchanged_count} | "
            f"삭제 {len(deleted_doc_ids)} | 범위외 {scope_skip_count} | 내용부족스킵 {skip_count} | "
            f"파싱실패 {parse_error_count}"
        )

        # 11~12. 신규/변경 문서 색인 + 삭제 반영
        # finalize_graph=False: data_loader의 그래프 재구축·digest_guids 교체는
        # "스트림 = 해당 소스 전체" 가정의 교체 방식이라, 변경분만 흘리는 증분 적재에서
        # 그대로 두면 스냅샷이 변경분만으로 truncate된다. 아래에서 전체 기준으로 직접 수행.
        try:
            if jsonl_lines:
                from company_llm_rag.data_loader import load_data_to_chromadb
                load_data_to_chromadb(iter(jsonl_lines), finalize_graph=False)
            if deleted_doc_ids:
                _delete_docs(deleted_doc_ids)
        except Exception as e:
            logger.error(f"[DocsS3] 색인/삭제 단계 실패 — 수집 중단, 마지막 성공 버전·상태 유지: {e}", exc_info=True)
            return 1

        # 12.5. 그래프·digest_guids 마무리 — 전체 카탈로그 기준 (실패해도 색인에는 영향 없음)
        if jsonl_lines or deleted_doc_ids:
            try:
                from company_llm_rag.graph import entity_link
                entity_link.rebuild_entities(None, digest_graph_docs_full or None)
            except Exception as e:
                logger.error(f"[DocsS3] 그래프 재구축 실패 (색인은 정상): {e}", exc_info=True)
            try:
                from company_llm_rag.digest_store import replace_guids
                replace_guids(digest_guids_full)
            except Exception as e:
                logger.error(f"[DocsS3] digest_guids 스냅샷 갱신 실패 (색인은 정상): {e}", exc_info=True)

        # 13. 모든 처리가 성공한 뒤에만 상태 갱신
        platform_docs_store.commit_snapshot(source_commit, new_snapshot)
        logger.info(f"[DocsS3] 완료 — sourceCommit={source_commit}")
        return 0

    finally:
        # 14. 임시 파일 정리 (성공/실패 무관)
        if tmp_dir is not None and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="플랫폼 문서 S3 수집 (#62)")
    parser.add_argument(
        "--force", action="store_true",
        help="이전 성공과 동일한 sourceCommit이어도 강제로 재수집",
    )
    args = parser.parse_args()

    start_time = time.time()
    try:
        exit_code = run(force=args.force)
    except Exception as e:
        logger.error(f"[DocsS3] 예기치 못한 오류로 수집 실패: {e}", exc_info=True)
        exit_code = 1

    logger.info(f"[DocsS3] 종료(exit={exit_code}) | 소요: {time.time() - start_time:.1f}s")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

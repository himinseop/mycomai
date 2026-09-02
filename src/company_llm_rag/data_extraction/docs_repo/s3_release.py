"""
S3 릴리즈 아티팩트 검증·해제 (#62)

current.json 스키마 검증, artifact 다운로드·무결성 검증, 안전 tar 해제,
catalog.json/manifest.json 버전·파일 해시 검증을 담당합니다.

계약 상세는 docs/issues/62/design.md §2, §4 참고.
"""

import hashlib
import re
import tarfile
from pathlib import Path
from typing import Iterable, Set

from company_llm_rag.data_extraction.docs_repo.s3_client import S3Client
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_SHA1_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseValidationError(Exception):
    """current.json/artifact/manifest/catalog 검증 실패."""


class TarSecurityError(Exception):
    """tar 아카이브에 안전하지 않은 entry 또는 압축 폭탄 의심 항목이 있음."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_current_json(data: dict) -> dict:
    """current.json(schemaVersion 1)을 검증하고 정규화된 필드를 반환합니다.

    검증 규칙(설계 §2) 중 하나라도 실패하면 ReleaseValidationError를 발생시킵니다.
    documentCount 등 알 수 없는 추가 필드는 허용(무시)합니다.
    """
    if not isinstance(data, dict):
        raise ReleaseValidationError("current.json이 JSON object가 아님")

    if data.get("schemaVersion") != 1:
        raise ReleaseValidationError(f"schemaVersion 불일치: {data.get('schemaVersion')!r} (기대값 1)")

    source_commit = data.get("sourceCommit")
    if not isinstance(source_commit, str) or not _SHA1_HEX_RE.match(source_commit):
        raise ReleaseValidationError(f"sourceCommit 형식 오류(40자리 소문자 SHA 필요): {source_commit!r}")

    artifact = data.get("artifact")
    if not isinstance(artifact, dict):
        raise ReleaseValidationError("artifact 필드가 없거나 object가 아님")

    expected_key = f"docs/releases/{source_commit}/platform.tar.gz"
    key = artifact.get("key")
    if key != expected_key:
        raise ReleaseValidationError(f"artifact.key 불일치: {key!r} (기대값 {expected_key!r})")

    sha256 = artifact.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_HEX_RE.match(sha256):
        raise ReleaseValidationError(f"artifact.sha256 형식 오류(64자리 hex 필요): {sha256!r}")

    size = artifact.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ReleaseValidationError(f"artifact.size 오류(0 이상의 정수 필요): {size!r}")

    return {
        "source_commit": source_commit,
        "build_number": data.get("buildNumber"),
        "document_count": data.get("documentCount"),
        "artifact_key": key,
        "artifact_sha256": sha256,
        "artifact_size": size,
    }


def download_artifact(
    client: S3Client, bucket: str, key: str, *,
    expected_size: int, expected_sha256: str, dest_path: Path,
) -> None:
    """artifact를 다운로드해 dest_path에 저장하고, 크기·SHA-256을 current.json 신고값과 대조합니다.

    불일치 시 ReleaseValidationError를 발생시키며, 이 경우 dest_path는 쓰지 않습니다.
    S3Client가 던지는 S3ClientError(및 서브클래스)는 그대로 전파됩니다.
    """
    data = client.get_object(bucket, key)

    if len(data) != expected_size:
        raise ReleaseValidationError(
            f"artifact 크기 불일치: 실제 {len(data)} != 신고 {expected_size}"
        )

    actual_sha256 = sha256_bytes(data)
    if actual_sha256 != expected_sha256:
        raise ReleaseValidationError(
            f"artifact sha256 불일치: 실제 {actual_sha256} != 신고 {expected_sha256}"
        )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)


def safe_extract_tar(
    tar_path: Path, dest_dir: Path, *,
    max_files: int, max_file_mb: float, max_total_mb: float,
) -> None:
    """tar.gz를 안전하게 dest_dir에 해제합니다 (설계 §4).

    해제 전 모든 entry를 먼저 검사한 뒤(fail-fast, 부분 해제 방지), 통과한 경우에만
    실제로 파일을 씁니다. 다음은 거부하고 TarSecurityError를 발생시킵니다:
    - 절대 경로, '..' 경로 이동(정규화 후 루트 이탈 포함)
    - symlink, hardlink
    - device/FIFO 등 특수 파일 (regular file/directory만 허용)
    - 파일 개수·단일 파일 크기·전체 해제 크기 상한 초과
    """
    dest_dir = dest_dir.resolve()
    max_file_bytes = int(max_file_mb * 1024 * 1024)
    max_total_bytes = int(max_total_mb * 1024 * 1024)

    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()

        file_count = 0
        total_size = 0
        for member in members:
            name = member.name
            if name.startswith("/") or name.startswith("\\"):
                raise TarSecurityError(f"절대 경로 거부: {name}")
            if Path(name).is_absolute():
                raise TarSecurityError(f"절대 경로 거부: {name}")
            if ".." in Path(name).parts:
                raise TarSecurityError(f"상위 경로 이동('..') 거부: {name}")

            target = (dest_dir / name).resolve()
            try:
                target.relative_to(dest_dir)
            except ValueError:
                raise TarSecurityError(f"루트 이탈 거부: {name}")

            if member.issym() or member.islnk():
                raise TarSecurityError(f"symlink/hardlink 거부: {name}")
            if not (member.isreg() or member.isdir()):
                raise TarSecurityError(f"특수 파일 거부: {name} (type={member.type!r})")

            if member.isreg():
                file_count += 1
                if file_count > max_files:
                    raise TarSecurityError(f"파일 개수 상한 초과: > {max_files}")
                if member.size > max_file_bytes:
                    raise TarSecurityError(
                        f"단일 파일 크기 상한 초과: {name} ({member.size} bytes > {max_file_bytes} bytes)"
                    )
                total_size += member.size
                if total_size > max_total_bytes:
                    raise TarSecurityError(f"전체 해제 크기 상한 초과: {total_size} bytes > {max_total_bytes} bytes")

        # 검증을 모두 통과한 뒤에만 실제로 해제합니다.
        for member in members:
            target = dest_dir / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise TarSecurityError(f"regular file을 열 수 없음: {member.name}")
            with src, target.open("wb") as out:
                out.write(src.read())


def verify_catalog_manifest_version(catalog: dict, manifest: dict, source_commit: str) -> None:
    """catalog.json / manifest.json의 version 필드가 sourceCommit과 같은지 검증합니다."""
    catalog_version = catalog.get("version")
    if catalog_version != source_commit:
        raise ReleaseValidationError(
            f"catalog.json version 불일치: {catalog_version!r} != sourceCommit {source_commit!r}"
        )
    manifest_version = manifest.get("version")
    if manifest_version != source_commit:
        raise ReleaseValidationError(
            f"manifest.json version 불일치: {manifest_version!r} != sourceCommit {source_commit!r}"
        )


def verify_manifest_files(extract_dir: Path, manifest: dict, required_paths: Iterable[str]) -> None:
    """manifest에 기록된 파일 size·sha256을 실제 해제된 파일과 대조합니다.

    catalog가 참조하는 markdown 파일(required_paths)은 manifest에 반드시 있어야 하며,
    실제 파일이 존재하고 size·sha256이 일치해야 합니다. 하나라도 실패하면 중단합니다.
    """
    files = manifest.get("files")
    # 실 계약(Docs CI): files는 [{path, sha256, size}] 리스트 — dict 형식도 함께 허용
    if isinstance(files, list):
        try:
            files = {f["path"]: f for f in files}
        except (TypeError, KeyError):
            raise ReleaseValidationError("manifest.json files 리스트 항목 형식 오류(path 필수)")
    if not isinstance(files, dict):
        raise ReleaseValidationError("manifest.json에 files 매핑이 없음")

    required: Set[str] = set(required_paths)
    missing = required - set(files.keys())
    if missing:
        raise ReleaseValidationError(f"manifest에 없는 필수 파일: {sorted(missing)}")

    for relpath in sorted(required):
        info = files[relpath]
        if not isinstance(info, dict):
            raise ReleaseValidationError(f"manifest 파일 항목 형식 오류: {relpath}")

        full = extract_dir / relpath
        if not full.is_file():
            raise ReleaseValidationError(f"manifest 파일 누락(해제본 없음): {relpath}")

        expected_size = info.get("size")
        actual_size = full.stat().st_size
        if expected_size is not None and actual_size != expected_size:
            raise ReleaseValidationError(
                f"파일 크기 불일치: {relpath} (실제 {actual_size} != manifest {expected_size})"
            )

        expected_sha256 = info.get("sha256")
        if expected_sha256:
            actual_sha256 = sha256_file(full)
            if actual_sha256 != expected_sha256:
                raise ReleaseValidationError(f"파일 해시 불일치: {relpath}")

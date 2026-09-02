"""
플랫폼 문서 S3 수집 테스트 (#62)

docs/issues/62/design.md §7 TC S1~S11. FakeS3Client와 자체 tarfile 픽스처만 사용하며
AWS 연결은 필요 없습니다. ChromaDB 컬렉션·JSONL 적재(load_data_to_chromadb)는
fake/mock으로 대체합니다.

AWS 연결이 실제로 필요한 통합 테스트(실 버킷 접근)는 이 파일에 포함하지 않습니다.
필요해지면 `@pytest.mark.integration`으로 분리하고 기본 실행(`pytest -m "not integration"`)에서
제외하는 컨벤션을 그대로 따르면 됩니다.
"""

import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest

from company_llm_rag.config import settings
from company_llm_rag.data_extraction.docs_repo import s3_docs_ingest
from company_llm_rag.data_extraction.docs_repo.s3_client import (
    FakeS3Client,
    S3AccessDeniedError,
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

SOURCE_COMMIT_A = "a" * 40
SOURCE_COMMIT_B = "b" * 40


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _valid_current_json(source_commit=SOURCE_COMMIT_A, sha256="c" * 64, size=100, document_count=1):
    return {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "buildNumber": 1,
        "documentCount": document_count,
        "artifact": {
            "key": f"docs/releases/{source_commit}/platform.tar.gz",
            "sha256": sha256,
            "size": size,
        },
    }


def _tar_bytes_with_members(members_and_data):
    """[(TarInfo, bytes_or_None), ...] 로 tar.gz 바이트를 만듭니다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members_and_data:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def _make_markdown(title, body="플랫폼 문서 본문입니다. " * 5):
    return f"# {title}\n\n{body}"


def _build_release(docs):
    """docs: {catalog_id: {"path": str, "title": str, "markdown": str}}

    표준 계약(catalog.json/manifest.json/markdown/**)에 맞는 tar.gz 바이트를 만들고,
    (tar_bytes, catalog_documents) 를 반환합니다. source_commit은 호출부에서 채워 current.json을
    만들도록 별도로 넘깁니다.
    """
    manifest_files = []  # 실 계약(Docs CI): [{path, sha256, size}] 리스트 형식
    catalog_documents = []
    entries = []
    for cid, meta in docs.items():
        md_bytes = meta["markdown"].encode("utf-8")
        md_relpath = f"markdown/{meta['path']}"
        info = tarfile.TarInfo(name=md_relpath)
        entries.append((info, md_bytes))
        manifest_files.append({
            "path": md_relpath,
            "size": len(md_bytes),
            "sha256": hashlib.sha256(md_bytes).hexdigest(),
        })
        catalog_documents.append({
            "id": cid,
            "path": meta["path"],
            "markdownPath": md_relpath,
            "htmlPath": f"html/{meta['path']}".replace(".md", ".html"),
            "title": meta.get("title", ""),
        })
    return catalog_documents, manifest_files, entries


def _package(source_commit, docs):
    """docs 딕셔너리로부터 (tar_bytes, current_json) 완성본을 만듭니다."""
    catalog_documents, manifest_files, entries = _build_release(docs)
    catalog = {"version": source_commit, "documents": catalog_documents}
    manifest = {"version": source_commit, "files": manifest_files}
    for name, obj in (("catalog.json", catalog), ("manifest.json", manifest)):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        info = tarfile.TarInfo(name=name)
        entries.append((info, data))
    tar_bytes = _tar_bytes_with_members(entries)
    current = {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "buildNumber": 1,
        "documentCount": len(docs),
        "artifact": {
            "key": f"docs/releases/{source_commit}/platform.tar.gz",
            "sha256": hashlib.sha256(tar_bytes).hexdigest(),
            "size": len(tar_bytes),
        },
    }
    return tar_bytes, current


def _client_for(bucket, tar_bytes, current):
    client = FakeS3Client()
    client.put_object(bucket, settings.PLATFORM_DOCS_S3_CURRENT_KEY, json.dumps(current).encode("utf-8"))
    client.put_object(bucket, current["artifact"]["key"], tar_bytes)
    return client


class FakeCollection:
    """_delete_docs가 쓰는 최소한의 ChromaDB 컬렉션 인터페이스만 구현."""

    def __init__(self):
        self.docs = {}  # chunk_id -> {"metadata": {...}}

    def add_chunk(self, chunk_id, original_doc_id):
        self.docs[chunk_id] = {"metadata": {"original_doc_id": original_doc_id}}

    def get(self, ids=None, where=None, include=None):
        if where and "original_doc_id" in where:
            target = where["original_doc_id"]
            matched = [cid for cid, d in self.docs.items() if d["metadata"].get("original_doc_id") == target]
            return {"ids": matched}
        return {"ids": list(self.docs.keys())}

    def delete(self, where=None):
        if where and "original_doc_id" in where:
            target = where["original_doc_id"]
            for cid in [c for c, d in self.docs.items() if d["metadata"].get("original_doc_id") == target]:
                del self.docs[cid]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def s3_env(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_DOCS_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "PLATFORM_DOCS_S3_CURRENT_KEY", "docs/current.json")
    monkeypatch.setattr(settings, "PLATFORM_DOCS_MAX_FILES", 20000)
    monkeypatch.setattr(settings, "PLATFORM_DOCS_MAX_FILE_MB", 20)
    monkeypatch.setattr(settings, "PLATFORM_DOCS_MAX_TOTAL_MB", 1024)
    return settings


@pytest.fixture
def platform_docs_db(tmp_path, monkeypatch):
    from company_llm_rag import platform_docs_store
    db_path = tmp_path / "app_data.db"
    monkeypatch.setattr(platform_docs_store, "_DB_PATH", db_path)
    if hasattr(platform_docs_store._local, "con"):
        delattr(platform_docs_store._local, "con")
    yield platform_docs_store
    if hasattr(platform_docs_store._local, "con"):
        delattr(platform_docs_store._local, "con")


@pytest.fixture
def fake_loader(monkeypatch):
    """load_data_to_chromadb 호출을 가로채 어떤 문서가 넘어왔는지 기록합니다."""
    calls = []

    def _fake(lines_iter, **kwargs):
        docs = [json.loads(line) for line in lines_iter]
        calls.append(docs)
        # 증분 적재는 loader의 전체-교체형 마무리를 꺼야 한다 (#62 검증에서 발견)
        assert kwargs.get("finalize_graph") is False

    monkeypatch.setattr("company_llm_rag.data_loader.load_data_to_chromadb", _fake)
    return calls


@pytest.fixture
def fake_finalize(monkeypatch):
    """오케스트레이터의 그래프·digest_guids 마무리 호출을 가로채 기록합니다 (실 DB 접근 차단)."""
    recorded = {"rebuild": [], "guids": []}

    def _fake_rebuild(conf_docs, digest_docs):
        recorded["rebuild"].append((conf_docs, digest_docs))
        return {}

    monkeypatch.setattr("company_llm_rag.graph.entity_link.rebuild_entities", _fake_rebuild)
    monkeypatch.setattr("company_llm_rag.digest_store.replace_guids", lambda guids: recorded["guids"].append(set(guids)))
    return recorded


@pytest.fixture
def fake_chroma(monkeypatch):
    collection = FakeCollection()

    class _FakeDBManager:
        def get_collection(self):
            return collection

    monkeypatch.setattr("company_llm_rag.database.db_manager", _FakeDBManager())

    deleted_chunk_ids = []
    monkeypatch.setattr("company_llm_rag.fts_store.fts_delete", deleted_chunk_ids.extend)

    return collection, deleted_chunk_ids


# ---------------------------------------------------------------------------
# S1 — current.json 스키마 검증
# ---------------------------------------------------------------------------

class TestCurrentJsonValidation:
    def test_valid(self):
        info = validate_current_json(_valid_current_json())
        assert info["source_commit"] == SOURCE_COMMIT_A
        assert info["artifact_size"] == 100

    def test_schema_version_mismatch(self):
        data = _valid_current_json()
        data["schemaVersion"] = 2
        with pytest.raises(ReleaseValidationError, match="schemaVersion"):
            validate_current_json(data)

    def test_source_commit_not_hex40(self):
        data = _valid_current_json()
        data["sourceCommit"] = "not-a-sha"
        with pytest.raises(ReleaseValidationError, match="sourceCommit"):
            validate_current_json(data)

    def test_source_commit_uppercase_rejected(self):
        data = _valid_current_json()
        data["sourceCommit"] = "A" * 40
        with pytest.raises(ReleaseValidationError, match="sourceCommit"):
            validate_current_json(data)

    def test_artifact_key_mismatch(self):
        data = _valid_current_json()
        data["artifact"]["key"] = "docs/releases/wrong/platform.tar.gz"
        with pytest.raises(ReleaseValidationError, match="artifact.key"):
            validate_current_json(data)

    def test_artifact_sha256_bad_format(self):
        data = _valid_current_json()
        data["artifact"]["sha256"] = "xyz"
        with pytest.raises(ReleaseValidationError, match="sha256"):
            validate_current_json(data)

    def test_artifact_size_negative(self):
        data = _valid_current_json()
        data["artifact"]["size"] = -1
        with pytest.raises(ReleaseValidationError, match="size"):
            validate_current_json(data)

    def test_unknown_extra_fields_allowed(self):
        data = _valid_current_json()
        data["someFutureField"] = "ignored"
        info = validate_current_json(data)
        assert info["source_commit"] == SOURCE_COMMIT_A


# ---------------------------------------------------------------------------
# S2 — artifact 다운로드 무결성 검증
# ---------------------------------------------------------------------------

class TestDownloadArtifact:
    def test_success(self, tmp_path):
        data = b"hello platform docs artifact"
        client = FakeS3Client()
        client.put_object("bucket", "key", data)
        dest = tmp_path / "out.tar.gz"
        download_artifact(
            client, "bucket", "key",
            expected_size=len(data), expected_sha256=hashlib.sha256(data).hexdigest(),
            dest_path=dest,
        )
        assert dest.read_bytes() == data

    def test_size_mismatch_aborts_and_no_state_change(self, tmp_path):
        data = b"hello"
        client = FakeS3Client()
        client.put_object("bucket", "key", data)
        dest = tmp_path / "out.tar.gz"
        with pytest.raises(ReleaseValidationError, match="크기 불일치"):
            download_artifact(
                client, "bucket", "key",
                expected_size=999, expected_sha256=hashlib.sha256(data).hexdigest(),
                dest_path=dest,
            )
        assert not dest.exists()

    def test_sha256_mismatch_aborts(self, tmp_path):
        data = b"hello"
        client = FakeS3Client()
        client.put_object("bucket", "key", data)
        dest = tmp_path / "out.tar.gz"
        with pytest.raises(ReleaseValidationError, match="sha256 불일치"):
            download_artifact(
                client, "bucket", "key",
                expected_size=len(data), expected_sha256="0" * 64,
                dest_path=dest,
            )
        assert not dest.exists()


# ---------------------------------------------------------------------------
# S3 — manifest 파일 누락·해시 불일치
# ---------------------------------------------------------------------------

class TestVerifyManifestFiles:
    def test_missing_required_entry(self, tmp_path):
        manifest = {"version": "x", "files": {}}
        with pytest.raises(ReleaseValidationError, match="필수 파일"):
            verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])

    def test_missing_extracted_file(self, tmp_path):
        manifest = {"version": "x", "files": {"markdown/a.md": {"size": 5, "sha256": "0" * 64}}}
        with pytest.raises(ReleaseValidationError, match="해제본 없음"):
            verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])

    def test_size_mismatch(self, tmp_path):
        md_dir = tmp_path / "markdown"
        md_dir.mkdir()
        f = md_dir / "a.md"
        f.write_bytes(b"hello world")
        manifest = {
            "version": "x",
            "files": {"markdown/a.md": {"size": 999, "sha256": hashlib.sha256(b"hello world").hexdigest()}},
        }
        with pytest.raises(ReleaseValidationError, match="크기 불일치"):
            verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])

    def test_hash_mismatch(self, tmp_path):
        md_dir = tmp_path / "markdown"
        md_dir.mkdir()
        f = md_dir / "a.md"
        f.write_bytes(b"hello world")
        manifest = {
            "version": "x",
            "files": {"markdown/a.md": {"size": len(b"hello world"), "sha256": "0" * 64}},
        }
        with pytest.raises(ReleaseValidationError, match="해시 불일치"):
            verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])

    def test_valid_passes(self, tmp_path):
        md_dir = tmp_path / "markdown"
        md_dir.mkdir()
        f = md_dir / "a.md"
        f.write_bytes(b"hello world")
        manifest = {
            "version": "x",
            "files": {"markdown/a.md": {"size": len(b"hello world"), "sha256": hashlib.sha256(b"hello world").hexdigest()}},
        }
        verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])  # 예외 없이 통과

    def test_list_format_passes(self, tmp_path):
        """실 계약(Docs CI) 형식: files가 [{path, sha256, size}] 리스트."""
        md_dir = tmp_path / "markdown"
        md_dir.mkdir()
        (md_dir / "a.md").write_bytes(b"hello world")
        manifest = {
            "version": "x",
            "files": [{"path": "markdown/a.md", "size": len(b"hello world"),
                       "sha256": hashlib.sha256(b"hello world").hexdigest()}],
        }
        verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])  # 예외 없이 통과

    def test_list_format_bad_entry(self, tmp_path):
        manifest = {"version": "x", "files": [{"size": 1}]}  # path 누락
        with pytest.raises(ReleaseValidationError, match="리스트 항목 형식"):
            verify_manifest_files(tmp_path, manifest, required_paths=["markdown/a.md"])


# ---------------------------------------------------------------------------
# S11 — catalog/manifest version != sourceCommit
# ---------------------------------------------------------------------------

class TestVerifyCatalogManifestVersion:
    def test_catalog_version_mismatch(self):
        with pytest.raises(ReleaseValidationError, match="catalog.json version"):
            verify_catalog_manifest_version({"version": "aaa"}, {"version": SOURCE_COMMIT_A}, SOURCE_COMMIT_A)

    def test_manifest_version_mismatch(self):
        with pytest.raises(ReleaseValidationError, match="manifest.json version"):
            verify_catalog_manifest_version({"version": SOURCE_COMMIT_A}, {"version": "bbb"}, SOURCE_COMMIT_A)

    def test_both_match(self):
        verify_catalog_manifest_version({"version": SOURCE_COMMIT_A}, {"version": SOURCE_COMMIT_A}, SOURCE_COMMIT_A)


# ---------------------------------------------------------------------------
# S4 — 위험한 tar entry 차단
# ---------------------------------------------------------------------------

class TestSafeExtractTarSecurity:
    def _extract(self, tmp_path, members_and_data):
        tar_path = tmp_path / "archive.tar.gz"
        tar_path.write_bytes(_tar_bytes_with_members(members_and_data))
        dest = tmp_path / "extracted"
        dest.mkdir()
        safe_extract_tar(tar_path, dest, max_files=1000, max_file_mb=20, max_total_mb=1024)

    def test_absolute_path_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="/etc/passwd")
        with pytest.raises(TarSecurityError, match="절대 경로"):
            self._extract(tmp_path, [(info, b"evil")])

    def test_parent_traversal_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="../../evil.txt")
        with pytest.raises(TarSecurityError, match="상위 경로"):
            self._extract(tmp_path, [(info, b"evil")])

    def test_nested_parent_traversal_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="subdir/../../evil.txt")
        with pytest.raises(TarSecurityError, match="상위 경로"):
            self._extract(tmp_path, [(info, b"evil")])

    def test_symlink_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        with pytest.raises(TarSecurityError, match="symlink"):
            self._extract(tmp_path, [(info, None)])

    def test_hardlink_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="hard.txt")
        info.type = tarfile.LNKTYPE
        info.linkname = "somewhere"
        with pytest.raises(TarSecurityError, match="hardlink"):
            self._extract(tmp_path, [(info, None)])

    def test_device_file_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="dev0")
        info.type = tarfile.CHRTYPE
        with pytest.raises(TarSecurityError, match="특수 파일"):
            self._extract(tmp_path, [(info, None)])

    def test_fifo_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="fifo0")
        info.type = tarfile.FIFOTYPE
        with pytest.raises(TarSecurityError, match="특수 파일"):
            self._extract(tmp_path, [(info, None)])

    def test_valid_regular_files_and_dirs_extract_correctly(self, tmp_path):
        dir_info = tarfile.TarInfo(name="sub")
        dir_info.type = tarfile.DIRTYPE
        file_info = tarfile.TarInfo(name="sub/hello.txt")
        self._extract(tmp_path, [(dir_info, None), (file_info, b"hello there")])
        out = tmp_path / "extracted" / "sub" / "hello.txt"
        assert out.read_bytes() == b"hello there"


# ---------------------------------------------------------------------------
# S5 — 압축 폭탄 상한
# ---------------------------------------------------------------------------

class TestSafeExtractTarBombLimits:
    def test_file_count_exceeded(self, tmp_path):
        tar_path = tmp_path / "archive.tar.gz"
        members = []
        for i in range(3):
            info = tarfile.TarInfo(name=f"f{i}.txt")
            members.append((info, b"x"))
        tar_path.write_bytes(_tar_bytes_with_members(members))
        dest = tmp_path / "extracted"
        dest.mkdir()
        with pytest.raises(TarSecurityError, match="파일 개수"):
            safe_extract_tar(tar_path, dest, max_files=2, max_file_mb=20, max_total_mb=1024)

    def test_single_file_size_exceeded(self, tmp_path):
        tar_path = tmp_path / "archive.tar.gz"
        info = tarfile.TarInfo(name="big.txt")
        data = b"x" * 2048  # 2KB
        tar_path.write_bytes(_tar_bytes_with_members([(info, data)]))
        dest = tmp_path / "extracted"
        dest.mkdir()
        # 상한을 1KB(약 0.0009765625MB)로 설정해 2KB 파일이 초과되도록 함
        with pytest.raises(TarSecurityError, match="단일 파일 크기"):
            safe_extract_tar(tar_path, dest, max_files=1000, max_file_mb=1024 / (1024 * 1024), max_total_mb=1024)

    def test_total_size_exceeded(self, tmp_path):
        tar_path = tmp_path / "archive.tar.gz"
        members = []
        for i in range(4):
            info = tarfile.TarInfo(name=f"f{i}.txt")
            members.append((info, b"x" * 500))  # 파일당 500B, 총 2000B
        tar_path.write_bytes(_tar_bytes_with_members(members))
        dest = tmp_path / "extracted"
        dest.mkdir()
        # 단일 파일 상한은 넉넉히, 전체 상한만 1500B로 좁힘
        with pytest.raises(TarSecurityError, match="전체 해제 크기"):
            safe_extract_tar(
                tar_path, dest, max_files=1000,
                max_file_mb=1024 / (1024 * 1024) * 1000,  # 1000B 넉넉히
                max_total_mb=1500 / (1024 * 1024),
            )


# ---------------------------------------------------------------------------
# 오케스트레이터 (run()) — S6~S10
# ---------------------------------------------------------------------------

class TestRunOrchestration:
    def test_s3_bucket_unset_skips(self, monkeypatch, platform_docs_db):
        monkeypatch.setattr(settings, "PLATFORM_DOCS_S3_BUCKET", "")
        assert s3_docs_ingest.run() == 0

    def test_s9_current_json_not_found(self, s3_env, platform_docs_db):
        client = FakeS3Client()  # current.json을 아예 넣지 않음 → NoSuchKey
        assert s3_docs_ingest.run(client=client) == 1
        assert platform_docs_db.get_last_source_commit() is None

    def test_s9_current_json_access_denied(self, s3_env, platform_docs_db):
        client = FakeS3Client()
        client.set_error(
            "test-bucket", settings.PLATFORM_DOCS_S3_CURRENT_KEY,
            S3AccessDeniedError("denied"),
        )
        assert s3_docs_ingest.run(client=client) == 1
        assert platform_docs_db.get_last_source_commit() is None

    def test_s9_artifact_not_found(self, s3_env, platform_docs_db):
        current = _valid_current_json()
        client = FakeS3Client()
        client.put_object("test-bucket", settings.PLATFORM_DOCS_S3_CURRENT_KEY, json.dumps(current).encode())
        # artifact object는 넣지 않음 → NoSuchKey
        assert s3_docs_ingest.run(client=client) == 1
        assert platform_docs_db.get_last_source_commit() is None

    def test_s10_tmp_cleanup_on_failure(self, s3_env, platform_docs_db):
        before = set(Path(tempfile.gettempdir()).glob("docs_s3_*"))
        # catalog.json이 없는 tar (7단계에서 실패) → tmp_dir은 이미 생성된 뒤 실패
        info = tarfile.TarInfo(name="markdown/a.md")
        tar_bytes = _tar_bytes_with_members([(info, b"# t\n\n" + b"x" * 60)])
        current = _valid_current_json(sha256=hashlib.sha256(tar_bytes).hexdigest(), size=len(tar_bytes))
        client = _client_for("test-bucket", tar_bytes, current)
        assert s3_docs_ingest.run(client=client) == 1
        after = set(Path(tempfile.gettempdir()).glob("docs_s3_*"))
        assert after == before

    def test_s6_s7_s10_full_lifecycle(self, s3_env, platform_docs_db, fake_loader, fake_chroma, fake_finalize):
        collection, deleted_chunk_ids = fake_chroma
        before_tmp = set(Path(tempfile.gettempdir()).glob("docs_s3_*"))

        # --- Run 1: 신규 문서 2건 ---
        docs_v1 = {
            "cat-a": {"path": "platform/features/a.md", "title": "A", "markdown": _make_markdown("A")},
            "cat-b": {"path": "platform/features/b.md", "title": "B", "markdown": _make_markdown("B")},
        }
        tar1, current1 = _package(SOURCE_COMMIT_A, docs_v1)
        client1 = _client_for("test-bucket", tar1, current1)

        rc = s3_docs_ingest.run(client=client1)
        assert rc == 0
        assert len(fake_loader) == 1
        assert {d["id"] for d in fake_loader[0]} == {"docs-platform/features/a.md", "docs-platform/features/b.md"}
        assert platform_docs_db.get_last_source_commit() == SOURCE_COMMIT_A
        snapshot = platform_docs_db.get_doc_snapshot()
        assert set(snapshot.keys()) == {"cat-a", "cat-b"}

        # b가 삭제될 것에 대비해 컬렉션에 청크를 미리 넣어둠
        collection.add_chunk("docs-platform/features/b.md-chunk-0", "docs-platform/features/b.md")

        # --- S7: 동일 sourceCommit 재실행 → 스킵 ---
        rc = s3_docs_ingest.run(client=client1)
        assert rc == 0
        assert len(fake_loader) == 1  # 추가 호출 없음(그대로)
        assert platform_docs_db.get_last_source_commit() == SOURCE_COMMIT_A

        # --force 로는 동일 버전이어도 강제 재수집
        rc = s3_docs_ingest.run(client=client1, force=True)
        assert rc == 0
        assert len(fake_loader) == 2

        # --- Run 2: a는 그대로, b는 삭제, c는 신규 추가 ---
        docs_v2 = {
            "cat-a": docs_v1["cat-a"],
            "cat-c": {"path": "platform/features/c.md", "title": "C", "markdown": _make_markdown("C")},
        }
        tar2, current2 = _package(SOURCE_COMMIT_B, docs_v2)
        client2 = _client_for("test-bucket", tar2, current2)

        rc = s3_docs_ingest.run(client=client2)
        assert rc == 0
        assert len(fake_loader) == 3
        # a는 동일 콘텐츠라 재적재 대상에서 빠지고 c만 신규로 적재됨
        assert {d["id"] for d in fake_loader[2]} == {"docs-platform/features/c.md"}
        # b는 삭제 반영: FTS/컬렉션에서 제거됨
        assert "docs-platform/features/b.md-chunk-0" in deleted_chunk_ids
        assert "docs-platform/features/b.md-chunk-0" not in collection.docs

        assert platform_docs_db.get_last_source_commit() == SOURCE_COMMIT_B
        snapshot2 = platform_docs_db.get_doc_snapshot()
        assert set(snapshot2.keys()) == {"cat-a", "cat-c"}

        # --- S10: 임시 디렉터리가 남지 않았는지 확인 ---
        after_tmp = set(Path(tempfile.gettempdir()).glob("docs_s3_*"))
        assert after_tmp == before_tmp

    def test_s12_incremental_keeps_full_digest_snapshot(self, s3_env, platform_docs_db, fake_loader, fake_chroma, fake_finalize):
        """증분 적재에서도 그래프·digest_guids 마무리는 전체 카탈로그 기준이어야 한다.

        (검증에서 발견한 결함 고정: 변경분만 loader에 흘리면서 loader의 전체-교체형
        마무리를 그대로 두면 534건 GUID 스냅샷이 변경분 몇 건으로 truncate된다.)
        """
        guid_a = "AAAAAAAA-1111-2222-3333-444444444444"
        guid_b = "BBBBBBBB-1111-2222-3333-444444444444"

        def _digest_md(title, guid, body_suffix=""):
            return (
                f"# {title}\n\n"
                f"> - 원본: [{title}.pptx](https://o2olab.sharepoint.com/sites/x/_layouts/15/Doc.aspx?sourcedoc=%7B{guid}%7D&file={title}.pptx)\n"
                "> - 문서 기록 날짜: 2024-05-01 · 종류: 기획서 · 버전: v1.0\n"
                "> - SharePoint 위치: `/x`\n"
                "> - 관련 주제: 없음\n\n"
                f"다이제스트 본문입니다. 검색 색인용 내용이 충분히 길어야 합니다. {body_suffix}"
            )

        docs_v1 = {
            "dg-a": {"path": "platform/sharepoint-index/digests/2024-05_a.md", "title": "DA",
                     "markdown": _digest_md("DA", guid_a)},
            "dg-b": {"path": "platform/sharepoint-index/digests/2024-05_b.md", "title": "DB",
                     "markdown": _digest_md("DB", guid_b)},
        }
        tar1, current1 = _package(SOURCE_COMMIT_A, docs_v1)
        rc = s3_docs_ingest.run(client=_client_for("test-bucket", tar1, current1))
        assert rc == 0
        assert fake_finalize["guids"][-1] == {guid_a, guid_b}
        assert len(fake_finalize["rebuild"][-1][1]) == 2  # digest doc 노드 2건

        # v2: a만 내용 변경, b는 그대로 → loader에는 a만 가지만 마무리는 전체(2건) 기준
        docs_v2 = dict(docs_v1)
        docs_v2["dg-a"] = {**docs_v1["dg-a"], "markdown": _digest_md("DA", guid_a, body_suffix="개정판.")}
        tar2, current2 = _package(SOURCE_COMMIT_B, docs_v2)
        rc = s3_docs_ingest.run(client=_client_for("test-bucket", tar2, current2))
        assert rc == 0
        assert len(fake_loader[-1]) == 1  # 변경분(a)만 적재
        assert fake_finalize["guids"][-1] == {guid_a, guid_b}  # 전체 GUID 유지
        assert len(fake_finalize["rebuild"][-1][1]) == 2       # digest 노드도 전체 유지

    def test_s13_real_contract_path_normalization_and_scope(self, s3_env, platform_docs_db, fake_loader, fake_chroma, fake_finalize):
        """실 계약: catalog.path에 platform/ 접두사가 없고, 범위 밖 항목(README·색인 문서)이 포함된다.

        → platform/ 접두사를 보정해 로컬 체크아웃 수집과 doc_id를 일치시키고,
          DOCS_REPO_SUBDIRS 밖 항목과 README는 수집하지 않는다.
        """
        docs_v1 = {
            # 실 계약 형식: platform/ 접두사 없음 → docs-platform/features/a.md로 정규화되어야 함
            "cat-a": {"path": "features/a.md", "title": "A", "markdown": _make_markdown("A")},
            # 범위 밖: sharepoint-index 루트 색인 문서 → 수집 제외
            "cat-idx": {"path": "sharepoint-index/manifest-index.md", "title": "IDX", "markdown": _make_markdown("IDX")},
            # README → 수집 제외
            "cat-readme": {"path": "README.md", "title": "R", "markdown": _make_markdown("R")},
        }
        tar1, current1 = _package(SOURCE_COMMIT_A, docs_v1)
        rc = s3_docs_ingest.run(client=_client_for("test-bucket", tar1, current1))
        assert rc == 0
        assert {d["id"] for d in fake_loader[-1]} == {"docs-platform/features/a.md"}
        assert set(platform_docs_db.get_doc_snapshot().keys()) == {"cat-a"}

    def test_s8_indexing_failure_keeps_previous_state(self, s3_env, platform_docs_db, fake_loader, fake_chroma, monkeypatch):
        docs_v1 = {
            "cat-a": {"path": "platform/features/a.md", "title": "A", "markdown": _make_markdown("A")},
        }
        tar1, current1 = _package(SOURCE_COMMIT_A, docs_v1)
        client1 = _client_for("test-bucket", tar1, current1)

        def _boom(lines_iter, **kwargs):
            list(lines_iter)  # 소비는 하되
            raise RuntimeError("색인 중 강제 실패(테스트)")

        monkeypatch.setattr("company_llm_rag.data_loader.load_data_to_chromadb", _boom)

        rc = s3_docs_ingest.run(client=client1)
        assert rc == 1
        # 색인 실패 → 상태가 갱신되지 않아야 함 (마지막 성공 버전 없음 그대로)
        assert platform_docs_db.get_last_source_commit() is None
        assert platform_docs_db.get_doc_snapshot() == {}

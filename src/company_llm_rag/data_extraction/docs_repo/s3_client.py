"""
S3 클라이언트 추상화 (#62)

플랫폼 문서 S3 수집이 사용하는 최소 인터페이스(get_object)만 노출합니다.
- Boto3S3Client: 실제 AWS S3 접근. boto3는 지연 import — boto3 미설치 로컬 환경에서도
  이 모듈을 import하는 다른 코드(설정 로딩 등)가 깨지지 않도록 함.
- FakeS3Client: 테스트용. dict 기반 오브젝트 저장 + 오류 시뮬레이션(NoSuchKey/AccessDenied 등).
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple


class S3ClientError(Exception):
    """S3 접근 중 발생한 일반 오류."""


class S3ObjectNotFoundError(S3ClientError):
    """오브젝트가 존재하지 않음 (NoSuchKey/404)."""


class S3AccessDeniedError(S3ClientError):
    """접근 권한 없음 (AccessDenied/403)."""


class S3Client(ABC):
    """S3 접근 인터페이스. 지식허브는 GetObject만 필요로 한다 (ListBucket 불필요)."""

    @abstractmethod
    def get_object(self, bucket: str, key: str) -> bytes:
        """오브젝트 전체 내용을 bytes로 반환합니다.

        Raises:
            S3ObjectNotFoundError: 오브젝트가 없음
            S3AccessDeniedError: 접근 권한 없음
            S3ClientError: 그 외 S3 오류
        """
        raise NotImplementedError


class Boto3S3Client(S3Client):
    """boto3 기반 실제 S3 클라이언트. 표준 AWS 자격증명 체인(환경변수·IAM 역할)을 사용합니다."""

    def __init__(self, region: Optional[str] = None):
        self._region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3  # 지연 import
            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def get_object(self, bucket: str, key: str) -> bytes:
        import botocore.exceptions  # boto3와 함께 설치됨

        client = self._get_client()
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise S3ObjectNotFoundError(f"오브젝트 없음: s3://{bucket}/{key}") from e
            if code in ("AccessDenied", "403"):
                raise S3AccessDeniedError(f"접근 거부: s3://{bucket}/{key}") from e
            raise S3ClientError(f"S3 오류({code}): s3://{bucket}/{key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            # 네트워크·엔드포인트 등 클라이언트단 오류도 S3ClientError로 정규화
            raise S3ClientError(f"S3 연결 오류: s3://{bucket}/{key}: {e}") from e


class FakeS3Client(S3Client):
    """테스트용 dict 기반 S3 클라이언트. AWS 연결이 필요 없습니다."""

    def __init__(self, objects: Optional[Dict[Tuple[str, str], bytes]] = None):
        self._objects: Dict[Tuple[str, str], bytes] = dict(objects or {})
        self._errors: Dict[Tuple[str, str], Exception] = {}

    def put_object(self, bucket: str, key: str, data: bytes) -> None:
        self._objects[(bucket, key)] = data

    def set_error(self, bucket: str, key: str, error: Exception) -> None:
        """이후 get_object(bucket, key) 호출 시 주어진 예외를 발생시킵니다."""
        self._errors[(bucket, key)] = error

    def get_object(self, bucket: str, key: str) -> bytes:
        err = self._errors.get((bucket, key))
        if err is not None:
            raise err
        try:
            return self._objects[(bucket, key)]
        except KeyError:
            raise S3ObjectNotFoundError(f"오브젝트 없음(fake): s3://{bucket}/{key}")

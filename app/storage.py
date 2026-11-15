"""S3-compatible object storage (SeaweedFS locally, any S3 in the cloud). Calls run in worker threads."""

from collections.abc import Iterator

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class ObjectStore:
    """Thin async wrapper around one bucket."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
        addressing_style: str = "path",
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": addressing_style}),
        )

    async def ensure_bucket(self, create: bool) -> None:
        """Fail early if the bucket is missing (or create it, for local S3)."""

        def _check() -> None:
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                if not create:
                    raise
                self._client.create_bucket(Bucket=self.bucket)

        await anyio.to_thread.run_sync(_check)

    async def put_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        """Write a UTF-8 text object."""
        await anyio.to_thread.run_sync(
            lambda: self._client.put_object(Bucket=self.bucket, Key=key, Body=text.encode(), ContentType=content_type)
        )

    async def get_text(self, key: str) -> str:
        """Read a UTF-8 text object."""
        response = await anyio.to_thread.run_sync(lambda: self._client.get_object(Bucket=self.bucket, Key=key))
        return response["Body"].read().decode()

    async def delete(self, keys: list[str]) -> None:
        """Delete objects in batches of 1000 (the S3 limit per call)."""
        for i in range(0, len(keys), 1000):
            batch = [{"Key": key} for key in keys[i:i + 1000]]
            await anyio.to_thread.run_sync(
                lambda b=batch: self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": b, "Quiet": True})
            )

    def _iter_keys(self, prefix: str) -> Iterator[str]:
        """Yield every key under a prefix."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    async def list_keys(self, prefix: str) -> list[str]:
        """List every key under a prefix."""
        return await anyio.to_thread.run_sync(lambda: list(self._iter_keys(prefix)))

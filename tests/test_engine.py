"""Pytest suite for ServerlessBedrockTwinEngine."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from bedrock_twin import (
    BedrockTurnResponse,
    InvalidSessionTokenError,
    OptimisticLockError,
    S3ConversationState,
    ServerlessBedrockTwinEngine,
    WorkspaceIsolationError,
)


class _BytesBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class InMemoryETagS3:
    """In-memory S3 mock enforcing IfMatch / IfNoneMatch ETag preconditions."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._seq = 0

    def get_object(self, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                "GetObject",
            )
        body, etag = self.objects[(Bucket, Key)]
        return {"Body": _BytesBody(body), "ETag": etag}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: str) -> dict:
        existing = self.objects.get((Bucket, Key))
        if kwargs.get("IfNoneMatch") == "*" and existing is not None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Object exists"}},
                "PutObject",
            )
        if "IfMatch" in kwargs and (existing is None or existing[1] != kwargs["IfMatch"]):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "ETag mismatch"}},
                "PutObject",
            )
        self._seq += 1
        etag = f'"etag-{self._seq}"'
        self.objects[(Bucket, Key)] = (Body, etag)
        return {"ETag": etag}


class StubBedrock:
    """Configurable Bedrock Runtime Converse API stub."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def converse(self, **kwargs: object) -> dict:
        self.calls += 1
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"output": {"message": {"content": [{"text": str(outcome)}]}}}


def _throttle_error(code: str = "ThrottlingException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "Rate exceeded"}},
        "Converse",
    )


def test_tampered_session_signature_rejected() -> None:
    s3 = InMemoryETagS3()
    bedrock = StubBedrock(["Hello"])
    engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=bedrock,
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="dev",
    )
    token = engine.create_session_token("sess-1", now=1000)
    tampered = token.model_copy(update={"signature": "0" * 64})

    with pytest.raises(InvalidSessionTokenError):
        engine.converse(tampered, "Hi", now=1010)
    assert bedrock.calls == 0


def test_concurrent_s3_write_etag_conflict_detected() -> None:
    s3 = InMemoryETagS3()
    bedrock = StubBedrock(["First reply", "Concurrent reply"])
    engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=bedrock,
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="dev",
    )
    token = engine.create_session_token("sess-lock", now=1000)

    first = engine.converse(token, "Turn 1", now=1001)
    assert first.status == "ok"

    stale_state = S3ConversationState(
        session_id="sess-lock",
        workspace="dev",
        messages=[],
        etag='"stale-etag"',
    )
    with pytest.raises(OptimisticLockError):
        engine.save_state(stale_state)


def test_dev_vs_prod_workspace_prefix_isolation() -> None:
    s3 = InMemoryETagS3()
    dev_engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=StubBedrock(["Dev reply"]),
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="dev",
    )
    prod_engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=StubBedrock(["Prod reply"]),
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="prod",
    )

    dev_token = dev_engine.create_session_token("shared-id", now=1000)
    prod_token = prod_engine.create_session_token("shared-id", now=1000)

    dev_engine.converse(dev_token, "Hello dev", now=1005)
    prod_engine.converse(prod_token, "Hello prod", now=1005)

    assert ("twin-bucket", "dev/sessions/shared-id.json") in s3.objects
    assert ("twin-bucket", "prod/sessions/shared-id.json") in s3.objects

    with pytest.raises(WorkspaceIsolationError):
        prod_engine.verify_session_token(dev_token, now=1005)


def test_bedrock_throttling_structured_retry_handling() -> None:
    s3 = InMemoryETagS3()
    sleeps: list[float] = []
    bedrock = StubBedrock([_throttle_error(), _throttle_error(), _throttle_error()])
    engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=bedrock,
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="test",
        max_retries=2,
        base_backoff_seconds=0.1,
        sleep_fn=sleeps.append,
    )
    token = engine.create_session_token("sess-throttle", now=1000)
    resp = engine.converse(token, "Hello?", now=1005)

    assert isinstance(resp, BedrockTurnResponse)
    assert resp.status == "throttled"
    assert resp.retry_exhausted is True
    assert resp.attempts == 3
    assert resp.error_code == "ThrottlingException"
    assert sleeps == [0.1, 0.2]


def test_bedrock_transient_throttling_recovers() -> None:
    s3 = InMemoryETagS3()
    sleeps: list[float] = []
    bedrock = StubBedrock([_throttle_error(), "Recovered reply"])
    engine = ServerlessBedrockTwinEngine(
        s3_client=s3,
        bedrock_client=bedrock,
        bucket_name="twin-bucket",
        signing_secret="super-secret",
        workspace="dev",
        max_retries=2,
        base_backoff_seconds=0.05,
        sleep_fn=sleeps.append,
    )
    token = engine.create_session_token("sess-recover", now=1000)
    resp = engine.converse(token, "Retry me", now=1005)

    assert resp.status == "ok"
    assert resp.retry_exhausted is False
    assert resp.attempts == 2
    assert resp.reply == "Recovered reply"
    assert sleeps == [0.05]

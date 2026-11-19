"""Runnable CLI demo entrypoint for Serverless Bedrock Digital Twin Mk II."""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3

from bedrock_twin.engine import ServerlessBedrockTwinEngine


class _DemoBedrockClient:
    """Offline mock Bedrock client for local CLI demonstrations."""

    def converse(self, modelId: str, messages: list[dict], system: list[dict]) -> dict:
        last_user = messages[-1]["content"][0]["text"]
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": (
                                f"[Demo {modelId}] Received: {last_user!r}. "
                                "HMAC token verified and S3 ETag state persisted."
                            )
                        }
                    ]
                }
            }
        }


class _DemoS3Client:
    """In-memory ETag-enforcing S3 client for local CLI demonstrations."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._counter = 0

    def get_object(self, Bucket: str, Key: str) -> dict:
        from botocore.exceptions import ClientError

        if (Bucket, Key) not in self._objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                "GetObject",
            )
        body, etag = self._objects[(Bucket, Key)]
        return {"Body": _BytesBody(body), "ETag": etag}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: str) -> dict:
        from botocore.exceptions import ClientError

        existing = self._objects.get((Bucket, Key))
        if kwargs.get("IfNoneMatch") == "*" and existing is not None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Exists"}},
                "PutObject",
            )
        if "IfMatch" in kwargs and (existing is None or existing[1] != kwargs["IfMatch"]):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "ETag mismatch"}},
                "PutObject",
            )
        self._counter += 1
        etag = f'"demo-etag-{self._counter}"'
        self._objects[(Bucket, Key)] = (Body, etag)
        return {"ETag": etag}


class _BytesBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bedrock-twin",
        description="Serverless Bedrock Digital Twin Mk II CLI",
    )
    parser.add_argument("--workspace", default="dev", choices=["dev", "test", "prod"])
    parser.add_argument("--session-id", default="demo-session-001")
    parser.add_argument("--message", default="Summarise the serverless twin architecture.")
    parser.add_argument("--bucket", default=os.getenv("TWIN_S3_BUCKET", "twin-state-bucket"))
    parser.add_argument("--secret", default=os.getenv("TWIN_HMAC_SECRET", "local-dev-secret"))
    parser.add_argument("--live", action="store_true", help="Use live AWS S3 and Bedrock clients")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.live:
        s3_client = boto3.client("s3")
        bedrock_client = boto3.client("bedrock-runtime")
    else:
        s3_client = _DemoS3Client()
        bedrock_client = _DemoBedrockClient()

    engine = ServerlessBedrockTwinEngine(
        s3_client=s3_client,
        bedrock_client=bedrock_client,
        bucket_name=args.bucket,
        signing_secret=args.secret,
        workspace=args.workspace,
    )
    token = engine.create_session_token(args.session_id)
    response = engine.converse(token, args.message)
    print(json.dumps(response.model_dump(), indent=2))
    return 0 if response.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

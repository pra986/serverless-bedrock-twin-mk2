"""Core engine for Serverless Bedrock Digital Twin Mk II."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError

from bedrock_twin.models import (
    ALLOWED_WORKSPACES,
    BedrockTurnResponse,
    ConversationMessage,
    S3ConversationState,
    SignedSessionToken,
    WorkspaceType,
)

THROTTLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
}


class InvalidSessionTokenError(ValueError):
    """Raised when an HMAC session token is malformed, tampered, or expired."""


class WorkspaceIsolationError(ValueError):
    """Raised when a workspace is invalid or crosses dev/test/prod boundaries."""


class OptimisticLockError(RuntimeError):
    """Raised when S3 ETag conditional write detects concurrent modification."""


class ServerlessBedrockTwinEngine:
    """Stateless digital twin engine with HMAC tokens, S3 ETag locking, and workspace isolation."""

    def __init__(
        self,
        s3_client: Any,
        bedrock_client: Any,
        bucket_name: str,
        signing_secret: str | bytes,
        workspace: WorkspaceType = "dev",
        model_id: str = "amazon.nova-lite-v1:0",
        persona_prompt: str = "You are Pranil Deshmukh's serverless AI digital twin.",
        max_retries: int = 3,
        base_backoff_seconds: float = 0.05,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if workspace not in ALLOWED_WORKSPACES:
            raise WorkspaceIsolationError(
                f"Unsupported workspace '{workspace}'. Must be one of {ALLOWED_WORKSPACES}."
            )
        self.s3_client = s3_client
        self.bedrock_client = bedrock_client
        self.bucket_name = bucket_name
        self.signing_secret = (
            signing_secret.encode("utf-8")
            if isinstance(signing_secret, str)
            else signing_secret
        )
        self.workspace: WorkspaceType = workspace
        self.model_id = model_id
        self.persona_prompt = persona_prompt
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.sleep_fn = sleep_fn

    def _compute_signature(self, payload: str) -> str:
        return hmac.new(
            self.signing_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def create_session_token(
        self,
        session_id: str,
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> SignedSessionToken:
        if not session_id or ":" in session_id or "/" in session_id:
            raise InvalidSessionTokenError("Invalid session_id format.")
        issued_at = int(time.time()) if now is None else now
        expires_at = issued_at + ttl_seconds
        payload = f"{session_id}:{self.workspace}:{issued_at}:{expires_at}"
        signature = self._compute_signature(payload)
        return SignedSessionToken(
            session_id=session_id,
            workspace=self.workspace,
            issued_at=issued_at,
            expires_at=expires_at,
            signature=signature,
        )

    def verify_session_token(
        self,
        token: SignedSessionToken | str,
        now: int | None = None,
    ) -> SignedSessionToken:
        try:
            parsed = (
                SignedSessionToken.from_token_string(token)
                if isinstance(token, str)
                else token
            )
        except Exception as exc:
            raise InvalidSessionTokenError("Malformed session token.") from exc

        if parsed.workspace != self.workspace:
            raise WorkspaceIsolationError(
                f"Token workspace '{parsed.workspace}' does not match engine workspace '{self.workspace}'."
            )

        expected_sig = self._compute_signature(parsed.payload_string())
        if not hmac.compare_digest(parsed.signature, expected_sig):
            raise InvalidSessionTokenError("Invalid session token signature.")

        current_time = int(time.time()) if now is None else now
        if current_time > parsed.expires_at:
            raise InvalidSessionTokenError("Session token has expired.")

        return parsed

    def session_s3_key(self, session_id: str) -> str:
        return f"{self.workspace}/sessions/{session_id}.json"

    def load_state(self, session_id: str) -> S3ConversationState:
        key = self.session_s3_key(session_id)
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"NoSuchKey", "404"}:
                return S3ConversationState(
                    session_id=session_id,
                    workspace=self.workspace,
                    messages=[],
                    etag=None,
                )
            raise

        raw_body = response["Body"].read().decode("utf-8")
        data = json.loads(raw_body)
        state = S3ConversationState.model_validate(data)
        if state.workspace != self.workspace:
            raise WorkspaceIsolationError(
                f"State workspace '{state.workspace}' violates '{self.workspace}' isolation."
            )
        state.etag = response.get("ETag")
        return state

    def save_state(self, state: S3ConversationState) -> str:
        if state.workspace != self.workspace:
            raise WorkspaceIsolationError(
                f"Cannot save '{state.workspace}' state from '{self.workspace}' engine."
            )
        state.updated_at = datetime.now(timezone.utc).isoformat()
        payload = state.model_dump(exclude={"etag"})
        body = json.dumps(payload).encode("utf-8")
        key = self.session_s3_key(state.session_id)

        put_kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
        }
        if state.etag:
            put_kwargs["IfMatch"] = state.etag
        else:
            put_kwargs["IfNoneMatch"] = "*"

        try:
            response = self.s3_client.put_object(**put_kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status_code == 412:
                raise OptimisticLockError(
                    f"Concurrent S3 write conflict for key '{key}' (stale ETag={state.etag})."
                ) from exc
            raise

        new_etag = response.get("ETag", "")
        state.etag = new_etag
        return new_etag

    def _invoke_bedrock_with_retry(
        self,
        messages: list[ConversationMessage],
    ) -> tuple[str | None, int, bool, str | None, float | None]:
        bedrock_messages = [
            {"role": msg.role, "content": [{"text": msg.content}]} for msg in messages
        ]
        total_allowed = 1 + self.max_retries
        last_error_code: str | None = None
        last_backoff: float | None = None

        for attempt in range(1, total_allowed + 1):
            try:
                resp = self.bedrock_client.converse(
                    modelId=self.model_id,
                    messages=bedrock_messages,
                    system=[{"text": self.persona_prompt}],
                )
                reply_text = resp["output"]["message"]["content"][0]["text"]
                return reply_text, attempt, False, None, None
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code not in THROTTLE_ERROR_CODES:
                    raise
                last_error_code = error_code
                last_backoff = self.base_backoff_seconds * (2 ** (attempt - 1))
                if attempt < total_allowed:
                    self.sleep_fn(last_backoff)

        return None, total_allowed, True, last_error_code, last_backoff

    def converse(
        self,
        token: SignedSessionToken | str,
        user_message: str,
        now: int | None = None,
    ) -> BedrockTurnResponse:
        verified = self.verify_session_token(token, now=now)
        state = self.load_state(verified.session_id)
        state.messages.append(ConversationMessage(role="user", content=user_message))

        reply_text, attempts, retry_exhausted, error_code, retry_after = (
            self._invoke_bedrock_with_retry(state.messages)
        )

        if retry_exhausted or reply_text is None:
            return BedrockTurnResponse(
                status="throttled",
                session_id=verified.session_id,
                workspace=self.workspace,
                reply="Bedrock service is temporarily throttled. Please retry shortly.",
                attempts=attempts,
                retry_exhausted=True,
                etag=state.etag,
                error_code=error_code,
                retry_after_seconds=retry_after,
            )

        state.messages.append(ConversationMessage(role="assistant", content=reply_text))
        new_etag = self.save_state(state)

        return BedrockTurnResponse(
            status="ok",
            session_id=verified.session_id,
            workspace=self.workspace,
            reply=reply_text,
            attempts=attempts,
            retry_exhausted=False,
            etag=new_etag,
        )

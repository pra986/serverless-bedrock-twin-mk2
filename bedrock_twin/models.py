"""Pydantic data models for Serverless Bedrock Digital Twin Mk II."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

ALLOWED_WORKSPACES: tuple[str, ...] = ("dev", "test", "prod")
WorkspaceType = Literal["dev", "test", "prod"]


class ConversationMessage(BaseModel):
    """Single conversation turn stored in S3 state."""

    role: Literal["user", "assistant"]
    content: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SignedSessionToken(BaseModel):
    """HMAC-SHA256 signed session token bound to a workspace and TTL."""

    session_id: str
    workspace: WorkspaceType
    issued_at: int
    expires_at: int
    signature: str

    def payload_string(self) -> str:
        return f"{self.session_id}:{self.workspace}:{self.issued_at}:{self.expires_at}"

    def to_token_string(self) -> str:
        return f"{self.payload_string()}:{self.signature}"

    @classmethod
    def from_token_string(cls, raw: str) -> "SignedSessionToken":
        parts = raw.split(":")
        if len(parts) != 5:
            raise ValueError("Malformed signed session token")
        session_id, workspace, issued_raw, expires_raw, signature = parts
        return cls(
            session_id=session_id,
            workspace=workspace,  # type: ignore[arg-type]
            issued_at=int(issued_raw),
            expires_at=int(expires_raw),
            signature=signature,
        )


class S3ConversationState(BaseModel):
    """Conversation state persisted in S3 with ETag optimistic locking."""

    session_id: str
    workspace: WorkspaceType
    messages: list[ConversationMessage] = Field(default_factory=list)
    etag: str | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class BedrockTurnResponse(BaseModel):
    """Structured response returned for each digital twin turn."""

    status: Literal["ok", "throttled"]
    session_id: str
    workspace: WorkspaceType
    reply: str
    attempts: int = 1
    retry_exhausted: bool = False
    etag: str | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None

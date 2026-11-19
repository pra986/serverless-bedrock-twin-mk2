# Architecture: Serverless Bedrock Digital Twin Mk II

## Overview

`serverless-bedrock-twin-mk2` implements a stateless, serverless digital twin backed by Amazon Bedrock and Amazon S3, designed for deployment behind Amazon CloudFront, Amazon API Gateway (HTTP API), and AWS Lambda with Terraform workspace isolation (`dev`, `test`, `prod`).

## Request & State Flow

```
Client (CloudFront)
  │
  ▼  POST /api/chat (session_token, message)
API Gateway (HTTP API)
  │
  ▼
AWS Lambda / ServerlessBedrockTwinEngine
  ├── 1. Verify HMAC-SHA256 SignedSessionToken (constant-time digest + expiry)
  ├── 2. Resolve S3 key with strict workspace prefix: <workspace>/sessions/<session_id>.json
  ├── 3. Load S3ConversationState + current ETag (or initialize new state)
  ├── 4. Call Amazon Bedrock Runtime Converse API with exponential backoff on throttling
  └── 5. Persist updated conversation state to S3 using If-Match: <etag> optimistic locking
```

## Key Design Guarantees

1. **HMAC-Signed Session Tokens (`SignedSessionToken`)**
   - Session tokens bind `session_id`, `workspace`, `issued_at`, and `expires_at` with an HMAC-SHA256 signature.
   - Verification uses `hmac.compare_digest` to prevent timing attacks and rejects tampered, expired, or cross-workspace tokens before any S3 or Bedrock call.

2. **S3 ETag Optimistic Locking (`S3ConversationState`)**
   - Reads capture the object's `ETag`.
   - Writes pass `IfMatch=etag` on `put_object` (or `IfNoneMatch="*"` on initial creation) so concurrent requests for the same session cannot silently clobber each other.
   - `412 PreconditionFailed` / `ConditionalRequestConflict` errors surface as a structured `OptimisticLockError`.

3. **Strict Terraform Workspace Prefix Isolation**
   - Allowed workspaces are strictly `dev`, `test`, and `prod`.
   - Every S3 object key is scoped under `<workspace>/sessions/<session_id>.json`, preventing cross-environment state leakage.

4. **Structured Bedrock Throttling Retries (`BedrockTurnResponse`)**
   - `ThrottlingException` / `ServiceUnavailableException` responses from Bedrock Runtime are retried with bounded exponential backoff.
   - Outcomes return a structured `BedrockTurnResponse` containing `status` (`"ok"` or `"throttled"`), `reply`, `attempts`, `retry_exhausted`, `workspace`, `session_id`, and `etag`.

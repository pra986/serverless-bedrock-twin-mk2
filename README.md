# Serverless Bedrock Digital Twin Mk II (Project 27 / D2)

Serverless AWS Bedrock digital twin deployed on CloudFront, API Gateway, AWS Lambda, S3, and Terraform workspaces with HMAC-signed session tokens and S3 ETag optimistic locking.

## Architecture

- **Edge & Frontend Delivery**: Amazon CloudFront serving static web assets from S3 and routing `/api/*` requests to API Gateway.
- **API Layer**: Amazon API Gateway (HTTP API) with strict CORS policies and request throttling.
- **Compute**: AWS Lambda (Python 3.12) hosting the digital twin inference and session-state handler.
- **Foundation Model**: Amazon Bedrock Runtime (`Converse` API) using configurable Nova/Claude model IDs with persona context injection.
- **State Persistence**: Amazon S3 storing conversation histories keyed by signed `session_id` with `If-Match` ETag optimistic locking.
- **Infrastructure as Code**: Terraform workspaces (`dev`, `staging`, `prod`) managing isolated S3 buckets, IAM least-privilege policies, Lambda packaging, API Gateway routes, and CloudFront distributions.

## 6-Stage Workflow

1. **Session Bootstrap**: Client requests or resumes a session; server issues or verifies an HMAC-SHA256 signed session token (`session_id` + expiry + signature).
2. **Input Validation**: Lambda validates message length, character bounds, and payload schema before invoking downstream services.
3. **Optimistic State Load**: Lambda fetches the existing conversation JSON and current `ETag` from S3 (`sessions/<session_id>.json`).
4. **Persona Context & Bedrock Inference**: Persona facts, guardrails, and bounded conversation history are sent to AWS Bedrock.
5. **Conditional S3 Write (ETag Locking)**: Updated conversation turns are persisted to S3 using `If-Match=<etag>` (or `If-None-Match=*` on creation) to prevent lost updates under concurrent requests.
6. **Structured Response**: API Gateway returns the assistant reply, updated signed session token, and request metadata to the client.

## Data Contract

### Request (`POST /chat`)
```json
{
  "session_token": "sess_abc123.1760000000.hmac_sig",
  "message": "What production AI systems have you built?"
}
```

### Response (`200 OK`)
```json
{
  "session_id": "sess_abc123",
  "session_token": "sess_abc123.1760003600.hmac_sig",
  "reply": "I have designed serverless Bedrock assistants with Terraform...",
  "etag": "\"d41d8cd98f00b204e9800998ecf8427e\"",
  "model_id": "amazon.nova-micro-v1:0"
}
```

## Production Controls

- **Signed Session Tokens**: Prevents client-side session ID spoofing via server-side HMAC-SHA256 verification and TTL expiration.
- **S3 ETag Optimistic Locking**: Detects concurrent writes (`412 Precondition Failed`) and retries or rejects stale updates safely.
- **Bounded Context & Input Limits**: Enforces maximum prompt length and sliding history window to bound Bedrock token spend.
- **Workspace Isolation**: Terraform workspaces separate state, S3 buckets, and IAM roles across environments.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest -v
```

## Attribution

Inspired by and adapted from `ed-donner/production` (`week2` Digital Twin Mk II curriculum).

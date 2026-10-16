# Architecture Decision Records (ADR)

This running record captures key architectural and design decisions for the Sewa Setu AI Assistant, including rationale, trade-offs, and alternatives considered.

---

## portal-is-the-only-rules-engine

### Context
Sewa Setu is a government welfare scheme where statutory rules dictate eligibility: age ≥ 60 on scheme deadline/submission date, valid IFSC code, 9-18 digit bank account, required fields, one active application per mobile, and application window closure. If validation or state-transition logic were replicated across the portal, the internal agent API, and the MCP servers, rules would inevitably drift, resulting in discrepancies where an assistant might admit or refuse an application that the web counter would treat differently.

### Decision
The portal (`app/app.py`) is the sole, definitive rules engine:
1. All validation logic resides exclusively in `validate_application(data, doc_path)` and `active_application(cur, mobile)`.
2. A single submission core function, `submit_application_for(mobile, data, doc_path, channel, client_name)`, is shared identically by the web submission handler (`handle_submission`) and the internal agent API (`POST /internal/agent/submit`).
3. Deciding applications (`decide_application(app_id, new_status, actor, note)`) shares identical SQL guards (`WHERE status = 'PENDING'`) and audit logging.
4. MCP servers never perform local business rule validation; they relay requests to the portal's internal agent API and return the portal's canonical error messages and decisions.

### Alternatives Considered
* *Re-implementing validations in Python MCP servers:* Rejected. Violates the core requirement that an AI assistant must never get anyone a pension the counter or web form would not. Creates code duplication and high maintenance overhead.
* *Storing rules in a shared YAML/JSON configuration file:* Insufficient, because state-dependent checks (such as active application uniqueness and SLA deemed-approval cutoffs) require transactional database queries.

---

## no-OTP-in-assistant

### Context
Elderly citizens apply for welfare benefits on shared phones, rural CSC kiosks, or via assisted typing. A common vulnerability in conversational AI assistants is requesting one-time passwords (OTPs) or portal credentials directly from users in chat, creating severe phishing risks, logging credentials in chat transcripts, and violating government security protocols.

### Decision
The assistant and MCP servers **never handle, request, log, or transmit OTPs, passwords, or authentication secrets**:
1. All citizen and officer authentication is performed directly on the portal's web interface via OAuth 2.1 (RFC 8414 discovery, RFC 7591 dynamic client registration, RFC 8707 PKCE authorization code grant).
2. Citizens verify ownership of their mobile number and consent to client application access on the portal's native screens.
3. The citizen's date of birth (used as the portal password for the citizen status portal) is never handled by or revealed to the assistant.
4. Only simulated evaluation test runners acting as the human user are permitted to read `/__gateway/api/messages` to automate end-to-end testing; the assistant runtime has no access to telecom gateway messages.

### Alternatives Considered
* *Allowing the assistant to prompt for OTP in conversation and relay to the portal:* Rejected. Exposes users to social engineering, violates OAuth 2.1 standards, and breaks kiosk privacy requirements.

---

## separate-endpoints-and-audiences

### Context
The Sewa Setu ecosystem serves two fundamentally different roles:
1. **Citizens:** Submitting personal details, checking eligibility, uploading age proof, reviewing summaries, checking status, and withdrawing applications.
2. **Block Officers:** Viewing the administrative queue, inspecting application details, and approving or rejecting applications.

If both roles shared an endpoint or audience, a compromised or misconfigured client could access officer capabilities, or an LLM might hallucinate officer tool calls in a citizen conversation.

### Decision
Complete separation of endpoints, processes, and OAuth audiences:
1. **Separate Endpoints:** Citizen tools are served strictly at `/mcp/citizen`; officer tools are served strictly at `/mcp/officer`.
2. **Canonical Resource URLs & Required `aud` Claims:**
   - Citizen MCP Resource URL: `${PUBLIC_BASE_URL}/mcp/citizen`
   - Officer MCP Resource URL: `${PUBLIC_BASE_URL}/mcp/officer`
   These are the **exact** canonical audience (`aud`) strings that the Phase 4 MCP resource servers MUST require when verifying incoming OAuth 2.1 access tokens. Any token with an `aud` that does not match this server's exact canonical URL is rejected with HTTP 401.
3. **Strict Resource Server Rejection:**
   - A citizen token presented to `/mcp/officer` returns HTTP 401/403.
   - An officer token presented to `/mcp/citizen` returns HTTP 401/403.
   - Any token with mismatched `aud` is rejected immediately.
4. **Tool Isolation:** `tools/list` on `/mcp/citizen` exposes only the 8 citizen tools; `tools/list` on `/mcp/officer` exposes only the 5 officer tools.

### Alternatives Considered
* *Single unified MCP server with runtime role checks inside tools:* Rejected. Exposing officer tools in tool schemas sent to citizen LLMs invites prompt manipulation and violates least-privilege security design.

---

## deterministic-officer-flags

### Context
Officers reviewing pension applications must make accurate, legally compliant decisions under statutory deadlines. Relying on an LLM to spot fraudulent patterns, under-age applicants, duplicate bank accounts, or prompt injection in applicant text is non-deterministic, prone to hallucinations, and vulnerable to adversarial attack.

### Decision
Safety-critical checks and officer flags are computed using **deterministic Python code and SQL queries**, not probabilistic model evaluations:
1. **Hard Flags (Block Approval):**
   - `age_below_60_at_submission`: Explicitly calculated using `age_on(dob, submitted_at.date()) < 60`. Blocks approval; reject is always permitted.
2. **Soft Flags (Informational Review):**
   - `age_borderline`: Applicant was within 30 days of turning 60 at submission.
   - `ifsc_malformed`: Regex verification against `^[A-Z]{4}0[A-Z0-9]{6}$`.
   - `duplicate_bank_account`: Same bank account number appears on another active application with a different mobile.
   - `no_document_on_file`: Document path is empty (soft flag to accommodate legacy seed data).
   - `suspicious_text`: Detects instruction-like patterns (e.g. `"ignore"`, `"approve this"`, `"system"`, `"assistant"`, angle brackets `< >`) in untrusted citizen fields.
   - `near_deemed_approval`: Application is within 3 days of statutory 15-day deemed approval.
   - `overdue`: Application has exceeded the 15-day statutory SLA.
3. **Mandatory Case View Prior to Decision:** An officer cannot approve or reject an application without having retrieved and inspected the case via `get_case` within the preceding 60 minutes.

### Alternatives Considered
* *Using an LLM agent to summarize and recommend approval/rejection:* Rejected for compliance and safety. Decisions must be verifiable, auditable, and resistant to prompt injection attacks embedded in applicant-entered text.

---

## staging-vs-production-evals

### Context
Evaluation harnesses test edge cases, deliberate injection attempts, invalid applications, under-age applicants, and malicious inputs. Running automated evals against the production database would pollute real application registries, distort administrative queues, trigger unintended deemed approvals, and compromise production audit logs inspected by reviewers and the Director.

### Decision
1. **Environment Isolation:** Automated evals run against an isolated **staging environment** (`sewasetu-staging` Compose project) with an independent database volume (`staging_pgdata`).
2. **Production Hygiene:** The production environment remains clean and holds only legitimate portal data and reviewer test runs.
3. **Evaluation Dashboard Transparency:** The `/eval-dashboard` clearly displays that evaluation suites run against the staging environment, documenting all assertions, tool traces, and verification queries independently of the production operational store.

### Alternatives Considered
* *Running evals on production with a test flag on applications:* Rejected. Risk of data leakage, query skew in administrative dashboards, and accidental deemed approval of test rows.

---

## draft-review-code-commit-protocol

### Context
Elderly applicants assisted by AI models or third-party operators may have drafts edited mid-conversation or modified without explicit consent. If an assistant could submit a draft without an explicit summary review and confirmation, hallucinated or modified banking information could be submitted to the government.

### Decision
A cryptographic review-code protocol is enforced in `POST /internal/agent/review` and `POST /internal/agent/submit`:
1. When `POST /review` is called on a valid draft, the server stores a SHA-256 hash of the complete draft data + document path (`review_hash`) and returns a temporary review code (valid 10 minutes).
2. The assistant is instructed to read the summary (including masked bank account) to the citizen and receive explicit approval.
3. `POST /submit` requires the review code. The portal verifies the review code, re-hashes the current draft + document, and asserts that the draft was not modified since review. Any mutation to the draft immediately invalidates prior review codes.
4. On successful submission, drafts and review codes are purged from `agent_drafts` and `agent_reviews`.

---

## session-liveness-check-via-internal-api

### Context
OAuth 2.1 access tokens are stateless JWTs signed with RS256. If a citizen at a public kiosk or an officer leaves their workstation and revokes their session via `/oauth/revoke` or the portal's Connected Applications page, the refresh token is marked revoked in `oauth_refresh_tokens`, but the access token remains cryptographically valid for its TTL (20–30 minutes) if verified solely via public JWKS keys.

### Decision
The portal provides an internal endpoint `GET /internal/agent/session-live?sub=&client_id=&aud=`:
1. Verifies that a non-revoked, unexpired row exists in `oauth_refresh_tokens` for the given subject, client_id, and resource audience.
2. The MCP resource servers query this endpoint and cache the result for up to 5 seconds.
3. If the session is no longer live, the assistant returns HTTP 401 with `WWW-Authenticate`, terminating access immediately upon revocation and ensuring kiosk safety.


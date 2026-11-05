# Security & privacy controls

How each control in ARCHITECTURE §12 is implemented and where it is tested. Run the
suite with `pytest` (backend) and `npm run test:e2e` (frontend). Test IDs refer to
IMPLEMENTATION_PLAN §4.

## Reporting a vulnerability

Please don't open a public issue. Email the maintainers with the steps to reproduce.
We acknowledge reports within 2 working days.

## OWASP Top 10 (2021)

| Risk | Control | Code | Tests |
|---|---|---|---|
| A01 Broken access control | Every order, quote, KYC application and export is checked for ownership; other people's records return 404. Back office: deny-by-default RBAC, partner scoping in queries. Erased principals' tokens are refused at once. | `api/deps.py`, `backoffice/rbac.py`, `privacy/rights.py` | AC-01, AC-02, `test_privacy.py` |
| A02 Cryptographic failures | AES-256-GCM everywhere. Each person has their own random data key, wrapped by a key derived from `MASTER_KEY`; associated data binds every ciphertext to its owner and slot. HSTS on API and PWA. Secrets come only from the environment. | `security/vault.py`, `kyc/storage.py` | AC-06, `test_vault.py`, `test_kyc_infra.py` |
| A03 Injection | Queries go through the ORM with bound parameters, and every body is validated by Pydantic (`extra="forbid"`). No shell call takes user input; Tesseract is called with fixed argv. The export is JSON (no CSV, so formula injection doesn't arise). | all routes | `test_upload.py`, API 422 tests |
| A04 Insecure design | Deterministic engines; HMAC-signed, immutable quotes; a server-side order state machine; append-only logs | `domain/*`, `orders/state_machine.py` | PR-*, TR-01, AC-07 |
| A05 Misconfiguration | Strict CORS: exact origins, https in production, no credentials mode. CSP and the security headers below go on every response. No docs or OpenAPI in production. Uvicorn runs without a `Server` header. Containers run read-only as non-root. | `security/headers.py`, `config.py`, `next.config.ts` | AC-09, `test_hardening.py`, `shell.spec.ts` |
| A06 Vulnerable components | Hash-pinned Python locks. CI runs `pip-audit --strict` and `npm audit` (runtime deps clean; dev deps fail at high), plus a weekly scheduled run and Dependabot. | `.github/workflows/ci.yml` | CI |
| A07 Authentication failures | Phone OTP: only an HMAC of the code is stored. A challenge locks after 5 wrong codes, and the phone locks for 60 min after 10 wrong codes across challenges. Requests are limited per phone and per IP. Staff use scrypt hashes, lock after 5 failures and get separate token keys. | `checkout/otp.py`, `backoffice/staff.py` | AC-08, `test_backoffice.py` |
| A08 Integrity failures | Hash-chained consent ledger and audit log, guarded by ORM checks and SQLite triggers; HMAC-signed quotes | `audit/log.py`, `consent/ledger.py` | AC-07, `test_consent_ledger.py` |
| A09 Logging failures | Every log record is masked when it is created. A static audit forbids unmasked personal-data arguments and non-literal messages. Each request gets an `X-Request-ID`. Security events go to `udyam.security`. | `security/pii_mask.py`, `security/headers.py` | AC-03, `test_pii_logs.py`, `test_pii_mask.py` |
| A10 SSRF | The server never fetches a URL supplied by a user | n/a | — |

### HTTP headers

**API, on every response:**
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
- `Strict-Transport-Security: max-age=63072000; includeSubDomains`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- COOP/CORP
- `Permissions-Policy` denying everything
- `Cache-Control: no-store` on `/api/*`

**PWA:** the same set, with this CSP:
- Scripts, styles and fonts come from this origin only.
- `connect-src` allows this origin and the API.
- `object-src 'none'`, `frame-ancestors 'none'`.

The pages are pre-rendered static HTML, so Next's inline bootstrap scripts need `'unsafe-inline'`. Per-request nonces would require server rendering. No `eval` is allowed outside the dev server.

### Rate limits
Every `/api/v1` route sits behind a per-IP backstop (`API_IP_RATE_PER_MINUTE`, 300 per minute). On top of that, each route has its own per-IP limit and a per-person or per-staff limit:
- Lookups and the advisor
- OTP, per phone and per IP
- Checkout, KYC and privacy
- Back-office login and reveal

The suite checks each operation in the OpenAPI schema and confirms it returns 429 once the budget is spent.

## OWASP LLM Top 10

| Risk | Control | Tests |
|---|---|---|
| LLM01 Prompt injection | User text is wrapped as data; the model has no tools and must return the slot schema; prices come only from the signed quote | LL-04, LL-05 |
| LLM02 Insecure output handling | LLM text is never rendered as HTML. A verifier rejects any number or product that isn't in the quote, and the deterministic fallback takes over. | LL-01, LL-02 |
| LLM04 Model DoS / cost | Caps on turns, message length and output tokens; a per-IP advisor limit; one retry, then the guided form | LL-07, LL-11 |
| LLM06 Sensitive information | A PII redactor runs before Groq. KYC data never reaches the model, and the outbound payload is captured in tests. | LL-06 |
| LLM08 Excessive agency | The model has no tools and cannot change state | LL-04 |
| LLM09 Overreliance | Verifier, deterministic engines, and a complete journey without the LLM | LL-07, LL-10 |
| Insecure file upload | Type checked by magic bytes, 5 MB cap, decompression-bomb guard, re-encoding (EXIF/GPS stripped), PDFs rasterised (scripts gone), random keys | UP-01 … UP-07 |

## DPDP Act 2023

| Control | Implementation | Tests |
|---|---|---|
| Notice & unbundled consent | The consent screen asks yes or no for each purpose; nothing is pre-ticked; the notice is versioned (409 if outdated) | AC-05, `checkout.spec.ts` |
| Consent ledger | Append-only and hash-chained; included in the export | AC-07 |
| Purpose limitation | KYC, lookup, order and payment routes are gated on KYC consent | AC-05, UP-07 |
| Minimisation & retention | The advisor is anonymous until checkout. Raw KYC images are deleted 7 days after activation, a drop (cancelled, not feasible, or 30 days idle) or withdrawal. Expired vault entries and OTP-held phones are deleted too. Run `python -m scripts.retention_job` daily. | `test_retention.py` |
| Masking | GSTIN `27XUIX******1ZP`, Udyam `UDYAM-MH-00-***6696`, names as initials, phone as its last 2 digits | AC-03, `test_pii_mask.py` |
| Access / export | `GET /api/v1/privacy/export` returns everything held about the person, decrypted, `no-store` | `test_privacy.py` |
| Correction | `PATCH /api/v1/privacy/kyc/{id}`: the KYC goes back to DRAFT and is re-checked. A verified KYC is changed through a reviewer. The audit records field names, never values. | `test_privacy.py` |
| Erasure | `POST /api/v1/privacy/erasure {"confirm":"ERASE"}` cancels open orders (refund recorded), withdraws every consent and deletes images, sealed data, vault entries and the phone mapping. It then **destroys the person's data key** and revokes their tokens. Erasure is refused while an install is booked or the service is live. | AC-06 |
| Withdrawal | `POST /api/v1/privacy/withdrawal` or `DELETE /api/v1/consents/{purpose}`: processing stops at once and deletion is scheduled | AC-05, J5 |
| Processors | Groq receives only redacted advisor text (cross-border processing, disclosed in the notice); Tesseract runs locally | LL-06 |

## Secrets

- **Secrets live only in the environment.** `.env` is git-ignored, and `.env.example` has empty placeholders.
- **Production refuses to start without strong keys.** `MASTER_KEY` and `QUOTE_HMAC_KEY` must each be at least 32 characters.
- **Pre-commit blocks private keys** (`detect-private-key`).
- **CI scans the full git history** with gitleaks: job `secrets`, default rules plus `.gitleaks.toml`.

## Dependency audit status

| Ecosystem | Result |
|---|---|
| Python (`requirements-dev.lock`, `pip-audit --strict`) | No known vulnerabilities |
| npm runtime (`npm audit --omit=dev`) | 0 vulnerabilities |
| npm dev | 6 moderate, all one advisory: `minimatch` ReDoS (GHSA-g3hv-75wg-m249), pulled in through `@typescript-eslint/*` under `eslint-config-next` |

**Why the dev advisory is accepted:** the code only runs in lint tooling on trusted source files. `npm audit fix` upgrades the plugin to a version that breaks ESLint 9's `no-unused-expressions` rule, so the advisory is waiting for an `eslint-config-next` release. CI fails on high or above.

## Breach-response runbook

1. **Contain**
   - Rotate `MASTER_KEY` and `QUOTE_HMAC_KEY`. Re-wrapping the per-person keys under the new key is an offline job.
   - Revoke staff accounts and invalidate tokens by rotating the key.
   - Block the source IPs.
2. **Assess**
   - Use the audit log (hash-chained, so check `verify_chain`) and the request IDs to scope what was accessed. `PII_REVEAL` rows show which staff user saw which fields.
   - Ciphertext without `MASTER_KEY` is unreadable. Erased people's data stays unreadable even with it, because their key row is empty.
3. **Notify**
   - Notify the Data Protection Board and affected Data Principals without delay, as the DPDP Act requires.
   - Use the grievance contact in the notice.
4. **Recover and learn**
   - Restore from backup, then re-apply every erasure made since that backup was taken. The `DATA_ERASED` audit rows list them; keep that list outside the backup.
   - Write up the timeline and add a regression test.

**Erasure and backups:** in this demo the wrapped per-person keys sit in the same database as the data. So a full database backup taken *before* an erasure can still decrypt that person's data until the backup expires. Production should keep the key table in a separate KMS-backed store with short backup retention.

# Security: threats, mitigations and reviews

What PragMattie PMO defends against, how, and which test proves it. Written for item 2.4 and kept
up to date by every change that touches login, sessions, permissions, the database or the agents.
It follows rules R6 (access, protection and a review schedule) and R10 (the risk register holds
risks from using AI as well as ordinary ones) in `policies/project-rules.md`.

Test names are `file::test` under `apps/api/tests/` unless they start with `apps/web/`. The
security suite is `apps/api/tests/security/`; CI runs it as the `API (security tests)` check, and
the two `mysql` tests run on real MySQL in `API (migrations on MySQL)`.

## Seed risks for the risk register

Every threat below carries a seed id (`SR-nn`). When the risk register arrives in P4, each one
becomes a starting entry there, unscored: a person scores it on the agreed probability and impact
scales (`policies/risk.yaml`), names an owner and decides the response. Threats SR-20 to SR-24
come from using AI (R10); the rest are ordinary web and data threats.

## Web and data threats

| Id | Threat | Mitigation | Tests |
| --- | --- | --- | --- |
| SR-01 | Someone reaches a route they have no right to, or a new route ships without a check. | Every route depends on a guard from `app/authz.py`; only health, login, logout and the dev/test docs are open. One permissions table. | `test_authz.py::test_every_route_has_a_guard`, `test_authz.py::test_matrix`, `security/test_authz_matrix.py::test_a_claimed_lesser_role_takes_nothing_away` |
| SR-02 | A user raises their own rights with a session token that claims a bigger role. | The role is read from the user's database row on every request, never from the token. | `security/test_authz_matrix.py::test_a_claimed_admin_role_grants_nothing`, `security/test_authz_matrix.py::test_an_unknown_claimed_role_is_ignored` |
| SR-03 | A forged or altered session token: no algorithm, wrong key, edited payload, expired, missing claims. | HS256 only, a key of at least 32 bytes, required claims (`sub`, `role`, `iat`, `exp`, `jti`), expiry checked. | `security/test_jwt.py::test_a_tampered_token_is_401`, `security/test_jwt.py::test_a_token_expiring_now_is_401` |
| SR-04 | A stolen session keeps working after sign-out. | Logout revokes the token's `jti` until it would expire; a new login revokes the session it replaces. | `security/test_jwt.py::test_a_revoked_token_is_401`, `test_auth.py::test_logout_revokes_the_token_and_clears_cookies`, `test_auth.py::test_a_new_login_revokes_the_session_it_replaces` |
| SR-05 | Page script (for example injected script) reads the session cookie. | The session cookie is HttpOnly, Secure and SameSite=Lax; only the CSRF cookie is readable. | `security/test_cookies.py::test_session_cookie_is_httponly_secure_and_lax`, `security/test_cookies.py::test_csrf_cookie_is_readable_by_the_page_but_secure_and_lax`, `security/test_cookies.py::test_cookie_secure_is_on_by_default` |
| SR-06 | Another site makes a signed-in browser change something (cross-site request forgery). | Every unsafe method needs an `X-CSRF-Token` header equal to the CSRF cookie, compared in constant time. Login takes JSON only, so a plain cross-site form can't sign anyone in. | `security/test_csrf.py::test_a_bad_csrf_header_is_refused`, `security/test_csrf.py::test_a_header_without_its_cookie_is_refused`, `test_auth.py::test_login_without_a_json_content_type_gets_the_generic_401` |
| SR-07 | Password guessing against one account, or many tries from one address. | Lockout after 5 failures in 15 minutes; 10 tries per minute per IP; failures counted under a row lock so parallel tries can't slip past. | `test_auth.py::test_lockout_engages_on_the_fifth_failure`, `test_auth.py::test_rate_limit_returns_429_per_ip_and_writes_no_row`, `security/test_mysql_races.py::test_parallel_login_failures_are_all_counted` (MySQL) |
| SR-08 | Learning which emails have accounts, or which are locked. | One generic login failure for every cause, given only after the password check; unknown users are checked against a dummy hash so timing matches. | `test_auth.py::test_wrong_password_and_unknown_user_get_the_same_response`, `test_auth.py::test_a_locked_account_and_an_unknown_email_get_identical_responses` |
| SR-09 | SQL injection through list filters, paging or login. | All queries are built by SQLAlchemy with bound parameters; paging values are typed and bounded (offset at most 1,000,000,000). | `security/test_sqli.py::test_injection_in_an_audit_filter_matches_nothing`, `security/test_sqli.py::test_injection_in_paging_is_422`, `security/test_sqli.py::test_injection_in_the_login_email_is_a_plain_failure` |
| SR-10 | Oversized requests tie up memory or argon2 time. | Bodies over `max_body_bytes` (64 KiB) get 413, counted by bytes actually received, so a missing or false `Content-Length` doesn't help; passwords over 256 characters are refused before hashing. | `security/test_payloads.py::test_an_oversized_body_is_413`, `security/test_payloads.py::test_an_oversized_body_without_content_length_is_413`, `security/test_payloads.py::test_an_oversized_body_with_a_false_content_length_is_413`, `test_auth.py::test_too_long_password_is_refused_with_the_generic_message` |
| SR-11 | The system is left with no active admin, so nobody can manage users. | A role change that would remove the last active admin is refused (409); the admin rows are locked until commit, so two admins demoting each other can't both succeed. A deadlock is a clean 409. | `test_authz.py::test_a_change_that_would_leave_no_active_admin_is_409`, `security/test_mysql_races.py::test_two_admins_demoting_each_other_leave_one_admin` (MySQL), `security/test_headers.py::test_409_lock_conflict` |
| SR-12 | A person approves their own change, such as their own role or unlock. | Nobody can change their own role or unlock their own account; `not_own_submission` is the hook for later decisions. | `test_authz.py::test_nobody_can_change_their_own_role`, `test_authz.py::test_an_admin_cannot_unlock_their_own_account`, `test_authz.py::test_not_own_submission` |
| SR-13 | The browser guesses a response's type, or the full URL leaks to other sites. | Every API response, errors included, carries `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. | `security/test_headers.py::test_401`, `security/test_headers.py::test_413`, `security/test_headers.py::test_422` (and the other tests in that file) |
| SR-14 | Injected script runs in the web app. | Vue escapes text by default. The built `index.html` carries a Content Security Policy: scripts only from the app's own files, no inline script, no eval, no plugins. | `apps/web/src/__tests__/csp.spec.ts` (all tests) |
| SR-15 | Passwords, tokens or keys end up in logs, responses, audit rows or error messages. | argon2id hashes only; secrets are `repr=False` and hidden in validation errors; audit rows hold event, reason, IP and user id only. | `test_auth.py::test_no_password_or_token_in_logs_or_responses`, `test_auth.py::test_each_event_type_writes_one_audit_row_without_secrets`, `test_authz.py::test_no_secret_in_the_role_change_audit_row`, `test_config.py::test_repr_hides_secrets`, `test_config.py::test_validation_error_does_not_echo_secrets` |
| SR-16 | Someone changes or deletes audit rows to hide what happened. | The audit table is append-only in the ORM: updates, deletes and bulk changes through any session raise. See the gap below. | `test_audit.py::test_an_orm_update_of_an_audit_row_raises`, `test_audit.py::test_an_orm_delete_of_an_audit_row_raises`, `test_audit.py::test_bulk_update_or_delete_of_audit_rows_raises` |
| SR-17 | API documentation in production maps the attack surface for an outsider. | `/docs`, `/redoc` and `/openapi.json` are served only when `APP_ENV` is dev or test. | `test_authz.py::test_docs_are_served_only_in_dev_and_test` |
| SR-18 | Database traffic is read or altered in transit to the hosted database. | TLS with the server verified against `DATABASE_CA_CERT`. | `test_db.py::test_tls_with_a_ca_path`, `test_db.py::test_dbcheck_reports_a_tls_verification_failure` |
| SR-19 | A dependency with a known weakness ships unnoticed. | The `Dependency audit` CI job runs `pip-audit` and `npm audit --omit=dev` on every pull request. Report-only for now; a person reads it. | CI job `Dependency audit` (no unit test) |

## Threats from using AI

The agents arrive in P3. Until then these are design rules, written down now so they become risk
register entries and so the first agent is built and tested against them. No agent code exists
yet, so most have no test; each line says which test the first agent item must add.

| Id | Threat | Mitigation | Tests |
| --- | --- | --- | --- |
| SR-20 | Prompt injection: text in a client brief, document or risk note tells an agent to do something else. | Input is passed as data, never as instructions; every call uses a strict JSON schema and anything outside it is refused; an agent's output is only ever a proposal (R1), so injected text can't take a decision. | None yet. The first agent item adds a test that hostile input text produces at most a schema-valid proposal. |
| SR-21 | An agent acts beyond its limits: approves a gate, accepts a risk, commits a price, sends a document or authorises a project. | Agents hold no role in the permissions table and no decision action; limits are enforced in code, never trusted from the model (R4); a person decides and the audit row records who. | Partly: `test_authz.py::test_permissions_table_is_exactly_the_agreed_one`. The first agent item adds a test that its run can't call a decision route. |
| SR-22 | Secrets or personal data are sent to the model. | The API key is `repr=False` and never logged; prompts are built from named fields, never whole records or settings. | Partly: `test_config.py::test_repr_hides_secrets`. The first agent item adds a test that a prompt never contains a secret or password hash. |
| SR-23 | Runaway cost: an agent loops or is triggered too often. | Every run is recorded with model, turns, tokens and cost in `pmo_audit`; build runs stop for the day at the daily cap; turn limits per run. | None in the app yet. The first agent item adds an in-app cap and its test. |
| SR-24 | A confident but wrong agent answer is taken as fact. | Output is a proposal that a person edits and releases (R1); risks that don't fit a number go to a person (R9). | None yet; covered by each agent item's acceptance tests. |

## Known gaps

- **Audit rows can be changed with raw SQL.** The append-only guard (`app/models/audit.py`) works
  in the ORM: changing or deleting a loaded row and bulk `update()`/`delete()` through any
  `Session` all raise. Raw SQL on a plain connection, or dropping the table, goes around it; the
  MySQL race tests do exactly that to empty their throwaway database. Database triggers that refuse
  `UPDATE` and `DELETE` on `pmo_audit` would close the gap, but creating triggers may need
  privileges on the hosted database that have not been checked. Until then, only the app's own
  database user should reach the database, and nobody runs hand-written SQL against
  `pmo_audit`.
- **The CSP is a `<meta>` tag.** A meta policy can't set `frame-ancestors` (so it can't stop
  framing) or reporting. When a production web server exists, the same policy should be sent as a
  `Content-Security-Policy` response header, with `frame-ancestors 'none'` added. `style-src`
  allows `'unsafe-inline'` because Vuetify writes its theme and some component styles inline;
  `script-src` stays strict. Vitest checks the policy text only: whether the built app works
  under it still needs a check in a real browser.
- **A crash bypasses the security headers.** An unhandled exception is answered by the server's
  own 500 handler, outside the headers middleware, so that one response lacks the two headers.
- **The login rate limit is per process.** It lives in memory, so several API processes each
  count on their own, and a restart forgets every count (`app/ratelimit.py`).
- **Validation errors echo the refused value.** A 422 shows the value that failed (never on
  login, which has its own generic answer). No route takes a secret in a validated field today.

## Security reviews (R6)

**When.**

1. At the end of every milestone, before the next one starts.
2. Every quarter, even when nothing has shipped.
3. After any change to login, sessions, cookies, CSRF, permissions, the audit table or how the
   app reaches the database, in the same pull request.
4. When the `Dependency audit` job reports a high or critical finding.

**How.** The repository owner runs the review, with the reviewer agent's comment as input:

1. Confirm the `API (security tests)` check and the MySQL race tests passed on `main`.
2. Walk the threat tables above: is each mitigation still true, and does each named test exist
   and still pass? Add a row for anything new, especially each new route or agent.
3. Read the latest `Dependency audit` output and decide what to upgrade.
4. Re-check the known gaps and close any that can now be closed.
5. Add a dated line to the log below with the outcome and any follow-up issues.

**Log.**

| Date | Trigger | Outcome |
| --- | --- | --- |
| 2026-11-02 | Item 2.4 (first version of this document) | Threats SR-01 to SR-24 written down; tests added for SR-02, SR-03, SR-05, SR-06, SR-09, SR-10, SR-11, SR-13, SR-14. |

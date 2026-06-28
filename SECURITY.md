# Security Policy

For3s OS is a self-hosted AI agent that handles **sensitive material**: encrypted
API keys, a master encryption key (KEK), an immutable audit chain, and the ability
to execute actions and manage containers. We take security seriously and appreciate
responsible disclosure.

---

## Reporting a Vulnerability

**Please do NOT open a public issue for security vulnerabilities.** Public disclosure
before a fix puts every user at risk.

Instead, report privately:

- **Email:** brayan002150@gmail.com
- Use the subject line: `[SECURITY] For3s OS — <short description>`
- If possible, encrypt sensitive details or ask for a secure channel first.

Please include:

1. A clear description of the vulnerability and its impact.
2. Steps to reproduce (proof of concept if available).
3. The affected version/commit and your environment (OS, Docker version).
4. Any suggested remediation.

### What to expect

- **Acknowledgement** within 72 hours.
- An initial assessment and severity rating within 7 days.
- We will keep you informed of the fix progress and coordinate a disclosure date.
- With your permission, we will **credit you** in the release notes once the issue
  is resolved.

### Responsible disclosure

- Do **not** publicly disclose the issue until a fix is released and a reasonable
  coordination window has passed.
- Do **not** access, modify, or exfiltrate data that does not belong to you.
- Do **not** run denial-of-service tests against shared infrastructure.
- Good-faith research under this policy will not be pursued legally.

---

## Scope

### In scope (we want to hear about these)

For3s OS handles secrets and runs untrusted-ish workloads, so we especially care about:

- **KEK / encryption bypass** — any way to read secrets in plaintext, leak the master
  key (`~/.for3s/master.key`), or decrypt the `secrets` table without authorization.
- **Secret/credential leakage** — API keys (Claude/Telegram), GitHub PAT, or tokens
  exposed in logs, errors, audit entries, or to other users/workspaces.
- **Audit chain tampering** — any way to alter or forge the immutable audit log.
- **Container escape / privilege escalation** — escaping the agent container or
  abusing container management to affect the host.
- **Prompt injection → tool execution** — making the agent run unintended/dangerous
  actions (this is a known HIGH-priority threat class for AI agents).
- **Cross-user / cross-workspace isolation breaks** — one user reading another's
  private memory, skills, or secrets in a multi-user deployment.
- **Bypass of the skill governor** — getting an auto-generated skill with dangerous
  patterns past the scanner, or disabling the kill switch without authorization.
- **SQL injection, SSRF, RCE** in any component.

### Out of scope

- Issues that require physical access to the host or an already-compromised machine.
- Vulnerabilities in third-party dependencies (report those upstream; tell us so we
  can bump the version).
- Self-inflicted misconfiguration (e.g. committing your own `.env`).
- Denial of service against your own instance.
- Missing security headers on pages that don't exist (For3s is self-hosted, not a
  hosted web app).

---

## Security Architecture (how For3s protects you)

For context, For3s is built with defense in depth:

- **Encrypted secrets at rest** — all credentials are encrypted in the database with
  a key derived from the master KEK. The **KEK is kept on the host, outside the
  database and containers**; stealing the database alone does not reveal any secret.
- **No plaintext exposure** — the system encrypts/decrypts secrets in memory only;
  they are never logged.
- **Immutable audit chain** — every sensitive action is recorded in a tamper-evident,
  append-only log (no UPDATE/DELETE).
- **Input/Output guarding** — incoming requests pass through a scanner (heuristics +
  classifier) and outgoing actions go through an output gate.
- **Governed autonomy** — self-generated skills pass through a security scanner and a
  kill switch before they can run; nothing auto-applies without the owner's approval.
- **Self-hosted by design** — your data and keys never leave your machine.

---

## Supported Versions

For3s OS is in active pre-release development. Security fixes are applied to the
latest version on the `main` branch. There is no long-term support for older
pre-release versions yet.

| Version | Supported          |
| ------- | ------------------ |
| latest (`main`) | ✅ |
| older pre-releases | ❌ |

---

Thank you for helping keep For3s OS and its users safe.

— Brian Jovany López Pérez, author & maintainer

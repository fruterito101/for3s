# Changelog

All notable changes to For3s OS are documented here. For3s is built in **milestones**
(H1, H2, …); the version reflects that. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Versions are `0.x` (alpha / pre-release).

## [0.12.0] — 2026-06-26 — H10 PLANEA (metacognition)
- Metacognition: the agent measures its own confidence before asserting.
- "Knows when it doesn't know": if unsure, it says so or asks instead of inventing.
- Confidence scoring from real signals (its own phrasing + history) + audit.
- Low-confidence answers are flagged as tentative.

## [0.11.0] — 2026-06-26 — H9 SUEÑA (DMN / works while idle)
- DMN: works on its own when you're inactive (maintenance + self-improvement).
- Housekeeping: pre-computes embeddings, consolidates memory, watches its own quality.
- Generative tasks (governed): detects patterns & hypotheses and proposes them to you.
- `/dmn` command (status/on/off/run/proposals/roi) — owner only.
- Per-task ROI: measures what each background task contributes.

## [0.10.0] — 2026-06-25 — H12 APRENDE (learning engine)
- `/aprende`: distills a reusable skill from what you just worked on.
- Self-improvement: after complex tasks it proposes skills (awaiting your approval).
- Every new skill passes the governor (security scanner) before being saved.
- Nightly curation: unused auto-skills are archived on their own (recoverable).

## [0.9.0] — 2026-06-25 — H11 GOVERNOR (the brake)
- Skills (reusable recipes): For3s can hold and apply SKILL.md files (`/skills`).
- Skill governor: scans every new skill for dangerous patterns.
- Auto-generation kill switch (`/autogen on|off|status`), off by default.
- Brakes: daily auto-creation cap, no duplicates, active-skill cap.
- Skills created by a person are untouchable by the system (provenance).

## [0.8.x] — 2026-06-23 — H8 EQUIPO (team / multi-user)
- Multi-agent teamwork: 5 specialists in parallel + synthesis (2 families).
- Multi-user: several people share one agent, with roles and a `/invitar` door.
- Hybrid memory: private per person + shared team knowledge.
- Approval gate for sensitive actions; per-user threads; live progress + token cost.

## [0.7.0] — 2026-06-23 — H7 (partial) /model
- `/model` command: pick the AI model (Haiku/Sonnet/Opus).

## [0.6.0] — 2026-06-20 — H6 SE CUIDA (self-maintenance)
- Maintains itself at night: backup + consolidation (CLS) + forgetting (Microglia).
- Memory organizes and improves itself while idle.

## [0.5.0] — 2026-06-20 — H5 MEMORIA REAL (real memory)
- Semantic memory: remembers by meaning across the whole history.
- Knowledge Graph (concepts, repos, issues) populated from GitHub reads.

## [0.4.0] — 2026-06-19 — MVP (H1–H4)
- Chat with persistent memory (Telegram + CLI).
- GitHub repo analysis + safe write tools with confirmation.
- Multimodal (images/PDF/Word/Excel) + web fetch + KEK encryption + audit chain.

[unreleased]: distribution / pre-testers phase — containerization, one-line installer,
clean public repo, AGPL-3.0 licensing.

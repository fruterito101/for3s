# Contributing to For3s OS

Thanks for your interest in For3s OS! This project is in active pre-release
development. Right now the most valuable contribution is **testing and feedback**.

## Found a security issue?

**Do not open a public issue.** See [SECURITY.md](SECURITY.md) and report it privately.

## I'm a tester

Please read [TESTING.md](TESTING.md) — it tells you what to try and how to report.
Use the **Tester feedback** or **Bug report** issue templates.

## Reporting bugs / requesting features

Open an issue with the matching template. One issue = one topic. Include your OS,
Docker version, and steps to reproduce.

## Submitting code (Pull Requests)

1. Fork the repo and create a branch from `main`.
2. Make your change. Keep it focused.
3. Before pushing, make sure these pass:
   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run pytest -q
   ```
4. Open a PR using the template. Describe what and why.

### Ground rules

- **Never commit secrets** (`.env`, keys, tokens) or personal data.
- Match the surrounding code style (the linter enforces it).
- New behavior should come with a test when practical.

## License of contributions

By contributing, you agree that your contributions are licensed under the project's
**AGPL-3.0** license. The copyright holder (Brian Jovany López Pérez) may also offer
the project under a separate commercial license.

— Thank you for helping make For3s OS better.

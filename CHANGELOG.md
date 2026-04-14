# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project aims for [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.1.0] — 2026-04-15

First tagged release. Everything up to this point was "works on
my machine"; 0.1.0 is the first version with a published contract,
a quality gate, and CI.

### Added

- Telegram bot (aiogram 3) with commands: `/start`, `/help`,
  `/search`, `/compare`, `/cart`, `/total`, `/history`, `/clear`.
  Plain text (not prefixed with `/`) is treated as a search query.
- Service adapters: VkusVill (HTML), Auchan (JSON REST), Metro
  (GraphQL). See [SPEC §2](docs/SPEC.md#2-adapter-contract) for the
  shared contract.
- Cross-service fuzzy matching via rapidfuzz token-set ratio plus
  quantity extraction (volume / mass / pieces); strict thresholds
  (80 / 12%) to avoid misleading users.
- SQLite-backed user carts and search history via async SQLAlchemy;
  Postgres supported by swapping `DATABASE_URL`.
- Retry helper (`net.fetch_with_retry`) with exponential backoff on
  transient HTTP failures; stable `FetchError.tag` for humanized
  error messages.
- TTL LRU response cache (5 min) and per-display search cache (30
  min) used by callback buttons to freeze prices at click time.
- Deployment: `Dockerfile` + `docker-compose.yml` (non-root user,
  named volume, log rotation) and `deploy/zakupator.service`
  (systemd unit, hardened).
- Documentation: [README](README.md), [SPEC](docs/SPEC.md),
  [ARCHITECTURE](docs/ARCHITECTURE.md),
  [ADAPTERS](docs/ADAPTERS.md), [recon](docs/recon.md).
- Quality gate: `ruff` (lint + format), `mypy --strict`, `bandit`,
  `semgrep` (p/python + p/security-audit + p/secrets), `pip-audit`,
  and `pytest` (127 offline tests). Wrapped in `scripts/check.sh all`.
- CI: GitHub Actions workflow mirroring the local gate across four
  parallel jobs (lint / type / sast / test).
- Pre-commit hooks for trailing whitespace, YAML/TOML validity,
  large-file guard, ruff, mypy, bandit.

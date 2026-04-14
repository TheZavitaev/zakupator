#!/usr/bin/env bash
#
# Run the full local quality gate. Mirrors CI so you can catch issues
# before pushing. Every step must exit zero; the script aborts on the
# first failure via `set -e`.
#
# Usage:
#   scripts/check.sh                # everything
#   scripts/check.sh lint           # just ruff
#   scripts/check.sh type           # just mypy
#   scripts/check.sh sast           # bandit + semgrep + pip-audit
#   scripts/check.sh test           # just pytest
#
set -euo pipefail

cd "$(dirname "$0")/.."

# macOS / iCloud keeps marking files inside .venv with UF_HIDDEN,
# which silently breaks the editable install's .pth file. Clear it
# on every run — no-op on non-macOS.
if [[ "$OSTYPE" == darwin* ]] && [[ -d .venv ]]; then
    chflags -R nohidden .venv 2>/dev/null || true
fi

PY=.venv/bin/python
STEP="${1:-all}"

run_lint() {
    echo "==> ruff check"
    .venv/bin/ruff check src tests scripts
    echo "==> ruff format --check"
    .venv/bin/ruff format --check src tests scripts
}

run_type() {
    echo "==> mypy --strict"
    .venv/bin/mypy --strict src/zakupator
}

run_sast() {
    echo "==> bandit"
    .venv/bin/bandit -r src/zakupator -q -c pyproject.toml
    echo "==> semgrep"
    .venv/bin/semgrep \
        --config=p/python \
        --config=p/security-audit \
        --config=p/secrets \
        --error --metrics=off --quiet \
        src/zakupator
    echo "==> pip-audit"
    .venv/bin/pip-audit --skip-editable
}

run_test() {
    echo "==> pytest"
    .venv/bin/pytest -q
}

case "$STEP" in
    lint) run_lint ;;
    type) run_type ;;
    sast) run_sast ;;
    test) run_test ;;
    all)
        run_lint
        run_type
        run_sast
        run_test
        echo ""
        echo "✓ all checks passed"
        ;;
    *)
        echo "usage: $0 [lint|type|sast|test|all]" >&2
        exit 2
        ;;
esac

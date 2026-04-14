#!/usr/bin/env bash
# Wrapper that deals with macOS putting UF_HIDDEN on the editable-install
# .pth file (happens whenever the containing directory is touched by
# iCloud Drive / Finder / certain backup tools — the site.py loader then
# silently skips the file and `import zakupator` fails).
#
# Clearing the flag is a no-op if everything's already fine, so it's safe
# to run every time.

set -e

cd "$(dirname "$0")"

if [ -d .venv ]; then
  chflags -R nohidden .venv 2>/dev/null || true
fi

exec .venv/bin/python -m zakupator "$@"

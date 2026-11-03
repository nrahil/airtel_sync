#!/usr/bin/env bash
# Regenerate hash-pinned lock files from pyproject.toml.
# Locks target Linux / CPython 3.11 (CI runners and the Docker image), so they are
# generated inside the same base image. Run from the repo root:  bash backend/scripts/lock.sh
set -euo pipefail
cd "$(dirname "$0")/.."
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W 2>/dev/null || pwd):/src" -w /src python:3.11-bookworm bash -c '
  set -e
  pip install -q --root-user-action=ignore "pip-tools>=7.4,<8"
  common="--quiet --generate-hashes --strip-extras --allow-unsafe --resolver=backtracking --no-emit-index-url"
  pip-compile $common --output-file requirements.lock pyproject.toml
  pip-compile $common --extra dev --output-file requirements-dev.lock pyproject.toml
'

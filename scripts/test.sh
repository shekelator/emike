#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate
ruff check src tests
mypy src
pytest -q

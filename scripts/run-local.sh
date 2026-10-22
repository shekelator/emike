#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate
emike --keyboard --config config/assistant.yaml

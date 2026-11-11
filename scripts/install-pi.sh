#!/usr/bin/env bash
# Raspberry Pi install. Nova Sonic requires Python >= 3.12, but Raspberry Pi OS
# (Bookworm) ships 3.11, so the venv is created with uv-managed Python 3.12.
set -euo pipefail

if [ "$(uname -m)" != "aarch64" ]; then
  echo "Note: this script targets Raspberry Pi OS 64-bit (aarch64)."
fi

sudo apt update
sudo apt install -y libportaudio2 alsa-utils

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv venv --python 3.12 --clear .venv 2>/dev/null || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .

source .venv/bin/activate

if command -v systemctl >/dev/null 2>&1; then
  echo "Installing and enabling emike systemd user service..."
  bash "$(dirname "$0")/install-service.sh"
else
  echo "systemd not detected; skipping service installation."
  echo "On Raspberry Pi, run: bash scripts/install-service.sh"
fi

echo "Install complete."
echo "Next:"
echo "  1. bash scripts/aws/setup-aws.sh   (IAM user + model access check)"
echo "  2. copy config/assistant.example.yaml to config/assistant.yaml"
echo "  3. copy .env.example to .env and add AWS credentials"

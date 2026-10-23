#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y \
  python3-venv \
  python3-pip \
  libportaudio2 \
  alsa-utils \
  portaudio19-dev

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .[dev,tts]

echo "Install complete."
echo "Next: copy config/assistant.example.yaml to config/assistant.yaml"

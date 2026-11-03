#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_name="emike"
service_src="$repo_root/systemd/emike.service"
service_dir="$HOME/.config/systemd/user"
service_target="$service_dir/${service_name}.service"

if [[ ! -f "$service_src" ]]; then
  echo "Service definition not found at $service_src" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is not available in this environment. Service installation requires systemctl."
  exit 1
fi

mkdir -p "$service_dir"
install -m 0644 "$service_src" "$service_target"

systemctl --user daemon-reload
systemctl --user enable --now "$service_name"
sudo loginctl enable-linger "$USER"

systemctl --user status "$service_name" --no-pager || true

echo "emike service installed and started."
echo "Useful commands:"
echo "  systemctl --user status emike"
echo "  journalctl --user -u emike -f"
echo "  systemctl --user restart emike"

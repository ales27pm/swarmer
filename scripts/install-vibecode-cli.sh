#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${VIBECODE_API_KEY:-}" ]]; then
  echo "VIBECODE_API_KEY is not set." >&2
  exit 1
fi

os="$(uname -s)"
arch="$(uname -m)"
version="v0.1.0"
base="https://github.com/vibecode/vibecode-cli/releases/download/${version}"

case "${os}/${arch}" in
  Linux/x86_64) asset="vibecode-cli-linux-amd64" ;;
  Linux/aarch64|Linux/arm64) asset="vibecode-cli-linux-arm64" ;;
  Darwin/arm64) asset="vibecode-cli-darwin-arm64" ;;
  Darwin/x86_64) asset="vibecode-cli-darwin-amd64" ;;
  *)
    echo "Unsupported platform: ${os}/${arch}" >&2
    exit 1
    ;;
esac

install_dir="${HOME}/.local/bin"
mkdir -p "${install_dir}"

curl --fail --silent --show-error --location \
  "${base}/${asset}" \
  --output "${install_dir}/vibecode-cli"
chmod +x "${install_dir}/vibecode-cli"

case ":${PATH}:" in
  *":${install_dir}:"*) ;;
  *) export PATH="${install_dir}:${PATH}" ;;
esac

vibecode-cli user

mkdir -p codex/skills
vibecode-cli skill > codex/skills/SKILL.md

echo
printf 'Installed: %s\n' "$(command -v vibecode-cli)"
echo "Skill written to codex/skills/SKILL.md"

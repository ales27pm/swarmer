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
  Linux/x86_64)
    asset="vibecode-cli-linux-amd64"
    expected_sha256="c53620eafe3372d42d168932c802f4585b2fe465c654ba7019a3ea9438d8c9e6"
    ;;
  Linux/aarch64|Linux/arm64)
    asset="vibecode-cli-linux-arm64"
    expected_sha256="bbeb79e1b1adb141d1e350f673418cb7cfb1b0b825d4442d393edc5e8f76af24"
    ;;
  Darwin/arm64)
    asset="vibecode-cli-darwin-arm64"
    expected_sha256="95ba644e62afc92524999d7e23bc69b9a5a3eaa08ff5b162ddd031ebf3c1aa97"
    ;;
  Darwin/x86_64)
    asset="vibecode-cli-darwin-amd64"
    expected_sha256="a426633eb1be7a8a8c66ef5daf722f7d3b2f24d2ef7c4d3c080c13bbd42658d1"
    ;;
  *)
    echo "Unsupported platform: ${os}/${arch}" >&2
    exit 1
    ;;
esac

install_dir="${HOME}/.local/bin"
mkdir -p "${install_dir}"
download_dir="$(mktemp -d "${TMPDIR:-/tmp}/vibecode-cli.XXXXXX")"
trap 'rm -f "${download_dir}/${asset}"; rmdir "${download_dir}" 2>/dev/null || true' EXIT
download_path="${download_dir}/${asset}"

curl --fail --silent --show-error --location \
  "${base}/${asset}" \
  --output "${download_path}"

if command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "${download_path}" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "${download_path}" | awk '{print $1}')"
else
  echo "A SHA-256 verification utility is required." >&2
  exit 1
fi
if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
  echo "Vibecode CLI checksum verification failed; refusing installation." >&2
  exit 1
fi

chmod 0755 "${download_path}"
mv "${download_path}" "${install_dir}/vibecode-cli"

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

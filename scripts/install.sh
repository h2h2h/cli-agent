#!/usr/bin/env bash
# Install cli-agent as a global command, usable from any directory
# (like opencode / pi). Requires uv and Python >= 3.11.
#
#   ./scripts/install.sh            # editable install from this checkout
#   ./scripts/install.sh --release  # non-editable install
#
# Uninstall with: uv tool uninstall cli-agent
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
  "")
    mode="editable"
    ;;
  "--release")
    mode="release"
    ;;
  *)
    echo "usage: $0 [--release]" >&2
    exit 2
    ;;
esac

command -v uv >/dev/null 2>&1 || {
  echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
}

# 1. Per-user configuration home, created once. cli-agent reads
#    ~/.cli-agent/.env on startup; real environment variables still win.
config_dir="${HOME}/.cli-agent"
config_file="${config_dir}/.env"
if [[ ! -f "${config_file}" ]]; then
  mkdir -p "${config_dir}"
  cp cli-agent.env.example "${config_file}"
  chmod 600 "${config_file}"
  echo "created ${config_file}"
  echo "  edit it and set CLI_AGENT_MODEL, CLI_AGENT_BASE_URL, CLI_AGENT_API_KEY"
else
  echo "using existing ${config_file}"
fi

# 2. Install the console script into uv's tool bin (on PATH from any directory).
if [[ "${mode}" == "editable" ]]; then
  uv tool install --editable .
else
  uv tool install .
fi

# 3. Make uv's tool bin directory (e.g. ~/.local/bin) available to new
#    shells. Idempotent; no-op when it is already on the PATH.
if ! uv tool update-shell >/dev/null 2>&1; then
  echo "note: run 'uv tool update-shell' to add ~/.local/bin to your PATH"
fi

# 4. The current shell still has the old PATH (it was started before the
#    fix), so tell the user exactly what to do instead of leaving uv's
#    "not on your PATH" warning unexplained.
tool_bin="${UV_TOOL_BIN_DIR:-${HOME}/.local/bin}"
case ":${PATH}:" in
  *":${tool_bin}:"*)
    ;;
  *)
    echo "note: ${tool_bin} is not on this shell's PATH yet."
    echo "  Restart your terminal, or run now:"
    echo "  export PATH=\"${tool_bin}:\$PATH\""
    ;;
esac

echo
echo "installed. Restart your terminal, then run from any directory:"
echo "  cli-agent"
echo "Uninstall with:"
echo "  uv tool uninstall cli-agent"

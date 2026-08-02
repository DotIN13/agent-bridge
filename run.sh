#!/usr/bin/env bash
# Launch the agent-bridge gateway. Run inside a persistent tmux on the login node:
#   ssh midway5              # one Duo push
#   tmux new -s gw
#   cd /project/jevans/tzhang3/agent-bridge && ./run.sh
set -euo pipefail

cd "$(dirname "$0")"

# claude lives in ~/.local/bin, which non-login shells may miss.
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"

CONFIG="${AGENT_BRIDGE_CONFIG:-config.toml}"
if [[ ! -f "$CONFIG" && -f config.example.toml ]]; then
    echo "no $CONFIG found; copying from config.example.toml" >&2
    cp config.example.toml "$CONFIG"
fi

exec python3 -m gateway --config "$CONFIG"

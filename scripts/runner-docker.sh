#!/usr/bin/env bash
# GPU model server for the memory agent with Docker, in one line. Run as root on the GPU machine:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/scripts/runner-docker.sh)"
# It downloads the model runner, looks at your graphics cards (Vulkan by default, CUDA or ROCm if you pick them), pulls the llama.cpp image, starts the runner as a service and prints the server
# address, API key and runner token for Settings > Servers. Options go after the command (see runner-container.sh --help).
set -euo pipefail
if [[ -f "${BASH_SOURCE[0]:-}" && -f "$(dirname "${BASH_SOURCE[0]}")/runner-container.sh" ]]; then
    exec bash "$(dirname "${BASH_SOURCE[0]}")/runner-container.sh" --engine docker "$@"
fi
exec bash <(curl -fsSL "https://raw.githubusercontent.com/${MA_REPO:-Brooklyn377/Memory-Vault}/${MA_REF:-main}/scripts/runner-container.sh") --engine docker "$@"

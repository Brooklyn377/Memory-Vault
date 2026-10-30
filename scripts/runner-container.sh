#!/usr/bin/env bash
# GPU model server for the memory agent, on a Linux machine with Podman or Docker (use runner-podman.sh or
# runner-docker.sh; they call this with --engine). Run as root. It
#   1. downloads the model runner (model_runner.py) from this project,
#   2. pulls the llama.cpp server image for Vulkan (AMD, Intel and NVIDIA GPUs through /dev/dri),
#   3. sets the runner up to manage a llama-server container that it creates when you apply the first settings,
#   4. starts the runner as a systemd service (memory-agent-runner),
#   5. prints the three things the Servers card in Settings needs: the server address, the API key and the runner token.
# Run with --help for the options. Re-running keeps the models folder and makes a new runner token and API key.
set -euo pipefail

REPO="${MA_REPO:-Brooklyn377/Memory-Vault}"   # GitHub owner/name the runner is downloaded from
REF="${MA_REF:-main}"                        # branch or tag
IMAGE="ghcr.io/ggml-org/llama.cpp:server-vulkan"
ENGINE=""; MODELS="/srv/llama-models"; DIR="/opt/memory-agent-runner"; NAME="llama-server"
LLAMA_PORT=8080; RUNNER_PORT=8090; DRY=0; UPDATE=0

usage() {
    cat <<EOF
Usage: runner-container.sh --engine podman|docker [options]

  --engine E        podman or docker (required here; runner-podman.sh and runner-docker.sh set it)
  --models DIR      where the model files are kept (default: $MODELS)
  --llama-port N    llama-server's port on this machine (default: $LLAMA_PORT)
  --runner-port N   the runner's port (default: $RUNNER_PORT)
  --image IMAGE     the llama.cpp server image (default: $IMAGE)
  --name NAME       the llama-server container's name (default: $NAME)
  --dir DIR         where the runner is installed (default: $DIR)
  --repo OWNER/NAME where the runner is downloaded from (default: $REPO)
  --ref REF         branch or tag (default: $REF)
  --update          only download the newest runner and restart it (keys, models and settings stay)
  --dry-run         print what would be done, change nothing
EOF
}

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() { if (( DRY )); then echo "+ $(printf '%q ' "$@")"; else "$@"; fi; }

while (( $# )); do
    case "$1" in
        --engine) ENGINE="$2"; shift ;;
        --models) MODELS="$2"; shift ;;
        --llama-port) LLAMA_PORT="$2"; shift ;;
        --runner-port) RUNNER_PORT="$2"; shift ;;
        --image) IMAGE="$2"; shift ;;
        --name) NAME="$2"; shift ;;
        --dir) DIR="$2"; shift ;;
        --repo) REPO="$2"; shift ;;
        --ref) REF="$2"; shift ;;
        --update) UPDATE=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option $1" ;;
    esac
    shift
done

[[ "$ENGINE" == podman || "$ENGINE" == docker ]] || die "--engine must be podman or docker"
[[ "$LLAMA_PORT" =~ ^[0-9]+$ && "$RUNNER_PORT" =~ ^[0-9]+$ ]] || die "the ports must be numbers"
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "the container name has odd characters"
[[ $EUID -eq 0 ]] || (( DRY )) || die "run as root (for example: sudo bash runner-$ENGINE.sh)"
have curl || die "curl is needed"
have python3 || die "python3 is needed (3.8 or newer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python3 3.8 or newer is needed"
(( DRY )) || have "$ENGINE" || die "$ENGINE is not installed"
(( DRY )) || have systemctl || die "this needs systemd (systemctl) to keep the runner running"

if (( UPDATE )); then
    say "Updating the runner"
    run curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/model_runner.py" -o "$DIR/model_runner.py"
    run chmod 755 "$DIR/model_runner.py"
    run systemctl restart memory-agent-runner
    say "Done: the runner is up to date; its keys, models and settings were kept"
    exit 0
fi

if ! (( DRY )) && have ss; then   # a runner or llama-server from before would answer in place of the new one
    for port in "$LLAMA_PORT" "$RUNNER_PORT"; do
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port\$"; then
            die "port $port is already in use on this machine; stop what uses it first (see: sudo ss -ltnp | grep :$port), or pass another port with --llama-port or --runner-port"
        fi
    done
fi

say "GPU"
setup=()
if [[ -d /dev/dri ]]; then
    setup+=(--device /dev/dri)
    echo "found /dev/dri: the model runs on the GPU through Vulkan"
else
    echo "warning: no /dev/dri on this machine, so llama-server would run on the CPU only." >&2
fi
render="$(ls /dev/dri/renderD* 2>/dev/null | head -n 1 || true)"
if [[ -n "$render" ]]; then
    gid="$(stat -c %g "$render")"
    setup+=(--group-add "$gid")   # the group that may open the GPU, joined by the container
    echo "GPU device $render, group $gid"
fi

say "Runner"
run install -d -m 755 "$DIR"
[[ -d "$MODELS" ]] || run install -d -m 755 "$MODELS"   # an existing folder is left as it is
run curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/model_runner.py" -o "$DIR/model_runner.py"
run chmod 755 "$DIR/model_runner.py"

say "llama.cpp image (a large download the first time)"
run "$ENGINE" pull "$IMAGE"

say "Setting the runner up"
key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
setup=(--setup --create --engine "$ENGINE" --image "$IMAGE" --models "$MODELS" --name "$NAME"
       --llama-port "$LLAMA_PORT" --port "$RUNNER_PORT" "${setup[@]}")
if (( DRY )); then
    printf '+ python3 %s %s --api-key <new key>\n' "$DIR/model_runner.py" "${setup[*]}"
    token="<new token>"
else
    out="$(python3 "$DIR/model_runner.py" "${setup[@]}" --api-key "$key")"
    token="$(printf '%s\n' "$out" | sed -n 's/^Token for the Settings page (shown once): //p')"
    [[ -n "$token" ]] || die "the runner did not print a token: $out"
fi

say "Service"
after="network-online.target"; requires=""
[[ $ENGINE == docker ]] && { after="$after docker.service"; requires="Requires=docker.service"; }
unit="[Unit]
Description=Memory agent model runner
After=$after
Wants=network-online.target
$requires

[Service]
ExecStart=$(command -v python3 || echo /usr/bin/python3) $DIR/model_runner.py
WorkingDirectory=$DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"
if (( DRY )); then
    printf '+ write /etc/systemd/system/memory-agent-runner.service:\n%s\n' "$unit"
else
    printf '%s\n' "$unit" > /etc/systemd/system/memory-agent-runner.service
fi
run systemctl daemon-reload
run systemctl enable --now memory-agent-runner
run systemctl restart memory-agent-runner

if ! (( DRY )); then
    for _ in $(seq 30); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$RUNNER_PORT/status" || true)"
        [[ "$code" == 401 || "$code" == 200 ]] && break
        sleep 1
    done
    [[ "$code" == 401 || "$code" == 200 ]] || die "the runner did not start; see: journalctl -u memory-agent-runner"
fi

addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
addr="${addr:-ADDRESS-OF-THIS-MACHINE}"
say "Done"
cat <<EOF
On the memory agent, open Settings > Servers > Add server and enter:
  Address:        http://$addr:$LLAMA_PORT
  API key:        $key
  Runner address: http://$addr:$RUNNER_PORT
  Runner token:   $token

Write these down: the key and the token are not shown again (the runner keeps them in $DIR/model_runner.json, root only).
Then open the server's "Models on that machine" on the same card, paste a Hugging Face .gguf link and press Download,
pick it under "Model on that server" on a model's card, and press Apply all. The runner creates the llama-server
container then. Keep ports $LLAMA_PORT and $RUNNER_PORT on your local network.
EOF

#!/usr/bin/env bash
# Runs INSIDE the container (install.sh pushes the code and calls this). Installs or updates the memory agent
# in the same layout as the original install:
#   /opt/memory-agent (code + venv)   /opt/llama.cpp   /opt/models   /etc/memory-agent   /var/lib/memory-agent
# Fresh install: writes config, env files, login and settings. --update: replaces code and packages only;
# config, login, settings, models and the vault are kept.
set -euo pipefail

SRC=""; UPDATE=0; DRY=0; SMALL=0; LLAMA_REF=""; VERSION=""; REPO=""; REF=""; GPU=""; NV_DRIVER=""; CUDA_PKG=""
declare -A URL=([qwen]="" [gpt]="" [cur]="")

APP=/opt/memory-agent
ETC=/etc/memory-agent
STATE=/var/lib/memory-agent
VAULT=/srv/memory-vault
MODELS=/opt/models
LLAMA=/opt/llama.cpp
LLAMA_REPO=https://github.com/ggml-org/llama.cpp
# Units enabled at boot. memory-publish needs a Gitea server (see README); memory-backup needs a script
# that is not part of this project. memory-bootstrap is started on demand.
# The model servers (llama-server, memory-reviewer, memory-curator-llm, memory-assistant-llm) are not enabled: the
# turn manager in memory-web starts them when something needs them and stops them when another model group has its
# turn (Settings > Model groups), so they never all start at boot on a machine that holds one model at a time.
ENABLE=(memory-web memory-importer memory-journal memory-agent
        memory-review-worker memory-chat memory-filechanges memory-curator memory-sweep)
MODEL_UNITS=(llama-server memory-reviewer memory-curator-llm memory-assistant-llm)

usage() {
    cat <<EOF
setup-container.sh --src DIR [--update] [--small] [--llama-ref REF] [--version SHA] [--repo OWNER/NAME --ref REF]
                   [--sweeper-url URL] [--reviewer-url URL] [--curator-url URL] [--gpu vulkan|cuda|rocm [--nv-driver V --cuda-pkg MAJOR-MINOR]] [--dry-run]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src) SRC="$2"; shift ;;
        --update) UPDATE=1 ;;
        --small) SMALL=1 ;;
        --llama-ref) LLAMA_REF="$2"; shift ;;
        --gpu) GPU="$2"; shift ;;
        --nv-driver) NV_DRIVER="$2"; shift ;;
        --cuda-pkg) CUDA_PKG="$2"; shift ;;
        --version) VERSION="$2"; shift ;;
        --repo) REPO="$2"; shift ;;
        --ref) REF="$2"; shift ;;
        --sweeper-url) URL[qwen]="$2"; shift ;;
        --reviewer-url) URL[gpt]="$2"; shift ;;
        --curator-url) URL[cur]="$2"; shift ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ -n "$SRC" && -f "$SRC/agent.py" ]] || { echo "--src must point at the unpacked code" >&2; exit 2; }

say() { printf '\n== %s\n' "$*"; }
run() {
    if (( DRY )); then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
# write_file PATH MODE: writes stdin to PATH (atomically), or shows it in dry-run mode.
write_file() {
    local path="$1" mode="$2" text
    text="$(cat)"
    if (( DRY )); then printf '+ write %s (mode %s):\n%s\n' "$path" "$mode" "$text"; return; fi
    install -m "$mode" /dev/null "$path.tmp"
    printf '%s\n' "$text" > "$path.tmp"
    mv "$path.tmp" "$path"
}

say "Packages"
run apt-get update -q
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    python3 python3-venv ca-certificates curl git cmake build-essential libcurl4-openssl-dev

say "llama.cpp"
# CPU or a GPU (vulkan, cuda or rocm): --gpu from the installer, else what was chosen before (Settings > Servers).
if [[ -n "$GPU" && "$GPU" != cpu ]]; then
    [[ "$GPU" == vulkan || "$GPU" == cuda || "$GPU" == rocm ]] || { echo "--gpu must be vulkan, cuda or rocm" >&2; exit 2; }
    if (( DRY )); then echo "+ write /etc/memory-agent/backend: $GPU"; else install -d -m 700 /etc/memory-agent; echo "$GPU" > /etc/memory-agent/backend; fi
    if [[ -n "$NV_DRIVER" ]]; then   # the host's driver version, kept for later switches to CUDA
        printf 'NV_DRIVER=%s\nCUDA_PKG=%s\n' "$NV_DRIVER" "$CUDA_PKG" | write_file /etc/memory-agent/nvidia 600
    fi
fi
BACKEND="$(cat /etc/memory-agent/backend 2>/dev/null || echo cpu)"
[[ -n "$GPU" && "$GPU" != cpu ]] && BACKEND="$GPU"
. "$SRC/scripts/backend-packages.sh"
[[ "$BACKEND" == cpu ]] || echo "Backend: GPU through $BACKEND"
backend_prepare "$BACKEND"
gpuopts=("${GPUOPTS[@]}")
flags="$(grep -o -w -E 'avx|avx2|avx512f|fma|f16c' /proc/cpuinfo | sort -u | tr '\n' ' ' || true)"
echo "CPU features: ${flags:-none of avx/avx2/avx512f/fma/f16c}. Building for this CPU (GGML_NATIVE)."
if [[ -x "$LLAMA/build/bin/llama-server" ]]; then
    echo "already built at $LLAMA, kept (delete $LLAMA/build to rebuild)"
else
    if [[ ! -d "$LLAMA/.git" ]]; then
        clone=(git clone --depth 1)
        [[ -n "$LLAMA_REF" ]] && clone+=(--branch "$LLAMA_REF")
        run "${clone[@]}" "$LLAMA_REPO" "$LLAMA"
    fi
    run cmake -S "$LLAMA" -B "$LLAMA/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON "${gpuopts[@]}"
    run cmake --build "$LLAMA/build" --target llama-server -j "$(nproc)"
fi

say "Code and Python packages"
run install -d -m 755 "$APP" "$APP/systemd" "$MODELS" "$VAULT"
run install -d -m 700 "$ETC" "$STATE"
for f in "$SRC"/*.py; do
    name="$(basename "$f")"
    run install -m 644 "$f" "$APP/$name"
done
for f in requirements.txt requirements.lock config.example.yaml; do
    run install -m 644 "$SRC/$f" "$APP/$f"
done
run install -m 644 "$SRC"/systemd/*.service "$APP/systemd/"
[[ -x "$APP/venv/bin/python" ]] || run python3 -m venv "$APP/venv"
run "$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.lock"
run "$APP/venv/bin/python" -m py_compile "$APP"/*.py
run install -m 755 "$SRC/scripts/memory-agent-update" /usr/local/bin/memory-agent-update
run install -m 755 "$SRC/scripts/memory-agent-backend" /usr/local/bin/memory-agent-backend
run install -D -m 644 "$SRC/scripts/backend-packages.sh" /usr/local/lib/memory-agent/backend-packages.sh
[[ -n "$VERSION" ]] && printf '%s\n' "$VERSION" | write_file "$APP/VERSION" 644
# Where memory-agent-update gets new code. Written once; edit it to follow another branch.
if [[ -n "$REPO" && ! -f "$ETC/source" ]]; then
    printf 'MA_REPO=%s\nMA_REF=%s\n' "$REPO" "${REF:-main}" | write_file "$ETC/source" 644
fi

if (( ! UPDATE )); then
    say "Models"
    declare -A FILE=()
    for role in qwen gpt cur; do
        u="${URL[$role]}"
        [[ -n "$u" ]] || continue
        name="$(basename "${u%%\?*}")"
        if [[ ! "$name" =~ ^[A-Za-z0-9._-]+\.gguf$ ]]; then
            echo "not a .gguf link, skipped: $u" >&2; continue
        fi
        FILE[$role]="$name"
        if [[ -s "$MODELS/$name" ]]; then echo "$name already present"; continue; fi
        echo "downloading $name"
        if ! run curl -fL --retry 3 -o "$MODELS/$name.part" "$u"; then
            rm -f "$MODELS/$name.part"; unset "FILE[$role]"
            echo "download of $name failed; pick that model later on the Settings page" >&2; continue
        fi
        if (( ! DRY )); then
            if [[ "$(head -c 4 "$MODELS/$name.part")" != "GGUF" ]]; then
                rm -f "$MODELS/$name.part"; echo "$name is not a GGUF file, removed" >&2; unset "FILE[$role]"; continue
            fi
            mv "$MODELS/$name.part" "$MODELS/$name"
        fi
    done

    say "Settings, config and login (fresh install only; existing files are kept)"
    threads="$(nproc)"
    if [[ ! -f "$STATE/settings.json" ]]; then
        # Only the values that differ from the defaults in settings.py; the rest come from there.
        args=("$threads" "$SMALL" "${FILE[qwen]:-}" "${FILE[gpt]:-}" "${FILE[cur]:-}")
        json="$(python3 - "${args[@]}" <<'PY'
import json, sys
threads, small, files = int(sys.argv[1]), sys.argv[2] == "1", dict(zip(("qwen", "gpt", "cur"), sys.argv[3:6]))
out = {}
for k in ("qwen", "gpt", "cur"):
    out[k] = {"threads": threads}
    if files[k]:
        out[k]["model_file"] = files[k]
out["pa"] = {"threads": threads}   # the Assistant: no model until you choose one
if small:
    out["cur"]["context"] = 16384  # the default 131072 is for the large curator model
print(json.dumps(out, indent=1))
PY
)"
        write_file "$STATE/settings.json" 600 <<<"$json"
    fi
    if [[ ! -f "$ETC/config.yaml" ]]; then
        sed -e "s#^  vault: .*#  vault: $VAULT#" "$SRC/config.example.yaml" | write_file "$ETC/config.yaml" 600
    fi
    if [[ ! -f "$ETC/web.env" ]]; then
        pw="(generated at install)"
        (( DRY )) || pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
        printf 'UI_USER=admin\nUI_PASSWORD=%s\n' "$pw" | write_file "$ETC/web.env" 600
    fi
    # memory-publish reads this file; it stays empty and the unit stays off until publishing is set up.
    [[ -f "$ETC/publish.env" ]] || printf 'GITEA_WRITE_TOKEN=\n' | write_file "$ETC/publish.env" 600
fi
# The llama env files hold what settings.json says, in the format applier.write_env writes. Written again on every
# update, from settings.json (the source of truth), so server arguments added in a newer version reach the servers
# when the updater restarts them; a model slot added in a newer version gets its file.
for pair in qwen:llama-qwen gpt:llama-gpt cur:llama-cur pa:llama-pa; do
    role="${pair%%:*}"; env="$ETC/${pair#*:}.env"
    if (( DRY )); then echo "+ write $env from settings.json"; continue; fi
    (cd "$APP" && "$APP/venv/bin/python" - "$role" <<'PY'
import sys, settings, serverflags
s = settings.get(sys.argv[1])
print(f"MODEL={s['model_file']}\nCTX={s['context']}\nTHREADS={s['threads']}\nPARALLEL={s['parallel']}\nEXTRA={serverflags.build(s)}")
PY
) | write_file "$env" 600
done

say "Services"
for f in "$SRC"/systemd/*.service; do
    [[ "$(basename "$f")" == memory-backup.service ]] && continue
    run install -m 644 "$f" /etc/systemd/system/
done
run systemctl daemon-reload
run systemctl enable "${ENABLE[@]/%/.service}"
# Installs from before model groups started the model servers at boot: they are started by the turn manager now.
run systemctl disable "${MODEL_UNITS[@]/%/.service}" || true
for unit in "${ENABLE[@]}"; do
    run systemctl restart "$unit.service"
done

say "Checking the web page"
if (( ! DRY )); then
    for _ in $(seq 30); do
        if curl -fs -o /dev/null http://127.0.0.1:8000/; then echo "web page is up"; exit 0; fi
        sleep 2
    done
    echo "the web page did not answer on port 8000; see: journalctl -u memory-web -n 50" >&2
    exit 1
fi

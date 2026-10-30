#!/usr/bin/env bash
# GPU model server for the memory agent, on a Proxmox host. Run as root in the Proxmox shell:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/scripts/runner-proxmox.sh)"
# It creates a Debian 12 LXC, passes one GPU of this host into it, builds llama.cpp for Vulkan inside, installs the
# model runner there as a service, and prints the server address, API key and runner token for Settings > Servers.
# Anything not given as an option is asked. The disk, memory and cores have no defaults: they are always asked.
# Options go after the command: bash -c "$(curl ...)" _ --disk 200 --memory 16384 --cores 8
set -euo pipefail

REPO="${MA_REPO:-Brooklyn377/Memory-Vault}"   # GitHub owner/name the runner is downloaded from
REF="${MA_REF:-main}"                        # branch or tag
CTID=""; HOSTNAME_=""; STORAGE=""; TSTORAGE="local"; DISK=""; CORES=""; MEMORY=""
BRIDGE=""; IP=""; GW=""; DNS=""; GPU=""; MODELS="/opt/models"; LLAMA_PORT=8080; RUNNER_PORT=8090
LLAMA_REF=""; UPDATE=""; YES=0; DRY=0

usage() {
    cat <<EOF
Usage: runner-proxmox.sh [options]

Container
  --ctid N              container ID (default: next free)
  --hostname NAME       default: llama-runner
  --storage NAME        storage for the container disk (default: the first that holds containers)
  --template-storage N  storage for the Debian template (default: local)
  --disk GB             disk size (always asked: the models live here)
  --cores N             CPU cores (always asked)
  --memory MB           memory (always asked)
  --bridge NAME         network bridge (default: vmbr0)
  --ip dhcp|CIDR        address, e.g. 192.168.1.60/24 (default: dhcp)
  --gw ADDRESS          gateway, needed with a fixed address
  --dns ADDRESS         DNS server, or "host" for the Proxmox host's (default: the gateway with a fixed address, else host)
  --gpu DEVICE          the GPU to pass in, e.g. /dev/dri/renderD128 (asked when this host has more than one)

Server
  --models DIR          where model files are kept inside the container (default: $MODELS)
  --llama-port N        llama-server's port (default: $LLAMA_PORT)
  --runner-port N       the runner's port (default: $RUNNER_PORT)
  --llama-ref REF       llama.cpp branch or tag to build (default: its newest code)

Other
  --update CTID         download the newest runner into an existing container and restart it (keys and models stay)
  --repo OWNER/NAME     where the runner is downloaded from (default: $REPO)
  --ref REF             branch or tag (default: $REF)
  --yes                 take the defaults instead of asking (not for disk, memory and cores)
  --dry-run             print what would be done, change nothing
EOF
}

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() { if (( DRY )); then echo "+ $(printf '%q ' "$@")"; else "$@"; fi; }

while (( $# )); do
    case "$1" in
        --ctid) CTID="$2"; shift ;;
        --hostname) HOSTNAME_="$2"; shift ;;
        --storage) STORAGE="$2"; shift ;;
        --template-storage) TSTORAGE="$2"; shift ;;
        --disk) DISK="$2"; shift ;;
        --cores) CORES="$2"; shift ;;
        --memory) MEMORY="$2"; shift ;;
        --bridge) BRIDGE="$2"; shift ;;
        --ip) IP="$2"; shift ;;
        --gw) GW="$2"; shift ;;
        --dns) DNS="$2"; shift ;;
        --gpu) GPU="$2"; shift ;;
        --models) MODELS="$2"; shift ;;
        --llama-port) LLAMA_PORT="$2"; shift ;;
        --runner-port) RUNNER_PORT="$2"; shift ;;
        --llama-ref) LLAMA_REF="$2"; shift ;;
        --update) UPDATE="$2"; shift ;;
        --repo) REPO="$2"; shift ;;
        --ref) REF="$2"; shift ;;
        --yes) YES=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option $1" ;;
    esac
    shift
done

[[ "$LLAMA_PORT" =~ ^[0-9]+$ && "$RUNNER_PORT" =~ ^[0-9]+$ ]] || die "the ports must be numbers"
[[ "$MODELS" =~ ^/[A-Za-z0-9_./-]*$ ]] || die "--models must be a folder path inside the container, such as /opt/models"
[[ $EUID -eq 0 ]] || (( DRY )) || die "run as root on the Proxmox host"
have curl || die "curl is needed"
(( DRY )) || have pct || die "pct not found: this must run on a Proxmox VE host"

# ask VAR "question" "default": keeps a value that was given; else asks (a blank answer takes the default), or takes the
# default with --yes. Questions are read from the terminal, so this also works when the script is piped from curl.
ask() {
    local var="$1" q="$2" def="${3:-}" ans
    [[ -n "${!var}" ]] && return 0
    if (( YES )) && [[ -n "$def" ]]; then printf -v "$var" '%s' "$def"; return 0; fi
    (( YES )) && die "--yes needs a default for: $q; give it as an option"
    [[ -r /dev/tty ]] || die "no terminal to ask on; give the options on the command line (see --help)"
    read -r -p "$q${def:+ [$def]}: " ans </dev/tty
    printf -v "$var" '%s' "${ans:-$def}"
    [[ -n "${!var}" ]] || die "an answer is needed"
}
ask_number() {
    local var="$1" q="$2" opt="$3"
    if [[ -z "${!var}" ]]; then
        (( YES )) && die "give $opt: it has no default"
        [[ -r /dev/tty ]] || die "no terminal to ask on; give $opt"
        read -r -p "$q: " "$var" </dev/tty
    fi
    [[ "${!var}" =~ ^[0-9]+$ && "${!var}" -gt 0 ]] || die "$opt must be a whole number above 0"
}

# --- --update: a newer runner in an existing container --------------------------------------------------------------
if [[ -n "$UPDATE" ]]; then
    say "Updating the runner in container $UPDATE"
    run pct exec "$UPDATE" -- curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/model_runner.py" -o /opt/memory-agent-runner/model_runner.py
    run pct exec "$UPDATE" -- systemctl restart memory-agent-runner
    say "Done: the runner in container $UPDATE is up to date; its keys, models and settings were kept"
    exit 0
fi

# --- questions -----------------------------------------------------------------------------------------------------
next=""
(( DRY )) && ! have pvesh || next="$(pvesh get /cluster/nextid 2>/dev/null || true)"
ask CTID "Container ID" "${next:-100}"
[[ "$CTID" =~ ^[0-9]+$ ]] || die "the container ID must be a number"
if ! (( DRY )) && have pvesh && ! pvesh get /cluster/nextid --vmid "$CTID" >/dev/null 2>&1; then die "ID $CTID is already in use"; fi
ask HOSTNAME_ "Host name" "llama-runner"
defstore=""
have pvesm && defstore="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')"
ask STORAGE "Storage for the container disk" "${defstore:-local-lvm}"

# the GPU: this host's render devices, with their names
gpus=()
for n in /dev/dri/renderD*; do [[ -e "$n" ]] && gpus+=("$n"); done
if [[ -z "$GPU" ]]; then
    (( ${#gpus[@]} )) || (( DRY )) || die "this host has no GPU render device (/dev/dri/renderD*)"
    if (( ${#gpus[@]} == 1 )); then
        GPU="${gpus[0]}"
    elif (( ${#gpus[@]} > 1 )); then
        echo "GPUs on this host:"
        for n in "${gpus[@]}"; do
            pci="$(basename "$(readlink -f "/sys/class/drm/$(basename "$n")/device")" 2>/dev/null)"
            name="$(have lspci && lspci -s "$pci" 2>/dev/null | cut -d' ' -f2- | cut -c1-60 || true)"
            echo "  $n  ${name:-$pci}"
        done
        ask GPU "GPU to pass in" "${gpus[0]}"
    else
        GPU="/dev/dri/renderD128"
    fi
fi
[[ "$GPU" =~ ^/dev/dri/renderD[0-9]+$ ]] || die "--gpu must be a device such as /dev/dri/renderD128"
(( DRY )) || [[ -e "$GPU" ]] || die "$GPU does not exist on this host"

ask BRIDGE "Network bridge" "vmbr0"
ask IP "Address (dhcp, or a fixed one such as 192.168.1.60/24)" "dhcp"
if [[ "$IP" != dhcp ]]; then ask GW "Gateway" ""; fi
ask DNS "DNS server (or host for this host's)" "${GW:-host}"
ask_number DISK "Disk size in GB (the models live here)" "--disk"
ask_number MEMORY "Memory in MB" "--memory"
ask_number CORES "CPU cores" "--cores"

# --- template and container ----------------------------------------------------------------------------------------
say "Debian 12 template"
if (( DRY )) && ! have pveam; then
    TEMPLATE="debian-12-standard_<newest>_amd64.tar.zst"
else
    run pveam update >/dev/null
    TEMPLATE="$(pveam available --section system | awk '{print $2}' | grep '^debian-12-standard_' | sort -V | tail -n 1)"
    [[ -n "$TEMPLATE" ]] || die "no Debian 12 template offered by pveam"
fi
if (( DRY )) || ! pveam list "$TSTORAGE" | grep -q "$TEMPLATE"; then
    run pveam download "$TSTORAGE" "$TEMPLATE"
fi

say "Creating container $CTID"
net="name=eth0,bridge=$BRIDGE,ip=$IP"
[[ -n "$GW" ]] && net="$net,gw=$GW"
dns=(); [[ "$DNS" != host ]] && dns=(--nameserver "$DNS")
run pct create "$CTID" "$TSTORAGE:vztmpl/$TEMPLATE" \
    --hostname "$HOSTNAME_" --ostype debian --unprivileged 1 \
    --cores "$CORES" --memory "$MEMORY" --swap 512 --rootfs "$STORAGE:$DISK" \
    --net0 "$net" "${dns[@]}" --onboot 1 --start 1
say "Passing $GPU into the container"
run pct set "$CTID" --dev0 "$GPU,mode=0666"
run pct reboot "$CTID"

if ! (( DRY )); then
    printf 'waiting for the container network'
    for _ in $(seq 60); do
        pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
        printf '.'; sleep 2
    done
    echo
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "the container cannot look up deb.debian.org; check --bridge, --ip, --gw and --dns"
fi

# --- inside the container: llama.cpp for Vulkan, then the runner ---------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/inside.sh" <<'INSIDE'
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
REPO=@REPO@; REF=@REF@; LLAMA_PORT=@LLAMA_PORT@; RUNNER_PORT=@RUNNER_PORT@; LLAMA_REF=@LLAMA_REF@; MODELS=@MODELS@
LLAMA=/opt/llama.cpp
APP=/opt/memory-agent-runner
echo "== Packages"
apt-get update -q
apt-get install -y -q --no-install-recommends python3 ca-certificates curl git cmake build-essential libcurl4-openssl-dev \
    libvulkan-dev mesa-vulkan-drivers glslc vulkan-tools
vulkaninfo --summary 2>/dev/null | sed -n 's/^\s*deviceName\s*=\s*/Vulkan device: /p' || true
echo "== llama.cpp for Vulkan (this takes a while)"
if [[ ! -x "$LLAMA/build/bin/llama-server" ]]; then
    if [[ ! -d "$LLAMA/.git" ]]; then
        clone=(git clone --depth 1)
        [[ -n "$LLAMA_REF" ]] && clone+=(--branch "$LLAMA_REF")
        "${clone[@]}" https://github.com/ggml-org/llama.cpp "$LLAMA"
    fi
    cmake -S "$LLAMA" -B "$LLAMA/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_VULKAN=ON
    cmake --build "$LLAMA/build" --target llama-server -j "$(nproc)"
fi
echo "== Runner"
install -d -m 755 "$APP" "$MODELS"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/model_runner.py" -o "$APP/model_runner.py"
chmod 755 "$APP/model_runner.py"
key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
out="$(python3 "$APP/model_runner.py" --setup --llama "$LLAMA/build/bin/llama-server" --models "$MODELS" \
    --api-key "$key" --jinja --llama-port "$LLAMA_PORT" --port "$RUNNER_PORT")"
token="$(printf '%s\n' "$out" | sed -n 's/^Token for the Settings page (shown once): //p')"
[[ -n "$token" ]] || { echo "the runner did not print a token: $out" >&2; exit 1; }
cat > /etc/systemd/system/memory-agent-runner.service <<UNIT
[Unit]
Description=Memory agent model runner
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $APP/model_runner.py
WorkingDirectory=$APP
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now memory-agent-runner
systemctl restart memory-agent-runner
for _ in $(seq 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$RUNNER_PORT/status" || true)"
    [[ "$code" == 401 || "$code" == 200 ]] && break
    sleep 1
done
[[ "$code" == 401 || "$code" == 200 ]] || { echo "the runner did not start: journalctl -u memory-agent-runner" >&2; exit 1; }
install -m 600 /dev/null /root/runner-info
printf 'KEY=%s\nTOKEN=%s\n' "$key" "$token" > /root/runner-info
INSIDE
sed -i "s|@REPO@|$REPO|; s|@REF@|$REF|; s|@LLAMA_PORT@|$LLAMA_PORT|; s|@RUNNER_PORT@|$RUNNER_PORT|; s|@LLAMA_REF@|'$LLAMA_REF'|; s|@MODELS@|$MODELS|" "$WORK/inside.sh"

say "Installing inside the container (building llama.cpp takes a while)"
run pct push "$CTID" "$WORK/inside.sh" /root/runner-setup.sh
run pct exec "$CTID" -- bash /root/runner-setup.sh
run pct exec "$CTID" -- rm -f /root/runner-setup.sh

(( DRY )) && { say "Dry run: nothing was changed"; exit 0; }
addr="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
key="$(pct exec "$CTID" -- sed -n 's/^KEY=//p' /root/runner-info)"
token="$(pct exec "$CTID" -- sed -n 's/^TOKEN=//p' /root/runner-info)"
say "Done"
cat <<EOF
On the memory agent, open Settings > Servers > Add server and enter:
  Address:        http://$addr:$LLAMA_PORT
  API key:        $key
  Runner address: http://$addr:$RUNNER_PORT
  Runner token:   $token

Write these down. They are kept in the container in /root/runner-info (root only).
Then open the server's "Models on that machine" on the same card, paste a Hugging Face .gguf link and press Download,
pick it under "Model on that server" on a model's card, and press Apply all.
Update the runner later with: bash -c "\$(curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/scripts/runner-proxmox.sh)" _ --update $CTID
EOF

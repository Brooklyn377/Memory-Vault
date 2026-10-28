#!/usr/bin/env bash
# Memory agent installer. Run as root in the Proxmox host shell.
# Creates a Debian 12 LXC (nesting on), installs the agent and its model servers inside, prints the address
# and the generated login. Run with --help for the options; anything not given on the command line is asked.
set -euo pipefail

REPO="${MA_REPO:-Brooklyn377/Memory-Vault}"   # GitHub owner/name the code is downloaded from
REF="${MA_REF:-main}"                        # branch or tag
SMALL_URL="https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"
# Default models, the ones the original install runs. A link that does not answer is skipped at install time;
# that model can then be downloaded on the Settings page.
DEF_SWEEPER="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf"
DEF_REVIEWER="https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf"
DEF_CURATOR="https://huggingface.co/LiquidAI/LFM2.5-24B-A2B-GGUF/resolve/main/LFM2.5-24B-A2B-Q4_K_M.gguf"

CTID=""; HOSTNAME_=""; STORAGE=""; TSTORAGE="local"; DISK=""; CORES=""; MEMORY=""
BRIDGE=""; IP=""; GW=""; LLAMA_REF=""; UPDATE=""; YES=0; DRY=0; SMALL=0; NOMODELS=0
SWEEPER_URL=""; REVIEWER_URL=""; CURATOR_URL=""

usage() {
    cat <<EOF
Usage: install.sh [options]

Container
  --ctid N              container ID (default: next free)
  --hostname NAME       default: memory-agent
  --storage NAME        storage for the container disk (default: local-lvm, or the first that holds containers)
  --template-storage N  storage for the Debian template (default: local)
  --disk GB             disk size (default: 80, or 16 with --small)
  --cores N             CPU cores (default: all host cores, or 4 with --small)
  --memory MB           memory (default: 24576, or 6144 with --small)
  --bridge NAME         network bridge (default: vmbr0)
  --ip dhcp|CIDR        address, e.g. 192.168.1.60/24 (default: dhcp)
  --gw ADDRESS          gateway, needed with a fixed address

Models (.gguf download links, e.g. Hugging Face "resolve" links)
  --sweeper-url URL     main model, reads chats (default: Qwen3.5-9B Q4_K_M)
  --reviewer-url URL    reviewer model, checks facts and answers chat (default: gpt-oss-20b MXFP4)
  --curator-url URL     curator model, polishes notes (default: LFM2.5-24B-A2B Q4_K_M)
  --small               one tiny test model for all three, small container
  --no-models           no models now; download them later on the Settings page

Other
  --update CTID         update the code in an existing container; config, login and data are kept
  --llama-ref REF       llama.cpp branch or tag to build (default: its newest code)
  --repo OWNER/NAME     where to download the code (default: $REPO)
  --ref REF             branch or tag of the code (default: $REF)
  --yes                 take the defaults instead of asking
  --dry-run             print what would be done, change nothing
EOF
}

while [[ $# -gt 0 ]]; do
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
        --sweeper-url) SWEEPER_URL="$2"; shift ;;
        --reviewer-url) REVIEWER_URL="$2"; shift ;;
        --curator-url) CURATOR_URL="$2"; shift ;;
        --small) SMALL=1 ;;
        --no-models) NOMODELS=1 ;;
        --update) UPDATE="$2"; shift ;;
        --llama-ref) LLAMA_REF="$2"; shift ;;
        --repo) REPO="$2"; shift ;;
        --ref) REF="$2"; shift ;;
        --yes|-y) YES=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
run() {
    if (( DRY )); then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
# ask VAR "question" default: keeps VAR if already set; otherwise asks (or takes the default with --yes).
ask() {
    local var="$1" q="$2" def="$3" ans=""
    [[ -n "${!var}" ]] && return
    if (( YES )) || [[ ! -r /dev/tty ]]; then
        printf -v "$var" '%s' "$def"; return
    fi
    read -r -p "$q [$def]: " ans </dev/tty || true
    printf -v "$var" '%s' "${ans:-$def}"
}
have() { command -v "$1" >/dev/null 2>&1; }
# remote_size URL: prints the download size in GB, or "?" if the server does not say.
remote_size() {
    local n
    n="$(curl -fsIL --max-time 20 "$1" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} END{print n}')"
    if [[ -n "$n" ]]; then awk -v n="$n" 'BEGIN{printf "%.1f GB", n/1e9}'; else echo "? GB"; fi
}

[[ $EUID -eq 0 ]] || (( DRY )) || die "run as root on the Proxmox host"
if ! (( DRY )); then
    have pct || die "pct not found: this must run on a Proxmox VE host"
fi
have curl || die "curl is needed"

# --- the code: from this checkout when run from one, otherwise downloaded --------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TGZ="$WORK/memory-agent.tar.gz"
HERE=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
say "Code"
if [[ -n "$HERE" && -f "$HERE/agent.py" && -f "$HERE/scripts/setup-container.sh" ]]; then
    echo "using the code next to this script: $HERE"
    VERSION="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo local)"
    tar -C "$(dirname "$HERE")" --exclude=.git --exclude=venv --exclude=__pycache__ \
        -czf "$TGZ" "$(basename "$HERE")"
else
    VERSION="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/$REF" || true)"
    [[ "$VERSION" =~ ^[0-9a-f]{40}$ ]] || die "could not find $REPO at $REF on GitHub"
    echo "downloading $REPO at $REF ($VERSION)"
    run curl -fsSL -o "$TGZ" "https://codeload.github.com/$REPO/tar.gz/$VERSION"
fi

if [[ -n "$UPDATE" ]]; then
    CTID="$UPDATE"
else
    # --- questions -----------------------------------------------------------------------------------------
    say "Container"
    next=""; (( DRY )) && ! have pvesh && next=200
    [[ -n "$next" ]] || next="$(pvesh get /cluster/nextid)"
    ask CTID "Container ID" "$next"
    ask HOSTNAME_ "Host name" memory-agent
    defstore="local-lvm"
    if have pvesm && ! pvesm status -content rootdir 2>/dev/null | awk 'NR>1{print $1}' | grep -qx local-lvm; then
        defstore="$(pvesm status -content rootdir 2>/dev/null | awk 'NR==2{print $1}')"
    fi
    ask STORAGE "Storage for the container disk" "${defstore:-local-lvm}"
    if (( SMALL )); then c=4; (( $(nproc) < c )) && c="$(nproc)"; m=6144; d=16
    else c="$(nproc)"; m=24576; d=80; fi
    ask CORES "CPU cores" "$c"
    ask MEMORY "Memory in MB" "$m"
    ask DISK "Disk size in GB" "$d"
    ask BRIDGE "Network bridge" vmbr0
    ask IP "Address (dhcp, or CIDR like 192.168.1.60/24)" dhcp
    if [[ "$IP" != dhcp ]]; then ask GW "Gateway" ""; [[ -n "$GW" ]] || die "a fixed address needs --gw"; fi

    say "Models"
    if (( SMALL )); then
        SWEEPER_URL="$SMALL_URL"; REVIEWER_URL="$SMALL_URL"; CURATOR_URL="$SMALL_URL"
    elif (( ! NOMODELS )); then
        (( YES )) || ! [[ -r /dev/tty ]] || echo "Enter takes the default model, - skips it (download it later on the Settings page), or paste another .gguf link."
        ask SWEEPER_URL "Sweeper model" "$DEF_SWEEPER"
        ask REVIEWER_URL "Reviewer model" "$DEF_REVIEWER"
        ask CURATOR_URL "Curator model" "$DEF_CURATOR"
    fi
    for var in SWEEPER_URL REVIEWER_URL CURATOR_URL; do
        [[ "${!var}" == - ]] && printf -v "$var" '%s' ""
        u="${!var}"; role="${var%_URL}"; role="${role,,}"
        [[ -n "$u" ]] || continue
        if curl -fsIL --max-time 20 -o /dev/null "$u"; then
            echo "  $role: $(basename "${u%%\?*}"), $(remote_size "$u")"
        else
            echo "  $role: $u does not answer; skipped, download it later on the Settings page"
            printf -v "$var" '%s' ""
        fi
    done
    [[ -z "$SWEEPER_URL$REVIEWER_URL$CURATOR_URL" ]] && echo "  none now"

    # --- template --------------------------------------------------------------------------------------------
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

    # --- container -------------------------------------------------------------------------------------------
    say "Creating container $CTID"
    net="name=eth0,bridge=$BRIDGE,ip=$IP"
    [[ -n "$GW" ]] && net="$net,gw=$GW"
    run pct create "$CTID" "$TSTORAGE:vztmpl/$TEMPLATE" \
        --hostname "$HOSTNAME_" --ostype debian --unprivileged 1 --features nesting=1 \
        --cores "$CORES" --memory "$MEMORY" --swap 512 --rootfs "$STORAGE:$DISK" \
        --net0 "$net" --onboot 1 --start 1
fi

if ! (( DRY )); then
    pct status "$CTID" | grep -q running || run pct start "$CTID"
    printf 'waiting for the container network'
    for _ in $(seq 60); do
        pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
        printf '.'; sleep 2
    done
    echo
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "the container has no network; check --bridge, --ip and --gw"
fi

# --- install inside ----------------------------------------------------------------------------------------------
say "Installing inside the container (building llama.cpp takes a while)"
run pct push "$CTID" "$TGZ" /root/memory-agent.tar.gz
run pct exec "$CTID" -- sh -c 'rm -rf /root/memory-agent-src && mkdir /root/memory-agent-src && tar -xzf /root/memory-agent.tar.gz -C /root/memory-agent-src --strip-components=1'
args=(--src /root/memory-agent-src --version "$VERSION" --repo "$REPO" --ref "$REF")
[[ -n "$UPDATE" ]] && args+=(--update)
(( SMALL )) && args+=(--small)
[[ -n "$LLAMA_REF" ]] && args+=(--llama-ref "$LLAMA_REF")
[[ -n "$SWEEPER_URL" ]] && args+=(--sweeper-url "$SWEEPER_URL")
[[ -n "$REVIEWER_URL" ]] && args+=(--reviewer-url "$REVIEWER_URL")
[[ -n "$CURATOR_URL" ]] && args+=(--curator-url "$CURATOR_URL")
run pct exec "$CTID" -- bash /root/memory-agent-src/scripts/setup-container.sh "${args[@]}"

# --- done --------------------------------------------------------------------------------------------------------
(( DRY )) && { say "Dry run: nothing was changed"; exit 0; }
addr="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
say "Done"
echo "Web page:  http://$addr:8000"
if [[ -z "$UPDATE" ]]; then
    echo "Login:     $(pct exec "$CTID" -- sed -n 's/^UI_USER=//p' /etc/memory-agent/web.env)"
    echo "Password:  $(pct exec "$CTID" -- sed -n 's/^UI_PASSWORD=//p' /etc/memory-agent/web.env)"
    echo "Write the password down; you can change it on the Settings page. It is stored in the container"
    echo "in /etc/memory-agent/web.env (readable by root only)."
fi

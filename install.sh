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
BRIDGE=""; IP=""; GW=""; DNS=""; LLAMA_REF=""; UPDATE=""; YES=0; DRY=0; SMALL=0; NOMODELS=0; PLAIN=0
GPU=""; ADDGPU=""
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
  --dns ADDRESS         DNS server, or "host" for the Proxmox host's (default: the gateway with a fixed
                        address, otherwise host)
  --gpu cpu|DEVICE      run the local models on the CPU (default) or on this host's GPU through Vulkan, e.g.
                        --gpu /dev/dri/renderD128; the device is passed into the container (Proxmox VE 8.1+)

Models (.gguf download links, e.g. Hugging Face "resolve" links)
  --sweeper-url URL     main model, reads chats (default: Qwen3.5-9B Q4_K_M)
  --reviewer-url URL    reviewer model, checks facts and answers chat (default: gpt-oss-20b MXFP4)
  --curator-url URL     curator model, polishes notes (default: LFM2.5-24B-A2B Q4_K_M)
  --small               one tiny test model for all three, small container
  --no-models           no models now; download them later on the Settings page

Other
  --update CTID         update the code in an existing container; config, login and data are kept
  --add-gpu CTID        pass a GPU of this host into an existing container and switch its local models to it
                        (asks which GPU; --gpu DEVICE to say it)
  --llama-ref REF       llama.cpp branch or tag to build (default: its newest code)
  --repo OWNER/NAME     where to download the code (default: $REPO)
  --ref REF             branch or tag of the code (default: $REF)
  --yes                 take the defaults instead of asking
  --plain               ask in plain text lines instead of dialog boxes
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
        --dns) DNS="$2"; shift ;;
        --gpu) GPU="$2"; shift ;;
        --add-gpu) ADDGPU="$2"; shift ;;
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
        --plain) PLAIN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n== %s\n' "$*"; }
die() { restore_screen; echo "error: $*" >&2; exit 1; }
run() {
    if (( DRY )); then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }

# How questions are asked: dialog boxes (whiptail, as on every Proxmox host), plain text lines, or not at all.
INTERACTIVE=0; (( YES )) || [[ ! -r /dev/tty ]] || INTERACTIVE=1
UI=text; (( INTERACTIVE && ! PLAIN )) && have whiptail && UI=dialog
ASKING=$INTERACTIVE   # 0 after "Default install": the defaults are taken, but still checked
BACKTITLE="Memory agent installer"
# cancelled: ends the installer; boxes run in a subshell, so the exit trap prints the message after the screen is reset.
cancelled() { : >"$WORK/cancelled"; exit 1; }
# backdrop [message]: paints the terminal itself blue with the installer's name, and the message in the middle.
# whiptail draws each box on a screen of its own and shows the terminal again when the box closes; with this the
# terminal between two boxes looks the same as behind them.
BACKDROP=0
backdrop() {
    local rows=24 cols=80 c
    read -r rows cols < <(stty size </dev/tty 2>/dev/null) || true
    printf '\e[0;97;44m\e[2J\e[1;1H%s\e[?25l' "$BACKTITLE" >/dev/tty
    if [[ -n "${1:-}" ]]; then
        c=$(( (cols - ${#1}) / 2 + 1 )); (( c > 0 )) || c=1
        printf '\e[%d;%dH%s' $(( rows / 2 )) "$c" "$1" >/dev/tty
    fi
    BACKDROP=1
}
# restore_screen: back to the terminal's own colors, after the boxes or when stopping during them.
restore_screen() {
    (( BACKDROP )) || return 0
    printf '\e[0m\e[2J\e[H\e[?25h' >/dev/tty
    BACKDROP=0
}
# wt ARGS...: runs whiptail on the terminal and prints the answer; Cancel or Esc ends the installer.
wt() { whiptail --backtitle "$BACKTITLE" "$@" 3>&1 1>&2 2>&3 </dev/tty || cancelled; backdrop; }

# prompt "question" default "error": asks once and prints the answer; the error of the last answer is shown above.
prompt() {
    local q="$1" def="$2" err="$3" ans=""
    if [[ $UI == dialog ]]; then
        [[ -n "$err" ]] && q="Not accepted: $err"$'\n\n'"$q"
        wt --title "Memory agent" --inputbox "$q" 14 76 "$def"
    else
        [[ -n "$err" ]] && echo "  not accepted: $err" >/dev/tty
        read -r -p "$q [$def]: " ans </dev/tty || true
        printf '%s' "${ans:-$def}"
    fi
}
# check "validator args" value: runs the validator with the value last; it prints why a value is wrong and fails.
check() {
    local -a c
    read -ra c <<<"$1"
    (( ${#c[@]} )) || return 0
    "${c[@]}" "$2"
}
# ask VAR "question" default "validator args": a value from the command line, or the default when not asking, is
# kept if the validator accepts it. Otherwise the question is asked again until it does; without asking, it stops.
ask() {
    local var="$1" q="$2" def="$3" v="$4" ans="${!1}" err=""
    if [[ -n "$ans" ]] || (( ! ASKING )); then
        ans="${ans:-$def}"
        if err="$(check "$v" "$ans")"; then printf -v "$var" '%s' "$ans"; return; fi
        (( ASKING )) || die "$q: $err"
        def="$ans"
    fi
    while :; do
        ans="$(prompt "$q" "$def" "$err")" || exit 1   # Cancel; set -e may be off here (ask after &&)
        if err="$(check "$v" "$ans")"; then printf -v "$var" '%s' "$ans"; return; fi
        def="$ans"
    done
}
# choose VAR "question" default name description ...: like ask, but the answer must be one of the names, picked
# from a menu in a dialog box. Without names (a dry run off Proxmox) anything is taken.
choose() {
    local var="$1" q="$2" def="$3" ans="${!1}" names=() i
    shift 3
    for (( i = 1; i <= $#; i += 2 )); do names+=("${!i}"); done
    if (( ASKING )) && [[ $UI == dialog && -z "$ans" ]] && (( ${#names[@]} )); then
        ans="$(wt --title "Memory agent" --default-item "$def" --menu "$q" 18 76 8 "$@")" || exit 1
        printf -v "$var" '%s' "$ans"; return
    fi
    ask "$var" "$q" "$def" "${names[*]:+one_of ${names[*]}}"
}

# Validators: each takes the value last, prints what is wrong with it and fails, or prints nothing.
one_of() {
    local v="${!#}" o
    for o in "${@:1:$#-1}"; do [[ "$v" == "$o" ]] && return 0; done
    echo "'$v' is not one of: ${*:1:$#-1}"; return 1
}
range() {   # range MIN MAX value: a whole number from MIN to MAX
    [[ "$3" =~ ^(0|[1-9][0-9]{0,8})$ ]] && (( 10#$3 >= $1 && 10#$3 <= $2 )) && return 0
    echo "type a whole number from $1 to $2"; return 1
}
ipv4() {    # true for a dotted IPv4 address
    local o p=()
    local n='(0|[1-9][0-9]{0,2})'
    [[ "$1" =~ ^$n(\.$n){3}$ ]] || return 1
    IFS=. read -ra p <<<"$1"
    for o in "${p[@]}"; do (( 10#$o <= 255 )) || return 1; done
}
ip2int() { local p=(); IFS=. read -ra p <<<"$1"; echo $(( (10#${p[0]} << 24) | (10#${p[1]} << 16) | (10#${p[2]} << 8) | 10#${p[3]} )); }
valid_ctid() {
    if ! [[ "$1" =~ ^[1-9][0-9]{2,8}$ ]]; then echo "the container ID is a number from 100 to 999999999"; return 1; fi
    if have pvesh && ! pvesh get /cluster/nextid --vmid "$1" >/dev/null 2>&1; then
        echo "ID $1 is already used by another container or VM"; return 1
    fi
}
valid_hostname() {
    local label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    [[ "$1" =~ ^$label(\.$label)*$ ]] && return 0
    echo "use letters, digits and hyphens (not at the start or end), e.g. memory-agent"; return 1
}
valid_ip() {
    [[ "$1" == dhcp ]] && return 0
    if [[ "$1" != */* ]] && ipv4 "$1"; then echo "$1 needs its prefix length, e.g. $1/24"; return 1; fi
    ipv4 "${1%/*}" && [[ "${1#*/}" =~ ^[1-9][0-9]?$ ]] && (( 10#${1#*/} >= 1 && 10#${1#*/} <= 32 )) && return 0
    echo "type dhcp, or an address with its prefix length, e.g. 192.168.1.60/24"; return 1
}
valid_gw() {   # the gateway must be in the network of the fixed address in IP
    local a="${IP%/*}" n="${IP#*/}"
    ipv4 "$1" || { echo "a fixed address needs a gateway, e.g. 192.168.1.1"; return 1; }
    [[ "$1" != "$a" ]] || { echo "the gateway cannot be the container's own address"; return 1; }
    (( ( ($(ip2int "$1") ^ $(ip2int "$a")) >> (32 - 10#$n) ) == 0 )) && return 0
    echo "$1 is not in the network of $IP"; return 1
}
valid_dns() {   # "host", or one or more addresses separated by spaces
    local a list=()
    [[ "$1" == host ]] && return 0
    read -ra list <<<"$1"
    for a in "${list[@]}"; do ipv4 "$a" || list=(); done
    (( ${#list[@]} )) && return 0
    echo "type a DNS server address, e.g. 192.168.1.1, or host"; return 1
}
valid_model() {   # "-" skips the model; otherwise a link that answers (checked only while asking)
    [[ "$1" == - ]] && return 0
    [[ "$1" =~ ^https?://[^[:space:]]+$ ]] || { echo "paste a download link (https://...), or - to skip"; return 1; }
    (( ASKING )) || return 0
    [[ $UI == dialog ]] && backdrop "Checking the link..."
    curl -fsIL --max-time 20 -o /dev/null "$1" && return 0
    echo "that link does not answer; paste another, or - to skip"; return 1
}
# remote_size URL: prints the download size in GB, or "?" if the server does not say.
remote_size() {
    local n
    n="$(curl -fsIL --max-time 20 "$1" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} END{print n}')"
    if [[ -n "$n" ]]; then awk -v n="$n" 'BEGIN{printf "%.1f GB", n/1e9}'; else echo "? GB"; fi
}
# gpu_choices: name, description pairs for choose: cpu, then each GPU render device of this host with its PCI name.
gpu_choices() {
    local n dev pci
    printf '%s\n%s\n' cpu "CPU only"
    for n in /dev/dri/renderD*; do
        [[ -e "$n" ]] || continue
        pci="$(basename "$(readlink -f "/sys/class/drm/$(basename "$n")/device")" 2>/dev/null)"
        dev="$(have lspci && lspci -s "$pci" 2>/dev/null | cut -d' ' -f2- | cut -c1-50)"
        printf '%s\n%s\n' "$n" "GPU: ${dev:-$pci}"
    done
}
valid_gpu() { [[ "$1" == cpu || ( "$1" =~ ^/dev/dri/renderD[0-9]+$ && -e "$1" ) ]] && return 0; echo "cpu, or a GPU device such as /dev/dri/renderD128"; return 1; }
# pass_gpu CTID DEVICE: the device into the container at the next free devN, readable by its services.
pass_gpu() {
    local i=0
    while (( ! DRY )) && pct config "$1" | grep -q "^dev$i:"; do i=$((i + 1)); done
    run pct set "$1" --"dev$i" "$2,mode=0666"
}

[[ $EUID -eq 0 ]] || (( DRY )) || die "run as root on the Proxmox host"
if ! (( DRY )); then
    have pct || die "pct not found: this must run on a Proxmox VE host"
fi
have curl || die "curl is needed"

# --- --add-gpu: an existing container gets a GPU and its local models move to it ----------------------------------
if [[ -n "$ADDGPU" ]]; then
    mapfile -t gc < <(gpu_choices)
    (( ${#gc[@]} > 2 )) || die "this host has no GPU render device (/dev/dri/renderD*)"
    [[ "$GPU" == cpu ]] && GPU=""
    choose GPU "GPU to pass into container $ADDGPU" "${gc[2]}" "${gc[@]:2}"
    restore_screen
    say "Passing $GPU into container $ADDGPU"
    pass_gpu "$ADDGPU" "$GPU"
    run pct reboot "$ADDGPU"
    if ! (( DRY )); then
        for _ in $(seq 60); do pct exec "$ADDGPU" -- true >/dev/null 2>&1 && break; sleep 2; done
        pct exec "$ADDGPU" -- test -x /usr/local/bin/memory-agent-backend \
            || die "the container's code is too old for this: update it first (Settings > Updates), then run this again"
    fi
    say "Switching its local models to the GPU (building llama.cpp for Vulkan takes a while)"
    run pct exec "$ADDGPU" -- /usr/local/bin/memory-agent-backend vulkan
    say "Done: the local models of container $ADDGPU run on the GPU. Settings > Servers shows it."
    exit 0
fi

# --- the code: from this checkout when run from one, otherwise downloaded --------------------------------
WORK="$(mktemp -d)"
trap 'restore_screen; [[ -e "$WORK/cancelled" ]] && echo "cancelled; nothing was changed" >&2; rm -rf "$WORK"' EXIT
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
    if [[ $UI == dialog ]]; then
        # While the boxes are in use, the text lines go to a file and are shown after the last box.
        exec 4>&1 1>"$WORK/answers.txt"
        backdrop
        mode="$(wt --title "Memory agent" --notags --menu "Choose an option:" 14 60 2 \
            default "Default Install" advanced "Advanced Install")" || exit 1
        [[ "$mode" == default ]] && ASKING=0
        backdrop "Reading the host's settings..."
    fi
    say "Container"
    next=""; (( DRY )) && ! have pvesh && next=200
    [[ -n "$next" ]] || next="$(pvesh get /cluster/nextid)"
    ask CTID "Container ID" "$next" valid_ctid
    ask HOSTNAME_ "Host name" memory-agent valid_hostname
    stores=()   # name, type: the active storages that hold containers
    if have pvesm; then
        while read -r name type; do stores+=("$name" "$type"); done \
            < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1, $2}')
    fi
    defstore="local-lvm"
    (( ${#stores[@]} )) && ! one_of "${stores[@]}" local-lvm >/dev/null && defstore="${stores[0]}"
    choose STORAGE "Storage for the container disk" "$defstore" "${stores[@]}"
    if (( SMALL )); then c=4; (( $(nproc) < c )) && c="$(nproc)"; m=6144; d=16
    else c="$(nproc)"; m=24576; d=80; fi
    hostmem="$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)"
    ask CORES "CPU cores (1 to $(nproc))" "$c" "range 1 $(nproc)"
    ask MEMORY "Memory in MB (1024 to $hostmem)" "$m" "range 1024 $hostmem"
    ask DISK "Disk size in GB (at least 8)" "$d" "range 8 100000"
    bridges=()   # name, description: the Linux bridges on this host
    for b in /sys/class/net/*/bridge; do [[ -d "$b" ]] && bridges+=("$(basename "$(dirname "$b")")" bridge); done
    choose BRIDGE "Network bridge" vmbr0 "${bridges[@]}"
    ask IP "Address: dhcp, or a fixed address with its prefix length, e.g. 192.168.1.60/24" dhcp valid_ip
    [[ "$IP" != dhcp ]] && ask GW "Gateway for $IP" "" valid_gw
    ask DNS "DNS server: an address (several separated by spaces), or host for the Proxmox host's" \
        "${GW:-host}" valid_dns
    mapfile -t gc < <(gpu_choices)
    if (( ${#gc[@]} > 2 )); then   # this host has a GPU: CPU stays the default
        choose GPU "Run the local models on" cpu "${gc[@]}"
    else
        ask GPU "Run the local models on" cpu valid_gpu
    fi

    say "Models"
    if (( SMALL )); then
        SWEEPER_URL="$SMALL_URL"; REVIEWER_URL="$SMALL_URL"; CURATOR_URL="$SMALL_URL"
    elif (( ! NOMODELS )); then
        (( ! ASKING )) || [[ $UI == dialog ]] || echo "Enter takes the default model, - skips it (download it later on the Settings page), or paste another .gguf link."
        tail=""; [[ $UI == dialog ]] && tail=$'\n\nKeep the link for the default, type - to skip it (download it later on the Settings page), or paste another .gguf link.'
        ask SWEEPER_URL "Sweeper model, reads chats$tail" "$DEF_SWEEPER" valid_model
        ask REVIEWER_URL "Reviewer model, checks facts and answers chat$tail" "$DEF_REVIEWER" valid_model
        ask CURATOR_URL "Curator model, polishes notes$tail" "$DEF_CURATOR" valid_model
    fi
    [[ $UI == dialog ]] && backdrop "Checking the model links..."
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
    if [[ $UI == dialog ]]; then
        models=""
        for var in SWEEPER_URL REVIEWER_URL CURATOR_URL; do
            u="${!var}"; role="${var%_URL}"; models+=$'\n'"  ${role,,}: ${u:+$(basename "${u%%\?*}")}${u:-none, download it later}"
        done
        wt --title "Memory agent" --yesno "Create the container with these settings?

Container ID $CTID, host name $HOSTNAME_, storage $STORAGE
$CORES cores, $MEMORY MB memory, $DISK GB disk
Bridge $BRIDGE, address $IP${GW:+, gateway $GW}, DNS $DNS
Local models on: $([[ $GPU == cpu ]] && echo CPU || echo "GPU $GPU (Vulkan)")
Models:$models" 19 76 >/dev/null
        exec 1>&4 4>&-
        restore_screen
        cat "$WORK/answers.txt"
    fi

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
    dns=(); [[ "$DNS" != host ]] && dns=(--nameserver "$DNS")
    run pct create "$CTID" "$TSTORAGE:vztmpl/$TEMPLATE" \
        --hostname "$HOSTNAME_" --ostype debian --unprivileged 1 --features nesting=1 \
        --cores "$CORES" --memory "$MEMORY" --swap 512 --rootfs "$STORAGE:$DISK" \
        --net0 "$net" "${dns[@]}" --onboot 1 --start 1
    if [[ "$GPU" != cpu ]]; then   # the GPU into the container, then a restart so it appears there
        pass_gpu "$CTID" "$GPU"
        run pct reboot "$CTID"
    fi
fi

if ! (( DRY )); then
    pct status "$CTID" | grep -q running || run pct start "$CTID"
    printf 'waiting for the container network'
    for _ in $(seq 60); do
        pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
        printf '.'; sleep 2
    done
    echo
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "the container cannot look up deb.debian.org; check --bridge, --ip, --gw and --dns"
fi

# --- install inside ----------------------------------------------------------------------------------------------
say "Installing inside the container (building llama.cpp takes a while)"
run pct push "$CTID" "$TGZ" /root/memory-agent.tar.gz
run pct exec "$CTID" -- sh -c 'rm -rf /root/memory-agent-src && mkdir /root/memory-agent-src && tar -xzf /root/memory-agent.tar.gz -C /root/memory-agent-src --strip-components=1'
args=(--src /root/memory-agent-src --version "$VERSION" --repo "$REPO" --ref "$REF")
[[ -n "$UPDATE" ]] && args+=(--update)
(( SMALL )) && args+=(--small)
[[ -n "$LLAMA_REF" ]] && args+=(--llama-ref "$LLAMA_REF")
[[ -n "$GPU" && "$GPU" != cpu ]] && args+=(--gpu vulkan)
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
echo "Update:    pct exec $CTID -- /usr/local/bin/memory-agent-update"

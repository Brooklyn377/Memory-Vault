#!/usr/bin/env bash
# Shared by runner-container.sh and runner-proxmox.sh (they load it, next to themselves or from the repository): lists the
# graphics cards of this machine, asks which of them go into the container (none, one or several), and then lets you pick
# the software llama.cpp runs on for exactly those cards. Vulkan is the default; the scan only adds "(recommended)" to the
# one it thinks is fastest for the cards you picked. Not meant to be run on its own.
#
# Needs from the script that loads it: DRY, YES, GPUS (may be empty), BACKEND (may be empty), ENGINE (podman, docker, or empty
# on a Proxmox host), and the functions say, die and have.

KFD="${RUNNER_KFD:-/dev/kfd}"           # the AMD compute device; a test points these three elsewhere
SYSFS="${RUNNER_SYSFS:-/sys}"
DEVDIR="${RUNNER_DEV:-/dev}"

GPU_PCI=(); GPU_NAME=(); GPU_VEND=(); GPU_RENDER=(); GPU_NVIDX=(); GPU_ROCM=()
SEL=()          # the cards that were picked: indexes into the GPU_* lists
PASS_RENDER=(); PASS_NVIDX=(); PASS_KFD=0   # what goes into the container for the software that was chosen

# gpu_scan: one entry per graphics card: its PCI address, name, vendor, render device, NVIDIA index, and whether ROCm supports it.
gpu_scan() {
    GPU_PCI=(); GPU_NAME=(); GPU_VEND=(); GPU_RENDER=(); GPU_NVIDX=(); GPU_ROCM=()
    local line pci id vend name node idx d rocm nv=""
    have nvidia-smi && nv="$(nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null || true)"
    while IFS= read -r line; do
        pci="${line%% *}"; [[ "$pci" == *:*:* ]] || pci="0000:$pci"
        id="$(printf '%s' "$line" | grep -o '\[[0-9a-f]\{4\}:[0-9a-f]\{4\}\]' | tail -n 1)"
        name="$(printf '%s' "$line" | sed -E 's/^[^ ]+ [^[]*\[[0-9a-f]{4}\]: //; s/ \[[0-9a-f]{4}:[0-9a-f]{4}\].*$//')"
        case "${id:1:4}" in 10de) vend=nvidia ;; 1002) vend=amd ;; 8086) vend=intel ;; *) vend=other ;; esac
        rocm=0
        # cards ROCm supports: RDNA 2 and newer Radeon (RX 6000, 7000, 9000, Pro W6000 and up) and Instinct
        if [[ "$vend" == amd ]] && printf '%s' "$name" | grep -Eiq 'Navi (2[1-4]|3[1-3]|4[48])|Instinct|MI[0-9]{2,3}|(RX|Radeon|Radeon Pro|Radeon PRO) (W)?[679][0-9]{3}'; then rocm=1; fi
        node=""
        for d in "$SYSFS"/class/drm/renderD*/device; do   # the render device that belongs to this card
            [[ -e "$d" ]] || continue
            [[ "$(basename "$(readlink -f "$d")")" == "$pci" ]] && node="$DEVDIR/dri/$(basename "$(dirname "$d")")"
        done
        idx=""
        if [[ "$vend" == nvidia && -n "$nv" ]]; then
            idx="$(printf '%s\n' "$nv" | grep -i -- "${pci#0000:}\$" | head -n 1 | cut -d, -f1 | tr -d ' ' || true)"
        fi
        GPU_PCI+=("$pci"); GPU_NAME+=("$name"); GPU_VEND+=("$vend"); GPU_RENDER+=("$node"); GPU_NVIDX+=("$idx"); GPU_ROCM+=("$rocm")
    done < <(lspci -nn 2>/dev/null | grep -E '\[03(00|02|80)\]' || true)
    if (( ${#GPU_NAME[@]} == 0 )); then   # no lspci: the render devices themselves
        for d in "$SYSFS"/class/drm/renderD*/device; do
            [[ -e "$d" ]] || continue
            pci="$(basename "$(readlink -f "$d")")"
            case "$(cat "$d/vendor" 2>/dev/null)" in 0x10de) vend=nvidia ;; 0x1002) vend=amd ;; 0x8086) vend=intel ;; *) vend=other ;; esac
            GPU_PCI+=("$pci"); GPU_NAME+=("${vend^} graphics card"); GPU_VEND+=("$vend")
            GPU_RENDER+=("$DEVDIR/dri/$(basename "$(dirname "$d")")"); GPU_NVIDX+=(""); GPU_ROCM+=(0)
        done
    fi
}

gpu_label() { local i="$1"; echo "${GPU_NAME[i]}${GPU_RENDER[i]:+ (${GPU_RENDER[i]##*/})}"; }

# _pick_tokens WORDS...: turns what was typed or given (numbers, all, none, a render device, a PCI address) into SEL.
# Prints what is wrong and returns 1 when a word means no card.
_pick_tokens() {
    local n=${#GPU_NAME[@]} t i found
    SEL=()
    for t in "$@"; do
        case "${t,,}" in
            none|cpu|0) SEL=(); return 0 ;;
            all) SEL=(); for (( i = 0; i < n; i++ )); do SEL+=("$i"); done; return 0 ;;
        esac
        found=""
        if [[ "$t" =~ ^[0-9]+$ ]] && (( t >= 1 && t <= n )); then found=$((t - 1))
        else
            for (( i = 0; i < n; i++ )); do
                if [[ -n "${GPU_RENDER[i]}" && ( "$t" == "${GPU_RENDER[i]}" || "$t" == "${GPU_RENDER[i]##*/}" ) ]]; then found=$i; fi
                if [[ "${GPU_PCI[i]}" == "$t" || "${GPU_PCI[i]#0000:}" == "$t" ]]; then found=$i; fi
            done
        fi
        [[ -n "$found" ]] || { echo "'$t' is not one of the cards above"; SEL=(); return 1; }
        [[ " ${SEL[*]} " == *" $found "* ]] || SEL+=("$found")
    done
    return 0
}

# Questions go to the terminal (a test points RUNNER_TTY at a file of answers). The file is opened once, on fd 9, so
# every question reads the next line instead of the first line again.
_can_ask() {
    local tty="${RUNNER_TTY:-/dev/tty}"
    ! (( YES )) && [[ -r "$tty" ]] && [[ -n "${RUNNER_TTY:-}" || -t 0 || -t 2 ]]
}
_ask() {
    [[ -e /proc/self/fd/9 ]] || exec 9<"${RUNNER_TTY:-/dev/tty}"
    printf '%s' "$1" >&2
    read -r -u 9 REPLY
}

# choose_gpus: lists the cards and sets SEL to the ones that go into the container. --gpus (GPUS) answers it: none, all, or
# numbers, render devices or PCI addresses separated by commas. Without it, it asks; with no terminal (or --yes) it only
# goes on when there is a single card, otherwise it stops and says to pass --gpus.
choose_gpus() {
    gpu_scan
    local n=${#GPU_NAME[@]} i ans="" err="" words=() def=""
    SEL=()
    if (( n == 0 )); then echo "No graphics card found: llama.cpp will run on the CPU only."; return 0; fi
    echo "Graphics cards on this machine:"
    for (( i = 0; i < n; i++ )); do echo "  $((i + 1))) $(gpu_label "$i")"; done
    if [[ -n "$GPUS" ]]; then
        read -ra words <<<"${GPUS//,/ }"
        err="$(_pick_tokens "${words[@]}")" || die "--gpus: $err"
        _pick_tokens "${words[@]}"
    else
        if _can_ask; then
            (( n == 1 )) && def="1"
            while :; do
                [[ -n "$err" ]] && echo "  not accepted: $err"
                _ask "Which of them go into the container? Numbers separated by spaces (for example 1 2), all, or none${def:+ [$def]}: " || REPLY=""
                ans="${REPLY:-$def}"
                read -ra words <<<"${ans//,/ }"
                (( ${#words[@]} )) || { err="type at least one number, all or none"; continue; }
                if err="$(_pick_tokens "${words[@]}")"; then _pick_tokens "${words[@]}"; break; fi
            done
        elif (( n == 1 )); then
            echo "Only one card here and no terminal to ask on: using it. Pass --gpus none to run on the CPU instead."
            SEL=(0)
        else
            die "several graphics cards found and nobody to ask: say which go into the container with --gpus (for example --gpus 1, --gpus 1,2 or --gpus none)"
        fi
    fi
    if (( ${#SEL[@]} )); then
        echo "Going into the container:"; for i in "${SEL[@]}"; do echo "  - $(gpu_label "$i")"; done
    else
        echo "No card goes into the container: llama.cpp will run on the CPU only."
    fi
}

# What each software needs, for the cards that were picked. Print what is missing, nothing when it is ready.
_sel_count() {   # vendor
    local i c=0; for i in "${SEL[@]}"; do [[ "${GPU_VEND[i]}" == "$1" ]] && c=$((c + 1)); done; echo "$c"
}
cuda_missing() {
    (( $(_sel_count nvidia) )) || { echo "no NVIDIA card picked"; return; }
    have nvidia-smi && nvidia-smi -L >/dev/null 2>&1 || { echo "the NVIDIA driver (nvidia-smi does not answer)"; return; }
    case "$ENGINE" in
        podman) have nvidia-ctk || [[ -e /etc/cdi/nvidia.yaml || -e /var/run/cdi/nvidia.yaml ]] || echo "the NVIDIA container toolkit (nvidia-ctk)" ;;
        docker) docker info 2>/dev/null | grep -qi nvidia || echo "Docker's NVIDIA runtime (nvidia-container-toolkit)" ;;
    esac
}
rocm_missing() {
    local i ok=0 any=0
    for i in "${SEL[@]}"; do
        if [[ "${GPU_VEND[i]}" == amd ]]; then any=1; (( GPU_ROCM[i] )) && ok=1; fi
    done
    (( any )) || { echo "no AMD card picked"; return; }
    (( ok )) || { echo "your AMD card is not one ROCm supports"; return; }
    [[ -e "$KFD" ]] || echo "$KFD (the amdgpu compute device)"
}

_warn_missing() {   # software, what is missing
    if [[ "$2" == no\ * || "$2" == your\ * ]]; then echo "warning: ${1}: ${2^}. Going on as you chose; it will not work like this." >&2
    else echo "warning: $1 needs $2. Going on as you chose; it will not work until that is in place." >&2; fi
}

# choose_backend: sets BACKEND to vulkan, cuda or rocm for the cards in SEL (cpu when there are none), and PASS_* to what goes into
# the container. Asks, unless --backend was given, --yes was, or there is no terminal.
backend_notes() {   # CMISS, RMISS, REC (the recommended software), and the TAG_*/NOTE_* texts of the menu lines
    CMISS="$(cuda_missing)"; RMISS="$(rocm_missing)"; REC=vulkan
    [[ -z "$CMISS" ]] && REC=cuda
    [[ "$REC" == vulkan && -z "$RMISS" ]] && REC=rocm
    TAG_V="default"; TAG_C=""; TAG_R=""
    [[ "$REC" == vulkan ]] && TAG_V="default, recommended"
    [[ "$REC" == cuda ]] && TAG_C=" (recommended)"
    [[ "$REC" == rocm ]] && TAG_R=" (recommended)"
    if [[ -z "$CMISS" ]]; then NOTE_C="NVIDIA only. Driver and container support found."
    elif [[ "$CMISS" == no\ * ]]; then NOTE_C="NVIDIA only. ${CMISS^}."
    else NOTE_C="NVIDIA only. Missing: $CMISS."; fi
    if [[ -z "$RMISS" ]]; then NOTE_R="AMD only. ROCm card and compute device found."
    elif [[ "$RMISS" == no\ * || "$RMISS" == your\ * ]]; then NOTE_R="AMD only. ${RMISS^}."
    else NOTE_R="AMD only. Missing: $RMISS."; fi
}
choose_backend() {
    local cmiss rmiss rec=vulkan i
    if (( ${#SEL[@]} == 0 )); then
        if [[ -n "$BACKEND" && "$BACKEND" != cpu ]]; then echo "No card was picked, so --backend $BACKEND does not apply: running on the CPU."; fi
        BACKEND=cpu; return 0
    fi
    backend_notes
    cmiss="$CMISS"; rmiss="$RMISS"; rec="$REC"
    if [[ -n "$BACKEND" ]]; then
        [[ "$BACKEND" == vulkan || "$BACKEND" == cuda || "$BACKEND" == rocm ]] || die "--backend must be vulkan, cuda or rocm"
        echo "Software: $BACKEND (from --backend)"
    else
        local tag_v="$TAG_V" tag_c="$TAG_C" tag_r="$TAG_R" note_c="$NOTE_C" note_r="$NOTE_R"
        echo "Software llama.cpp runs on, for the cards you picked (this decides the token speed):"
        echo "  1) Vulkan ($tag_v)   works on AMD, Intel and NVIDIA"
        echo "  2) CUDA$tag_c   $note_c"
        echo "  3) ROCm$tag_r   $note_r"
        local ans=""
        if _can_ask; then
            _ask "Enter 1, 2 or 3 [1]: " || REPLY=""
            ans="$REPLY"
        else
            echo "Not asking (no terminal, or --yes): using Vulkan. Pass --backend cuda or --backend rocm to change it."
        fi
        case "${ans:-1}" in 2|cuda|CUDA) BACKEND=cuda ;; 3|rocm|ROCm|ROCM) BACKEND=rocm ;; *) BACKEND=vulkan ;; esac
    fi
    case "$BACKEND" in
        cuda) [[ -z "$cmiss" ]] || _warn_missing CUDA "$cmiss" ;;
        rocm) [[ -z "$rmiss" ]] || _warn_missing ROCm "$rmiss" ;;
    esac
    backend_pass
}

# backend_pass: PASS_* = what goes into the container, only the picked cards that the software in BACKEND can use.
backend_pass() {
    local i unused=()
    PASS_RENDER=(); PASS_NVIDX=(); PASS_KFD=0
    for i in "${SEL[@]}"; do
        case "$BACKEND" in
            vulkan) if [[ -n "${GPU_RENDER[i]}" ]]; then PASS_RENDER+=("${GPU_RENDER[i]}"); else unused+=("$(gpu_label "$i") has no render device"); fi ;;
            cuda)   if [[ "${GPU_VEND[i]}" == nvidia ]]; then PASS_NVIDX+=("${GPU_NVIDX[i]:-$i}"); else unused+=("$(gpu_label "$i") is not NVIDIA"); fi ;;
            rocm)   if [[ "${GPU_VEND[i]}" == amd ]]; then
                        PASS_KFD=1; [[ -n "${GPU_RENDER[i]}" ]] && PASS_RENDER+=("${GPU_RENDER[i]}")
                    else unused+=("$(gpu_label "$i") is not AMD"); fi ;;
        esac
    done
    if (( ${#unused[@]} )); then printf 'not used by %s: %s\n' "$BACKEND" "$(IFS=';'; echo "${unused[*]}")"; fi
    return 0
}

# host_nvidia: HOST_NV_DRIVER and CUDA_PKG (for example 12-8), read from this host's driver, for a container that gets CUDA.
host_nvidia() {
    have nvidia-smi || die "CUDA needs the NVIDIA driver on this host (nvidia-smi was not found)"
    HOST_NV_DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
    CUDA_PKG="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: \([0-9]*\)\.\([0-9]*\).*/\1-\2/p' | head -n 1)"
    [[ -n "$HOST_NV_DRIVER" && -n "$CUDA_PKG" ]] || die "could not read the NVIDIA driver version from nvidia-smi"
}

# lxc_devices: DEVICES = the host device files an LXC needs for the cards in SEL and the software in BACKEND (after backend_pass).
lxc_devices() {
    local want=() n
    DEVICES=()
    case "$BACKEND" in
        vulkan) want=("${PASS_RENDER[@]}") ;;
        rocm)   want=("$KFD" "${PASS_RENDER[@]}") ;;
        cuda)   for n in "${PASS_NVIDX[@]}"; do want+=("$DEVDIR/nvidia$n"); done
                want+=("$DEVDIR/nvidiactl" "$DEVDIR/nvidia-uvm" "$DEVDIR/nvidia-uvm-tools" "$DEVDIR/nvidia-modeset") ;;
    esac
    for n in "${want[@]}"; do   # the helper devices (uvm, modeset) are not on every host
        [[ -e "$n" ]] || (( DRY )) || continue
        DEVICES+=("$n")
    done
    if [[ "$BACKEND" != cpu ]] && (( ${#DEVICES[@]} == 0 )) && ! (( DRY )); then
        die "none of the devices for the cards you picked exist on this host; is the driver loaded?"
    fi
}

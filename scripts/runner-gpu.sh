#!/usr/bin/env bash
# Shared by runner-container.sh and runner-proxmox.sh (they load it, next to themselves or from the repository): looks
# at the graphics cards of this machine and lets you pick the software llama.cpp runs on. Vulkan is the default; the scan
# only adds "(recommended)" to the one it thinks is fastest here. Not meant to be run on its own.
#
# Needs from the script that loads it: DRY, YES, BACKEND (may be empty), ENGINE (podman, docker, or empty on a Proxmox host),
# and the functions say and have.

GPU_DESC=(); GPU_NV=0; GPU_AMD=0; GPU_AMD_OK=0; GPU_INTEL=0
KFD="${RUNNER_KFD:-/dev/kfd}"   # the AMD compute device; a test points this elsewhere

# gpu_scan: fills GPU_DESC with one line per card, and the GPU_* flags with what kinds of card were found.
gpu_scan() {
    GPU_DESC=(); GPU_NV=0; GPU_AMD=0; GPU_AMD_OK=0; GPU_INTEL=0
    local line id name
    while IFS= read -r line; do
        id="$(printf '%s' "$line" | grep -o '\[[0-9a-f]\{4\}:[0-9a-f]\{4\}\]' | tail -n 1)"
        name="$(printf '%s' "$line" | sed -E 's/^[^ ]+ [^[]*\[[0-9a-f]{4}\]: //; s/ \[[0-9a-f]{4}:[0-9a-f]{4}\].*$//')"
        case "${id:1:4}" in
            10de) GPU_NV=1 ;;
            1002) GPU_AMD=1
                  # cards ROCm supports: RDNA 2 and newer Radeon (RX 6000, 7000, 9000, Pro W6000 and up) and Instinct
                  if printf '%s' "$name" | grep -Eiq 'Navi (2[1-4]|3[1-3]|4[48])|Instinct|MI[0-9]{2,3}|(RX|Radeon|Radeon Pro|Radeon PRO) (W)?[679][0-9]{3}'; then GPU_AMD_OK=1; fi ;;
            8086) GPU_INTEL=1 ;;
        esac
        GPU_DESC+=("$name")
    done < <(lspci -nn 2>/dev/null | grep -E '\[03(00|02|80)\]')
    if (( ${#GPU_DESC[@]} == 0 )); then   # no lspci: the vendor of each render device
        local d v
        for d in /sys/class/drm/renderD*/device; do
            [[ -e "$d/vendor" ]] || continue
            v="$(cat "$d/vendor" 2>/dev/null)"
            case "$v" in 0x10de) GPU_NV=1; GPU_DESC+=("NVIDIA graphics card") ;; 0x1002) GPU_AMD=1; GPU_DESC+=("AMD graphics card") ;;
                         0x8086) GPU_INTEL=1; GPU_DESC+=("Intel graphics card") ;; *) GPU_DESC+=("graphics card $v") ;; esac
        done
    fi
}

# cuda_missing / rocm_missing: print what is missing for that software here, nothing when it is ready.
cuda_missing() {
    (( GPU_NV )) || { echo "no NVIDIA card found"; return; }
    have nvidia-smi && nvidia-smi -L >/dev/null 2>&1 || { echo "the NVIDIA driver (nvidia-smi does not answer)"; return; }
    case "$ENGINE" in
        podman) have nvidia-ctk || [[ -e /etc/cdi/nvidia.yaml || -e /var/run/cdi/nvidia.yaml ]] || echo "the NVIDIA container toolkit (nvidia-ctk)" ;;
        docker) docker info 2>/dev/null | grep -qi nvidia || echo "Docker's NVIDIA runtime (nvidia-container-toolkit)" ;;
    esac
}
rocm_missing() {
    (( GPU_AMD )) || { echo "no AMD card found"; return; }
    (( GPU_AMD_OK )) || { echo "your AMD card is not one ROCm supports"; return; }
    [[ -e "$KFD" ]] || echo "$KFD (the amdgpu compute device)"
}

_warn_missing() {   # software, what is missing
    if [[ "$2" == no\ * || "$2" == your\ * ]]; then echo "warning: ${1}: ${2^}. Going on as you chose; it will not work like this." >&2
    else echo "warning: $1 needs $2. Going on as you chose; it will not work until that is in place." >&2; fi
}

# choose_backend: sets BACKEND to vulkan, cuda or rocm. Asks, unless --backend was given, --yes was, or there is no terminal.
choose_backend() {
    gpu_scan
    local cmiss rmiss rec=vulkan
    cmiss="$(cuda_missing)"; rmiss="$(rocm_missing)"
    [[ -z "$cmiss" ]] && rec=cuda
    [[ "$rec" == vulkan && -z "$rmiss" ]] && rec=rocm
    echo "Graphics cards found:"
    if (( ${#GPU_DESC[@]} )); then printf '  - %s\n' "${GPU_DESC[@]}"; else echo "  none (llama.cpp would run on the CPU only)"; fi
    if [[ -n "$BACKEND" ]]; then
        [[ "$BACKEND" == vulkan || "$BACKEND" == cuda || "$BACKEND" == rocm ]] || die "--backend must be vulkan, cuda or rocm"
        echo "Software: $BACKEND (from --backend)"
    else
        local tag_v="default" tag_c="" tag_r="" note_c note_r
        [[ "$rec" == vulkan ]] && tag_v="default, recommended"
        [[ "$rec" == cuda ]] && tag_c=" (recommended)"
        [[ "$rec" == rocm ]] && tag_r=" (recommended)"
        if [[ -z "$cmiss" ]]; then note_c="NVIDIA only. Driver and container support found."
        elif [[ "$cmiss" == no\ * ]]; then note_c="NVIDIA only. ${cmiss^}."
        else note_c="NVIDIA only. Missing: $cmiss."; fi
        if [[ -z "$rmiss" ]]; then note_r="AMD only. ROCm card and compute device found."
        elif [[ "$rmiss" == no\ * || "$rmiss" == your\ * ]]; then note_r="AMD only. ${rmiss^}."
        else note_r="AMD only. Missing: $rmiss."; fi
        echo "Software llama.cpp runs on (this decides the token speed):"
        echo "  1) Vulkan ($tag_v)   works on AMD, Intel and NVIDIA"
        echo "  2) CUDA$tag_c   $note_c"
        echo "  3) ROCm$tag_r   $note_r"
        local ans=""
        local tty="${RUNNER_TTY:-/dev/tty}"   # a test points this at a file with the answer
        if ! (( YES )) && [[ -r "$tty" ]] && [[ -n "${RUNNER_TTY:-}" || -t 0 || -t 2 ]]; then
            read -r -p "Enter 1, 2 or 3 [1]: " ans <"$tty" || ans=""
        else
            echo "Not asking (no terminal, or --yes): using Vulkan. Pass --backend cuda or --backend rocm to change it."
        fi
        case "${ans:-1}" in 2|cuda|CUDA) BACKEND=cuda ;; 3|rocm|ROCm|ROCM) BACKEND=rocm ;; *) BACKEND=vulkan ;; esac
    fi
    case "$BACKEND" in
        cuda) [[ -z "$cmiss" ]] || _warn_missing CUDA "$cmiss" ;;
        rocm) [[ -z "$rmiss" ]] || _warn_missing ROCm "$rmiss" ;;
    esac
}

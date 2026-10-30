#!/usr/bin/env bash
# Loaded by setup-container.sh and memory-agent-backend (not run on its own): installs what a llama.cpp backend needs in
# this container and sets GPUOPTS to the cmake options for it. cpu needs nothing; vulkan is the one that is well tried;
# cuda and rocm are best effort (the host's driver has to match: its version is kept in /etc/memory-agent/nvidia).
NVFILE=/etc/memory-agent/nvidia

_r() { if declare -F run >/dev/null; then run "$@"; else "$@"; fi; }   # setup-container.sh's run honours --dry-run

backend_prepare() {
    local be="$1" NV_DRIVER="" CUDA_PKG="" gfx
    GPUOPTS=()
    case "$be" in
    cpu) ;;
    vulkan)
        _r env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
            libvulkan-dev mesa-vulkan-drivers glslc vulkan-tools
        GPUOPTS=(-DGGML_VULKAN=ON) ;;
    cuda)   # the host's driver libraries (the same version as the host), then the CUDA toolkit
        [[ -r "$NVFILE" ]] && . "$NVFILE"
        if [[ -z "$NV_DRIVER" || -z "$CUDA_PKG" ]]; then
            (( ${DRY:-0} )) || { echo "error: the host's NVIDIA driver version is not known here ($NVFILE); run install.sh --add-gpu from the Proxmox host" >&2; return 1; }
            NV_DRIVER="<host driver>"; CUDA_PKG="<cuda>"
        fi
        echo "== NVIDIA driver libraries $NV_DRIVER (the same version as the host) and CUDA $CUDA_PKG"
        _r env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends gnupg
        _r curl -fsSL -o /tmp/nvidia.run "https://download.nvidia.com/XFree86/Linux-x86_64/$NV_DRIVER/NVIDIA-Linux-x86_64-$NV_DRIVER.run"
        _r sh /tmp/nvidia.run --silent --no-kernel-module --no-nvidia-modprobe --no-drm --no-x-check
        _r curl -fsSL -o /tmp/cuda-keyring.deb https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb
        _r dpkg -i /tmp/cuda-keyring.deb
        _r apt-get update -q
        _r env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "cuda-toolkit-$CUDA_PKG"
        export PATH="/usr/local/cuda-${CUDA_PKG/-/.}/bin:$PATH"
        (( ${DRY:-0} )) || nvidia-smi -L || { echo "error: the container cannot see the NVIDIA card: the driver libraries here must match the host's" >&2; return 1; }
        GPUOPTS=(-DGGML_CUDA=ON) ;;
    rocm)   # AMD's ROCm packages (built for Ubuntu; they install on Debian 12 in practice)
        echo "== ROCm"
        _r env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends gnupg
        _r install -d -m 755 /etc/apt/keyrings
        if (( ${DRY:-0} )); then
            echo "+ add AMD's ROCm package repository"
        else
            curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/rocm.gpg
            echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/6.4 jammy main" > /etc/apt/sources.list.d/rocm.list
        fi
        _r apt-get update -q
        _r env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends rocm-hip-sdk rocminfo
        if (( ${DRY:-0} )); then GPUOPTS=(-DGGML_HIP=ON); return 0; fi
        export PATH="/opt/rocm/bin:$PATH"
        gfx="$(rocminfo 2>/dev/null | grep -o 'gfx[0-9a-f]*' | head -n 1)"
        [[ -n "$gfx" ]] || { echo "error: rocminfo found no AMD GPU in the container (/dev/kfd and the driver on the host?)" >&2; return 1; }
        echo "AMD GPU target: $gfx"
        export HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)"
        GPUOPTS=(-DGGML_HIP=ON "-DAMDGPU_TARGETS=$gfx") ;;
    *) echo "error: unknown backend $be" >&2; return 1 ;;
    esac
}

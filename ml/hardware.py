"""
Hardware detection — decide GPU vs CPU for training AND inference, once, centrally.

Every model (Autoencoder, GNN) calls `device()` so the same policy applies everywhere. If
torch is not installed (the tree/sklearn-only path still works), this degrades cleanly to a
"cpu" string and torch-dependent models are skipped by the orchestrator.
"""
from __future__ import annotations

import os
import platform
from functools import lru_cache


@lru_cache(maxsize=1)
def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def device() -> str:
    """'cuda', 'mps' (Apple), or 'cpu'. Honours BF_DEVICE to force a choice."""
    forced = os.getenv("BF_DEVICE", "").strip().lower()
    if forced in {"cpu", "cuda", "mps"}:
        return forced
    if not torch_available():
        return "cpu"
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _nvidia_gpu_present() -> bool:
    """True if the host physically has an NVIDIA GPU (via nvidia-smi), regardless of whether
    CUDA initialised in THIS process. Used to tell "no GPU" apart from "GPU present but CUDA
    failed to init" (a transient driver hiccup) so we can log the difference clearly."""
    import shutil
    import subprocess
    if not shutil.which("nvidia-smi"):
        return False
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5, text=True)
        return out.returncode == 0 and "GPU" in (out.stdout or "")
    except Exception:
        return False


def require_gpu(logger=None) -> None:
    """Fail-fast IF BF_REQUIRE_GPU is set and no GPU is usable — so a *scheduled* GPU training job
    does not silently spend hours on CPU after a transient driver hiccup. OFF by default, so the
    normal policy (use GPU if present, else fall back to CPU) is unchanged — no regression."""
    if os.getenv("BF_REQUIRE_GPU", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if device() != "cuda":
        hint = ("a GPU is present but CUDA failed to initialise in this container — check the "
                "NVIDIA Container Toolkit / run with `--gpus all`, then retry"
                if _nvidia_gpu_present() else "no NVIDIA GPU detected on this host")
        msg = f"BF_REQUIRE_GPU is set but device={device()} ({hint})"
        (logger.error if logger else print)(msg)
        raise RuntimeError(msg)


def summary() -> dict:
    """A JSON-able description of the compute environment, logged with every training run."""
    info = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch_available": torch_available(),
        "device": device(),
        "cpu_count": os.cpu_count(),
    }
    if torch_available():
        import torch
        info["torch"] = torch.__version__
        if torch.cuda.is_available():
            i = torch.cuda.current_device()
            info["gpu_name"] = torch.cuda.get_device_name(i)
            info["gpu_mem_gb"] = round(torch.cuda.get_device_properties(i).total_memory / 1e9, 1)
            info["cuda"] = torch.version.cuda
    return info


def log_summary(logger=None) -> dict:
    s = summary()
    msg = (f"[hardware] device={s['device']} torch={s.get('torch','—')} "
           f"gpu={s.get('gpu_name','—')} ({s.get('gpu_mem_gb','?')}GB) cpus={s['cpu_count']}")
    (logger.info if logger else print)(msg)
    # Make a silent CPU fall-back LOUD when a GPU is physically present (transient CUDA-init issue),
    # so an operator sees why a "GPU" job is slow and can retry instead of assuming it's fine.
    if s["device"] == "cpu" and os.getenv("BF_DEVICE", "").strip().lower() != "cpu" \
            and _nvidia_gpu_present():
        warn = ("[hardware] NOTE: an NVIDIA GPU is present but CUDA did not initialise in this "
                "process — running on CPU (slower). This is usually transient; a re-run typically "
                "uses the GPU. Set BF_REQUIRE_GPU=1 to fail-fast instead of CPU-training.")
        (logger.warning if logger else print)(warn)
    return s


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2))

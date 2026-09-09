"""Shared ONNX Runtime helpers: prefer CUDA EP when the GPU wheel is installed."""
from __future__ import annotations

_PRELOADED = False


def preload_ort_cuda_dlls() -> None:
    """Load CUDA/cuDNN DLLs shipped with onnxruntime-gpu[cuda,cudnn]."""
    global _PRELOADED
    if _PRELOADED:
        return
    try:
        import onnxruntime as ort

        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            try:
                preload(cuda=True, cudnn=True)
            except TypeError:
                preload()
            except Exception:
                pass
    except Exception:
        pass
    _PRELOADED = True


def ort_providers() -> list[str]:
    """Return provider list with CUDA first when available."""
    preload_ort_cuda_dlls()
    try:
        import onnxruntime as ort

        avail = set(ort.get_available_providers())
        if "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]
    except Exception:
        return ["CPUExecutionProvider"]

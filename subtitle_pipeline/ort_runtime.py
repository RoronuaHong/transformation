"""Shared ONNX Runtime helpers: prefer CUDA EP when the GPU wheel is installed."""
from __future__ import annotations

import os

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


def ort_providers(*, require_gpu: bool | None = None) -> list:
    """Return provider list with CUDA first when available.

    ``VITUAL_REQUIRE_GPU=1`` (default) raises if CUDA EP is missing so jobs
    never silently fall back to multi-hour CPU ONNX.
    """
    preload_ort_cuda_dlls()
    if require_gpu is None:
        require_gpu = (os.environ.get("VITUAL_REQUIRE_GPU") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
    try:
        import onnxruntime as ort

        avail = set(ort.get_available_providers())
        if "CUDAExecutionProvider" in avail:
            cuda_opts = {
                "device_id": 0,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
            }
            return [("CUDAExecutionProvider", cuda_opts), "CPUExecutionProvider"]
        if require_gpu:
            raise RuntimeError(
                "CUDAExecutionProvider unavailable; install onnxruntime-gpu "
                f"(have {sorted(avail)}). Set VITUAL_REQUIRE_GPU=0 to allow CPU."
            )
        return ["CPUExecutionProvider"]
    except RuntimeError:
        raise
    except Exception:
        if require_gpu:
            raise
        return ["CPUExecutionProvider"]


def make_session_options():
    """ORT SessionOptions with multi-thread CPU ops (mask/pre/post)."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    threads = max(1, min(8, int(os.environ.get("VITUAL_ORT_THREADS", "4") or 4)))
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = max(1, threads // 2)
    return so

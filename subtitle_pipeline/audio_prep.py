"""Whisper-ready audio prep (16 kHz mono PCM).

Best practice: resample in ffmpeg, never strip silence on the timeline wav
(see PIPELINE_BEST_PRACTICES.md §1).
"""

from __future__ import annotations

from pathlib import Path


def whisper_wav_cmd(ffmpeg: str, src: Path | str, wav: Path | str) -> list[str]:
    """ffmpeg argv: video/audio → full_16k.wav for ASR."""
    return [
        str(ffmpeg),
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]

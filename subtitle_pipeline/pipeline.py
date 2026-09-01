#!/usr/bin/env python3
"""Offline subtitle pipeline: multipass ASR + Ollama translate/summarize.

Requires local faster-whisper weights and a local Ollama chat model.
ASR default: 3-pass Whisper + vote/rule finalize (see multipass_asr.py).
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from job_layout import (
    find_source_media,
    is_usable_wav,
    job_media_dir,
    job_subs_dir,
    locale_srt_path,
)
from langs import (
    coalesce_source_lang,
    file_tag,
    lang_name,
    normalize_lang,
    resolve_targets,
    whisper_lang,
)
from multipass_asr import (
    llm_correct_segments,
    load_hotwords,
    nearest_text,
    save_raw,
    strip_internal_fields,
    transcribe_multipass,
)
from sync_utils import (
    apply_slice_offset,
    assert_timeline_aligned,
    shift_segments,
    validate_srt_monotonic,
    write_sync_meta,
)

DEFAULT_TRANSLATE = "translategemma:4b"
DEFAULT_CHAT = "gemma4:e2b"  # ASR polish / glossary / correct / summarize
DEFAULT_OLLAMA = DEFAULT_TRANSLATE  # CLI --ollama-model (translate)
DEFAULT_CORRECT = DEFAULT_CHAT  # CLI --correct-model / --chat-model
DEFAULT_LANGS = "site"  # 16-lang pack; CLI may pass all / world / core / list
DEFAULT_LANG_WORKERS = 2
# Best open Whisper: large-v3. On RTX 2060 6GB use int8 (see pick_compute_type).
LOCAL_WHISPER = Path(r"D:\LLM\whisper-models\faster-whisper-large-v3")

_print_lock = threading.Lock()


def locked_print(*args, **kwargs) -> None:
    with _print_lock:
        print(*args, **kwargs)


def lang_workers(explicit: int | None = None) -> int:
    """Parallel language jobs for translate / notes localize (1–8)."""
    if explicit is not None:
        return max(1, min(8, int(explicit)))
    raw = (os.environ.get("VITUAL_LANG_WORKERS") or str(DEFAULT_LANG_WORKERS)).strip()
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return DEFAULT_LANG_WORKERS


LOCAL_WHISPER_FALLBACK = Path(r"D:\LLM\whisper-models\faster-whisper-small")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    # uvicorn --reload on Windows may spawn a system Python without imageio_ffmpeg
    root = Path(__file__).resolve().parent
    bundled = root / ".venv" / "Lib" / "site-packages" / "imageio_ffmpeg" / "binaries"
    if bundled.is_dir():
        for p in sorted(bundled.glob("ffmpeg-*")):
            if p.is_file():
                return str(p)
    for p in Path(r"D:\LLM\tools").glob("**/ffmpeg.exe"):
        return str(p)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise FileNotFoundError("ffmpeg not found. Set FFMPEG_PATH or pip install imageio-ffmpeg.")


def find_ollama() -> str:
    which = shutil.which("ollama")
    if which:
        return which
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if local.exists():
        return str(local)
    raise FileNotFoundError("ollama not found")


def stop_ollama_runners() -> None:
    """Unload Ollama models so Whisper has RAM/VRAM (6GB / multipass)."""
    ollama = shutil.which("ollama")
    if not ollama:
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        ollama = str(local) if local.exists() else None
    if not ollama:
        return
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return
    for item in data.get("models") or []:
        name = item.get("name") or item.get("model")
        if not name:
            continue
        try:
            subprocess.run(
                [ollama, "stop", name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except Exception:
            pass
    print("[env] ollama models stopped (free RAM for Whisper)")


def resolve_whisper_id(model_size: str) -> str:
    env = os.environ.get("WHISPER_MODEL_PATH")
    if env and Path(env).exists():
        return env
    candidate = Path(model_size)
    if candidate.exists():
        return str(candidate)
    if LOCAL_WHISPER.exists() and (LOCAL_WHISPER / "model.bin").exists():
        return str(LOCAL_WHISPER)
    if LOCAL_WHISPER_FALLBACK.exists():
        print(f"[whisper] large-v3 not ready, fallback -> {LOCAL_WHISPER_FALLBACK}")
        return str(LOCAL_WHISPER_FALLBACK)
    # Offline: do not download.
    raise FileNotFoundError(
        "No local Whisper model. Offline mode will not download.\n"
        f"  Expected: {LOCAL_WHISPER}\n"
        "  Or set WHISPER_MODEL_PATH to a faster-whisper directory."
    )


def pick_compute_type(device: str, model_id: str) -> str:
    """large-v3 needs ~10GB in float16; on 6GB cards use int8."""
    if device != "cuda":
        return "int8"
    name = model_id.replace("\\", "/").lower()
    if "large-v3" in name and "turbo" not in name:
        return "int8"
    return "float16"


def sec_to_srt(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sec_to_ts(t: float) -> str:
    return sec_to_srt(t).replace(",", ".")


def write_srt(segments: list[dict], path: Path) -> None:
    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{sec_to_srt(seg['start'])} --> {sec_to_srt(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_txt(segments: list[dict], path: Path) -> None:
    """Time on top, text below (delivery format)."""
    blocks = []
    for seg in segments:
        blocks.append(
            f"{sec_to_ts(seg['start'])} --> {sec_to_ts(seg['end'])}\n"
            f"{seg['text'].strip()}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def extract_audio(ffmpeg: str, video: Path, wav: Path) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def release_cuda() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def transcribe(wav: Path, model_size: str, device: str, language: str) -> tuple[list[dict], str]:
    from faster_whisper import WhisperModel

    model_id = resolve_whisper_id(model_size)
    compute = pick_compute_type(device, model_id)
    asr_lang = whisper_lang(language)
    print(f"[whisper] loading {model_id} device={device} compute={compute}")
    model = WhisperModel(model_id, device=device, compute_type=compute)
    try:
        kwargs = dict(vad_filter=True, beam_size=5)
        if asr_lang:
            kwargs["language"] = asr_lang
        segments_iter, info = model.transcribe(str(wav), **kwargs)
        detected = (getattr(info, "language", None) or "en").strip() or "en"
        print(
            f"[whisper] model={model_id} language={detected} "
            f"prob={info.language_probability:.2f}"
        )
        out = []
        for s in segments_iter:
            text = (s.text or "").strip()
            if not text:
                continue
            out.append({"start": float(s.start), "end": float(s.end), "text": text})
        return out, detected
    finally:
        del model
        release_cuda()
        print("[whisper] released GPU memory")


def ollama_chat(
    ollama: str,
    model: str,
    prompt: str,
    timeout: int = 300,
    *,
    role: str = "chat",
) -> str:
    """Complete via active LLM profile. ``ollama`` kept for call-site compat."""
    from llm.factory import complete

    return complete(role, prompt, timeout=timeout, model=model)


def ensure_ollama_model(model: str, *, role: str = "chat") -> None:
    """Ensure the role's backend is ready (Ollama tags / API key present)."""
    from llm.factory import ensure_role_ready

    ensure_role_ready(role, model=model)


def extract_json_object(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        return json.loads(match.group(0))
    return {}


def extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            return []
    return []


def format_glossary(glossary: dict) -> str:
    names = glossary.get("names") or {}
    terms = glossary.get("terms") or {}
    fixes = glossary.get("asr_fix") or {}
    lines = []
    if names:
        lines.append("Names (keep consistent):")
        for src, dst in names.items():
            lines.append(f"- {src} = {dst}")
    if terms:
        lines.append("Terms:")
        for src, dst in terms.items():
            lines.append(f"- {src} = {dst}")
    if fixes:
        lines.append("ASR corrections (source language):")
        for src, dst in fixes.items():
            lines.append(f"- {src} -> {dst}")
    return "\n".join(lines)


def apply_asr_fix(text: str, glossary: dict) -> str:
    for src, dst in (glossary.get("asr_fix") or {}).items():
        if src and dst:
            text = text.replace(str(src), str(dst))
    return text


def auto_build_glossary(
    ollama: str,
    model: str,
    segments: list[dict],
    source_lang: str,
) -> dict:
    sample = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments[:120]))
    src_name = lang_name(source_lang)
    prompt = (
        f"You are building a subtitle glossary from automatic speech recognition.\n"
        f"Source language: {src_name}.\n"
        "From the lines below, extract:\n"
        "- names: character/place names (source script -> source script, or romanization)\n"
        "- terms: world/setting terms in the source language\n"
        "- asr_fix: obvious ASR mishearings in SOURCE language only "
        "(wrong word -> likely correct source word)\n"
        "Return ONLY valid JSON with keys names, terms, asr_fix. "
        "Values must be objects of string->string. No markdown.\n\n"
        f"{sample}"
    )
    print(f"[auto] extracting glossary via {model}...")
    raw = ollama_chat(ollama, model, prompt)
    data = extract_json_object(raw)
    glossary = {
        "names": {str(k): str(v) for k, v in (data.get("names") or {}).items()},
        "terms": {str(k): str(v) for k, v in (data.get("terms") or {}).items()},
        "asr_fix": {str(k): str(v) for k, v in (data.get("asr_fix") or {}).items()},
    }
    print(
        f"[auto] glossary names={len(glossary['names'])} "
        f"terms={len(glossary['terms'])} asr_fix={len(glossary['asr_fix'])}"
    )
    return glossary


def correct_segments(
    ollama: str,
    model: str,
    segments: list[dict],
    glossary: dict,
    source_lang: str,
    batch_size: int = 8,
) -> list[dict]:
    src_name = lang_name(source_lang)
    fixed: list[dict] = []
    total = len(segments)
    for start in range(0, total, batch_size):
        chunk = segments[start : start + batch_size]
        numbered = "\n".join(
            f"{i+1}. {apply_asr_fix(s['text'], glossary)}" for i, s in enumerate(chunk)
        )
        prompt = (
            f"Correct obvious speech-to-text errors in these {src_name} subtitle lines.\n"
            "Keep spoken style. Do not translate. Do not merge/split lines.\n"
            "Use this glossary when relevant:\n"
            f"{format_glossary(glossary)}\n"
            "Return ONLY JSON array of strings, same length and order.\n\n"
            f"{numbered}"
        )
        print(f"[auto-correct] {start+1}-{start+len(chunk)}/{total}")
        raw = ollama_chat(ollama, model, prompt)
        arr = [str(x).strip() for x in extract_json_array(raw)]
        for i, seg in enumerate(chunk):
            text = arr[i] if i < len(arr) and arr[i] else apply_asr_fix(seg["text"], glossary)
            text = re.sub(r'^["“]|["”]$', "", text).strip()
            fixed.append({"start": seg["start"], "end": seg["end"], "text": text or seg["text"]})
    return fixed


def translate_segments(
    ollama: str,
    model: str,
    segments: list[dict],
    target_lang: str,
    source_lang: str,
    glossary: dict | None = None,
    batch_size: int = 8,
) -> list[dict]:
    src_name = lang_name(source_lang)
    tgt_name = lang_name(target_lang)
    gloss = format_glossary(glossary or {})
    translated: list[dict] = []
    total = len(segments)
    for start in range(0, total, batch_size):
        chunk = segments[start : start + batch_size]
        numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(chunk))
        prompt = (
            f"Translate these {src_name} subtitle lines into {tgt_name}.\n"
            "Rules:\n"
            "- Natural spoken subtitles.\n"
            "- One output line per input line, same order.\n"
            "- Do not merge, split, explain, or add notes.\n"
            "- Prefer Simplified Chinese if target is Simplified Chinese.\n"
            "- Use the glossary for names/terms when present; "
            "otherwise keep names consistent across lines.\n"
            f"{gloss}\n"
            "Return ONLY a JSON array of strings.\n\n"
            f"{numbered}"
        )
        locked_print(
            f"[translate {file_tag(target_lang)}] {start+1}-{start+len(chunk)}/{total}"
        )
        raw = ollama_chat(ollama, model, prompt, role="translate")
        arr = [str(x).strip() for x in extract_json_array(raw)]
        for i, seg in enumerate(chunk):
            text = arr[i] if i < len(arr) and arr[i] else ""
            if not text:
                text = ollama_chat(
                    ollama,
                    model,
                    f"Translate to {tgt_name}. Output only the translation:\n{seg['text']}",
                    role="translate",
                )
                text = text.splitlines()[0].strip() if text else seg["text"]
            text = re.sub(r'^["“]|["”]$', "", text).strip()
            translated.append({"start": seg["start"], "end": seg["end"], "text": text})
    return translated


def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())
    segs: list[dict] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        timing = None
        content_start = 1
        for idx, ln in enumerate(lines):
            if "-->" in ln:
                timing = ln
                content_start = idx + 1
                break
        if not timing:
            continue
        m = re.match(
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
            timing.strip(),
        )
        if not m:
            continue

        def to_sec(h, mi, s, ms):
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0

        start = to_sec(*m.groups()[:4])
        end = to_sec(*m.groups()[4:])
        body = " ".join(lines[content_start:]).strip()
        if body:
            segs.append({"start": start, "end": end, "text": body})
    return segs


def segments_plain_text(segments: list[dict]) -> str:
    return "\n".join(s["text"].strip() for s in segments if s.get("text"))


def _chunk_transcript(text: str, *, max_chars: int = 5500) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > max_chars and buf:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [text]


_OVERVIEW_AD_MARKERS = (
    "无痛",
    "出图",
    "找AI",
    "短视频平台",
    "朋友圈",
    "订阅",
    "关注我",
    "点赞",
    "三连",
)


def _strip_overview_ads(text: str) -> str:
    """Drop CTA / promo sentences that models sometimes treat as methods."""
    parts = [p.strip() for p in text.replace("\n", "").split("。") if p.strip()]
    kept: list[str] = []
    for p in parts:
        if any(m in p for m in _OVERVIEW_AD_MARKERS):
            continue
        if "AI" in p and ("图" in p or "减肥" in p):
            continue
        kept.append(p)
    if not kept:
        return text.strip()
    body = "。".join(kept).strip()
    if not body.endswith(("。", "！", "？", ".", "!", "?")):
        body += "。"
    return body


def _filter_checklist_ads(checklist: str) -> str:
    lines: list[str] = []
    for line in checklist.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(m in s for m in _OVERVIEW_AD_MARKERS):
            continue
        if "AI" in s and ("图" in s or "无痛" in s):
            continue
        lines.append(s)
    return "\n".join(lines)


def _overview_method_checklist(
    ollama: str,
    model: str,
    text: str,
    source_lang: str,
    summary_lang: str,
) -> str:
    """Ordered list of methods/tips so the final overview cannot skip beats."""
    out_name = lang_name(summary_lang)
    src_name = lang_name(source_lang)
    print("[overview] extract method checklist")
    prompt = (
        f"From this FULL {src_name} transcript, list EVERY distinct method / tip / ranking item "
        f"the speaker discusses as part of the main topic, in video order. Write in {out_name}.\n"
        "One short line per item: name + 3–12 word stance (recommend/warn/joke/rank).\n"
        "Include items like 少吃碳水, 精神减肥, 打针, 失恋, 爬楼梯, 16加8 when present.\n"
        "STRICTLY skip: ads, sponsors, AI image / '无痛出图' promos, subscribe CTAs, "
        "empty banter, closing slogans.\n"
        "Return plain lines only, no JSON, no intro.\n\n"
        f"{text[:20000]}"
    )
    return _filter_checklist_ads(ollama_chat(ollama, model, prompt, timeout=180).strip())


def summarize_overview(
    ollama: str,
    model: str,
    text: str,
    source_lang: str,
    summary_lang: str,
) -> str:
    """总体总结 from the FULL transcript — cover every major beat in order."""
    out_name = lang_name(summary_lang)
    src_name = lang_name(source_lang)
    chunks = _chunk_transcript(text)
    section_notes: list[str] = []
    checklist = _overview_method_checklist(
        ollama, model, text, source_lang=source_lang, summary_lang=summary_lang
    )

    if len(chunks) == 1:
        material = chunks[0]
    else:
        for i, chunk in enumerate(chunks, 1):
            print(f"[overview] map chunk {i}/{len(chunks)}")
            prompt = (
                f"Read this {src_name} transcript SECTION ({i}/{len(chunks)}, in video order).\n"
                f"Write in {out_name}. List EVERY distinct method / tip / claim the speaker makes "
                "in this section, in order. One short line per item. "
                "Keep concrete names (e.g. 少吃碳水, 16加8, 爬楼梯, 打针). "
                "Skip ads, subscribe CTAs, and empty banter.\n\n"
                f"{chunk}"
            )
            section_notes.append(
                f"[section {i}/{len(chunks)}]\n"
                + ollama_chat(ollama, model, prompt, timeout=180)
            )
        material = "\n\n".join(section_notes)

    print("[overview] synthesize full-video summary")
    prompt = (
        f"You write the 总体总结 for a video, in {out_name}.\n"
        "Input is the FULL transcript (or ordered section digests covering the whole video),\n"
        "plus a REQUIRED coverage checklist of methods already extracted in order.\n"
        "Return ONLY valid JSON: {\"summary\": \"...\"}.\n"
        "Rules for summary:\n"
        "- 5–8 sentences (or enough to cover the whole video; do not stop at 3 if more topics exist).\n"
        "- You MUST cover every checklist item by name, in the same order; do not merge or drop any.\n"
        "- Walk the video end-to-end; keep the speaker's stance (recommend / warn / joke / rank).\n"
        "- Keep concrete details from the transcript (numbers, caveats, examples) when they matter.\n"
        "- Cover the CORE content; do not collapse into vague diet/emotion advice.\n"
        "- Skip ads, AI-image promo, '无痛出图', subscribe CTAs — never treat them as methods.\n"
        "- Do not invent content absent from the transcript.\n"
        "- Not a bullet list; continuous paragraph(s). Not a one-liner teaser.\n\n"
        f"REQUIRED checklist (cover all, in order):\n{checklist}\n\n"
        f"Transcript / digests:\n{material[:18000]}"
    )
    raw = ollama_chat(ollama, model, prompt, timeout=300)
    try:
        data = extract_json_object(raw)
        overview = str(data.get("summary") or "").strip()
    except Exception:
        overview = ""
    if not overview:
        # Model sometimes returns bare prose
        overview = raw.strip().strip("`").strip()
        if overview.lower().startswith("summary"):
            overview = overview.split(":", 1)[-1].strip()
    return _strip_overview_ads(overview)


def summarize_text(
    ollama: str,
    model: str,
    text: str,
    source_lang: str,
    summary_lang: str,
) -> dict:
    out_name = lang_name(summary_lang)
    src_name = lang_name(source_lang)
    chunks = _chunk_transcript(text)

    partials: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"[summary] chunk {i}/{len(chunks)}")
            prompt = (
                f"Extract study-note bullets from this {src_name} transcript chunk.\n"
                f"Write in {out_name}. Group as 重点 / 要点 / 难点. Plain bullets only.\n\n"
                f"{chunk}"
            )
            partials.append(ollama_chat(ollama, model, prompt, timeout=180))
        else:
            partials.append(chunk)

    material = "\n".join(partials) if len(chunks) > 1 else text
    # Dedicated pass: overview must see the whole video, not a truncated notes prompt.
    overview = summarize_overview(
        ollama, model, text, source_lang=source_lang, summary_lang=summary_lang
    )

    prompt = (
        f"You write study notes from a video transcript for learners.\n"
        f"Transcript language: {src_name}. Write the ENTIRE JSON in {out_name}.\n"
        "This is a 整理稿, NOT a blog recap and NOT a long essay.\n"
        "Return ONLY valid JSON with keys:\n"
        '  "title" (string),\n'
        '  "one_liner" (string, ONE short sentence for the page teaser),\n'
        '  "focuses" (array of 3-6 objects: 重点 / core ideas you must grasp),\n'
        '  "key_points" (array of 5-12 objects: 要点 / concrete facts, steps, numbers),\n'
        '  "hard_points" (array of 3-8 objects: 难点 / pitfalls, easy mix-ups, constraints).\n'
        "Each list object: {\"title\" (≤18 words), \"detail\" (1-2 short sentences)}.\n"
        "Rules:\n"
        "- Lists only in focuses/key_points/hard_points. Do not put an essay in those fields.\n"
        "- 重点 ≠ 要点: focuses = what the lesson is about; key_points = listed facts/steps.\n"
        "- 难点 is NOT a restatement of 要点; it is mistakes, caveats, or hard-to-get bits.\n"
        "- 要点 must be doable: objects, ratios, times, placements "
        "(e.g. vinegar 1:1, soak 30 min, tray on the nightstand).\n"
        "- If the speaker ranks methods (排行 / Tip 1 / 第一), keep that order in key_points "
        "and name the method clearly in the title.\n"
        "- Ban empty slogans such as 'optimize your flow' or 'maximize space' "
        "unless the same bullet also says how.\n"
        "- Skip ads, sponsors, subscribe CTAs, AI image promo, unrelated banter.\n"
        "- Do not invent steps the transcript does not support.\n"
        "- No markdown, no numbering inside strings.\n\n"
        f"Known full-video overview (do not paste into list fields):\n{overview}\n\n"
        f"{material[:14000]}"
    )
    raw = ollama_chat(ollama, model, prompt, timeout=300)
    data = extract_json_object(raw)
    focuses = normalize_key_points(data.get("focuses"))
    key_points = normalize_key_points(data.get("key_points"))
    hard_points = normalize_key_points(data.get("hard_points"))
    one_liner = str(data.get("one_liner") or "").strip()
    if overview and one_liner and overview == one_liner:
        overview = ""
    if not overview and one_liner:
        focus_bits = [f"{x['title']}：{x['detail']}" for x in focuses[:4] if x.get("title")]
        overview = " ".join(focus_bits)[:480] if focus_bits else one_liner
    outline = [str(x) for x in (data.get("outline") or [])]
    if not outline:
        outline = [x["title"] for x in focuses] or ([one_liner] if one_liner else [])
    if not key_points and outline:
        key_points = [{"title": x, "detail": ""} for x in outline if x]
    return {
        "title": str(data.get("title") or ""),
        "source_lang": normalize_lang(source_lang),
        "summary_lang": normalize_lang(summary_lang),
        "one_liner": one_liner,
        "summary": overview or one_liner,
        "outline": outline,
        "focuses": focuses,
        "key_points": key_points,
        "hard_points": hard_points,
        "keypoints_intro": one_liner,
        "speakers": [str(x) for x in (data.get("speakers") or [])],
        "topics": [str(x) for x in (data.get("topics") or [])],
        "quotes": [str(x) for x in (data.get("quotes") or [])],
    }


def normalize_key_points(raw: object) -> list[dict]:
    """Coerce LLM output to [{title, detail, ...optional image/clip/start_sec}, ...]."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if not title:
                continue
            row: dict = {"title": title, "detail": detail}
            img = str(item.get("image") or "").strip()
            if img:
                row["image"] = img
            clip = str(item.get("clip") or "").strip()
            if clip:
                row["clip"] = clip
            if item.get("start_sec") is not None:
                try:
                    row["start_sec"] = float(item["start_sec"])
                except (TypeError, ValueError):
                    pass
            if item.get("end_sec") is not None:
                try:
                    row["end_sec"] = float(item["end_sec"])
                except (TypeError, ValueError):
                    pass
            out.append(row)
        elif isinstance(item, str) and item.strip():
            out.append({"title": item.strip(), "detail": ""})
    return out


def notes_minimally_valid(data: dict | None) -> bool:
    """Accept notes that have a teaser/overview and at least one structured list."""
    if not isinstance(data, dict):
        return False
    teaser = str(data.get("one_liner") or data.get("summary") or "").strip()
    if not teaser:
        return False
    lists = (
        normalize_key_points(data.get("focuses")),
        normalize_key_points(data.get("key_points")),
        normalize_key_points(data.get("hard_points")),
    )
    return any(lists)


def summarize_keypoints(
    ollama: str,
    model: str,
    text: str,
    source_lang: str,
    output_lang: str | None = None,
) -> dict:
    """Extract human-friendly bullet key points from a subtitle transcript."""
    out_lang = normalize_lang(output_lang or source_lang)
    src_lang = normalize_lang(source_lang)
    out_name = lang_name(out_lang)
    src_name = lang_name(src_lang)

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > 6000 and buf:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))

    notes: list[str] = []
    if len(chunks) > 1:
        for i, chunk in enumerate(chunks, 1):
            print(f"[keypoints] chunk {i}/{len(chunks)}")
            prompt = (
                f"Extract bullet notes from this {src_name} transcript chunk.\n"
                f"Write in {out_name}. Plain bullets only, one tip per line.\n\n"
                f"{chunk}"
            )
            notes.append(ollama_chat(ollama, model, prompt, timeout=180))
        material = "\n".join(notes)
    else:
        material = text

    prompt = (
        f"You summarize video subtitles for human readers.\n"
        f"Transcript language: {src_name}.\n"
        f"Write the ENTIRE JSON in {out_name}.\n"
        "Return ONLY valid JSON with keys:\n"
        '  "intro" (string, one friendly sentence: what the viewer will learn),\n'
        '  "key_points" (array of 5-12 objects, each with:\n'
        '      "title" (string, ≤18 words, the takeaway headline),\n'
        '      "detail" (string, 1-2 short sentences, concrete and actionable)\n'
        "  )\n"
        "Rules:\n"
        "- One distinct tip per key_points item; merge duplicates\n"
        "- Plain language; no markdown; no numbering inside strings\n"
        "- Skip ads, sponsors, subscribe CTAs, unrelated banter\n"
        "- Do not invent steps not supported by the transcript\n"
        "- Prefer verbs/imperatives where natural (e.g. 用醋清洁… / Use vinegar to…)\n\n"
        f"{material[:12000]}"
    )
    raw = ollama_chat(ollama, model, prompt, timeout=300)
    data = extract_json_object(raw)
    key_points = normalize_key_points(data.get("key_points"))
    intro = str(data.get("intro") or "").strip()
    if not key_points and intro:
        key_points = [{"title": intro, "detail": ""}]
    return {
        "intro": intro,
        "key_points": key_points,
        "source_lang": src_lang,
        "keypoints_lang": out_lang,
    }


def notes_headings(lang: str) -> dict[str, str]:
    """Section titles for the study-notes document (重点 / 要点 / 难点)."""
    table: dict[str, dict[str, str]] = {
        "zh": {
            "focuses": "重点",
            "key_points": "要点",
            "hard_points": "难点",
        },
        "zh-Hant": {
            "focuses": "重點",
            "key_points": "要點",
            "hard_points": "難點",
        },
        "en": {
            "focuses": "Core points",
            "key_points": "Key points",
            "hard_points": "Hard points",
        },
        "ja": {
            "focuses": "重点",
            "key_points": "要点",
            "hard_points": "難点",
        },
        "ko": {
            "focuses": "핵심",
            "key_points": "요점",
            "hard_points": "난점",
        },
        "ru": {
            "focuses": "Главное",
            "key_points": "Ключевые пункты",
            "hard_points": "Сложные места",
        },
        "pt": {
            "focuses": "O essencial",
            "key_points": "Pontos-chave",
            "hard_points": "Dificuldades",
        },
        "de": {
            "focuses": "Kern",
            "key_points": "Kernpunkte",
            "hard_points": "Schwierigkeiten",
        },
        "es": {
            "focuses": "Lo esencial",
            "key_points": "Puntos clave",
            "hard_points": "Dificultades",
        },
        "fr": {
            "focuses": "L’essentiel",
            "key_points": "Points clés",
            "hard_points": "Difficultés",
        },
        "ar": {
            "focuses": "المحور",
            "key_points": "النقاط الرئيسية",
            "hard_points": "الصعوبات",
        },
        "hi": {
            "focuses": "मुख्य बातें",
            "key_points": "मुख्य बिंदु",
            "hard_points": "कठिन बिंदु",
        },
        "id": {
            "focuses": "Inti",
            "key_points": "Poin utama",
            "hard_points": "Kesulitan",
        },
        "vi": {
            "focuses": "Trọng tâm",
            "key_points": "Điểm chính",
            "hard_points": "Phần khó",
        },
        "th": {
            "focuses": "จุดสำคัญ",
            "key_points": "ประเด็นสำคัญ",
            "hard_points": "จุดยาก",
        },
        "tr": {
            "focuses": "Özü",
            "key_points": "Önemli noktalar",
            "hard_points": "Zor noktalar",
        },
    }
    return table.get(normalize_lang(lang)) or table["en"]


def notes_fields(data: dict) -> dict[str, object]:
    """Stable payload for locale JSON / content.db."""
    one_liner = str(data.get("one_liner") or "").strip()
    overview = str(data.get("summary") or "").strip()
    if not one_liner and overview:
        # Legacy rows stored overview in one_liner only.
        one_liner = overview.split("。")[0].strip("。") + "。" if "。" in overview else overview[:80]
    if not overview:
        overview = one_liner
    if overview == one_liner:
        # Prefer a slightly richer overview when only one text exists.
        focus_bits = []
        for x in data.get("focuses") or []:
            if isinstance(x, dict) and x.get("title"):
                focus_bits.append(str(x["title"]))
        if focus_bits and one_liner:
            overview = f"{one_liner} 本视频围绕：{'、'.join(focus_bits[:5])}。"
    return {
        "title": str(data.get("title") or ""),
        "one_liner": one_liner,
        "summary": overview,
        "outline": [str(x) for x in (data.get("outline") or [])],
        "keypoints_intro": str(data.get("keypoints_intro") or one_liner),
        "focuses": normalize_key_points(data.get("focuses")),
        "key_points": normalize_key_points(data.get("key_points")),
        "hard_points": normalize_key_points(data.get("hard_points")),
    }


def notes_locale_dir(out_dir: Path, lang: str) -> Path:
    return Path(out_dir) / "notes" / file_tag(lang)


def render_notes_markdown(data: dict) -> str:
    """Human 整理稿: title + one_liner + 总体总结 + 重点 / 要点 / 难点."""
    lang = str(data.get("summary_lang") or data.get("source_lang") or "en")
    heads = notes_headings(lang)
    one = str(data.get("one_liner") or "").strip()
    overview = str(data.get("summary") or "").strip() or one
    lines = [
        f"# {data.get('title') or 'notes'}",
        "",
        f"- source: {data.get('source_lang')}",
        f"- notes language: {data.get('summary_lang') or lang}",
        "",
    ]
    if one:
        lines += [f"**{one}**", ""]
    if overview and overview != one:
        lines += ["## 总体总结", "", overview, ""]
    elif overview:
        lines += ["## 总体总结", "", overview, ""]
    lines += _md_note_items(heads["focuses"], normalize_key_points(data.get("focuses")))
    lines += _md_note_items(heads["key_points"], normalize_key_points(data.get("key_points")))
    lines += _md_note_items(heads["hard_points"], normalize_key_points(data.get("hard_points")))
    return "\n".join(lines).rstrip() + "\n"


def write_locale_notes(
    data: dict, out_dir: Path, lang: str | None = None
) -> tuple[Path, Path]:
    """Write notes/{lang}/summary.md + summary.json."""
    lang_n = normalize_lang(
        lang or str(data.get("summary_lang") or data.get("source_lang") or "en")
    )
    payload = dict(data)
    payload["summary_lang"] = lang_n
    folder = notes_locale_dir(out_dir, lang_n)
    folder.mkdir(parents=True, exist_ok=True)
    js = folder / "summary.json"
    md = folder / "summary.md"
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_notes_markdown(payload), encoding="utf-8")
    return js, md


def write_notes_index(out_dir: Path) -> Path:
    notes = Path(out_dir) / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    rows = ["# 总结 / Notes", "", "按语言分类：", ""]
    found: list[str] = []
    if notes.exists():
        for child in sorted(notes.iterdir(), key=lambda p: p.name.lower()):
            md = child / "summary.md"
            if child.is_dir() and md.exists():
                found.append(child.name)
                rows.append(f"- `{child.name}` — [{md.name}]({child.name}/{md.name})")
    if not found:
        rows.append("- （还没有分语种文档）")
    rows.append("")
    index = notes / "README.md"
    index.write_text("\n".join(rows), encoding="utf-8")
    return index


def locale_summary_json(work_dir: Path, stem: str, lang: str) -> Path:
    """Prefer notes/{lang}/summary.json, then legacy flat files."""
    tag = file_tag(lang)
    nested = notes_locale_dir(work_dir, tag) / "summary.json"
    if nested.exists():
        return nested
    flat = work_dir / f"{stem}_summary_{tag}.json"
    if flat.exists():
        return flat
    return nested


def sync_notes_docs(work_dir: Path, stem: str = "full_16k") -> list[Path]:
    """Rebuild notes/{lang}/summary.md from source + per-locale JSON."""
    work_dir = Path(work_dir)
    written: list[Path] = []
    source_js = work_dir / f"{stem}_summary.json"
    source = {}
    if source_js.exists():
        source = json.loads(source_js.read_text(encoding="utf-8-sig"))
        src_lang = coalesce_source_lang(
            str(source.get("summary_lang") or source.get("source_lang") or "en")
        )
        source["source_lang"] = coalesce_source_lang(
            str(source.get("source_lang") or src_lang)
        )
        source["summary_lang"] = src_lang
        _, md = write_locale_notes(source, work_dir, src_lang)
        written.append(md)

    seen = {p.parent.name for p in written}
    for path in sorted(work_dir.glob(f"{stem}_summary_*.json")):
        suffix = path.stem[len(stem) + 1 :]  # summary_zh
        if not suffix.startswith("summary_"):
            continue
        tag = suffix[len("summary_") :]
        if tag in seen:
            continue
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if source:
            data.setdefault("source_lang", source.get("source_lang"))
        data["summary_lang"] = tag
        _, md = write_locale_notes(data, work_dir, tag)
        written.append(md)
        seen.add(tag)

    write_notes_index(work_dir)
    return written


def _md_note_items(heading: str, items: list[dict[str, str]]) -> list[str]:
    if not items:
        return []
    lines = ["", f"## {heading}", ""]
    for item in items:
        if item.get("detail"):
            lines.append(f"- **{item['title']}** — {item['detail']}")
        else:
            lines.append(f"- {item['title']}")
    return lines


def merge_keypoints_into_summary(summary_path: Path, keypoints: dict) -> None:
    """Attach keypoints fields to an existing {stem}_summary.json."""
    data: dict = {}
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    data["keypoints_intro"] = keypoints.get("intro") or data.get("keypoints_intro") or ""
    incoming = normalize_key_points(keypoints.get("key_points"))
    if incoming:
        data["key_points"] = incoming
    data["keypoints_lang"] = keypoints.get("keypoints_lang") or data.get("keypoints_lang")
    summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_summary(data: dict, out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Write notes/{lang}/summary.md + summary.json only (no root copies)."""
    del stem  # stem lives on the work dir; notes are classified by language
    js, md = write_locale_notes(data, out_dir)
    write_notes_index(out_dir)
    return js, md


def load_notes_summary(work_dir: Path, stem: str, lang: str | None = None) -> dict:
    """Load summary JSON from notes/{lang}, then legacy flat files."""
    work_dir = Path(work_dir)
    candidates: list[Path] = []
    if lang:
        candidates.append(locale_summary_json(work_dir, stem, lang))
        candidates.append(work_dir / f"{stem}_summary.json")
    else:
        candidates.append(work_dir / f"{stem}_summary.json")
    notes = work_dir / "notes"
    if notes.is_dir():
        candidates.extend(sorted(notes.glob("*/summary.json")))
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    return {}


def purge_legacy_summary_files(work_dir: Path, stem: str = "full_16k") -> list[Path]:
    """Remove root-level summary copies now kept under notes/{lang}/."""
    work_dir = Path(work_dir)
    removed: list[Path] = []
    for name in (f"{stem}_summary.json", f"{stem}_summary.md", f"{stem}_keypoints.json"):
        path = work_dir / name
        if path.is_file():
            path.unlink()
            removed.append(path)
    for path in work_dir.glob(f"{stem}_summary_*.json"):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def parse_srt_or_txt(path: Path) -> tuple[list[dict], str]:
    if path.suffix.lower() == ".srt":
        segs = parse_srt(path)
        return segs, segments_plain_text(segs)
    return [], path.read_text(encoding="utf-8")


def clear_proxy_env() -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)


def add_common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--source-lang", default="zh", help="zh / en / ja / ko / auto")
    ap.add_argument(
        "--langs",
        default=DEFAULT_LANGS,
        help="site (16, default) | all | world | core | en,es,ar",
    )
    ap.add_argument(
        "--target-lang",
        default=None,
        help="Legacy single target; overrides --langs if set",
    )
    ap.add_argument(
        "--lang-workers",
        type=int,
        default=None,
        help=(
            "Parallel languages for translate (1–8). "
            f"Default from VITUAL_LANG_WORKERS or {DEFAULT_LANG_WORKERS}"
        ),
    )
    ap.add_argument(
        "--summary-lang",
        default=None,
        help="Defaults to source language",
    )
    ap.add_argument("--whisper-model", default=str(LOCAL_WHISPER))
    ap.add_argument(
        "--ollama-model",
        default=DEFAULT_TRANSLATE,
        help="Translate model (default translategemma:4b)",
    )
    ap.add_argument(
        "--chat-model",
        default=DEFAULT_CHAT,
        help="Chat model for glossary / post-ASR correct / summarize (default gemma4:e2b)",
    )
    ap.add_argument(
        "--llm-profile",
        default=None,
        help="LLM profile from llm.yaml / llm.example.yaml (local|tokenhub|openai|hybrid)",
    )
    ap.add_argument(
        "--llm-config",
        type=Path,
        default=None,
        help="Path to llm.yaml (default: subtitle_pipeline/llm.yaml or llm.example.yaml)",
    )
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument(
        "--multipass",
        dest="multipass",
        action="store_true",
        default=True,
        help="3-pass Whisper + vote/rule finalize (default on)",
    )
    ap.add_argument(
        "--no-multipass",
        dest="multipass",
        action="store_false",
        help="Single-pass Whisper only",
    )
    ap.add_argument("--hotwords", default=None, help="ASR initial_prompt / domain terms")
    ap.add_argument("--hotwords-file", type=Path, default=None)
    ap.add_argument(
        "--bvid",
        default=None,
        help="Bilibili BVxx: fetch danmaku/comments into hotwords",
    )
    ap.add_argument(
        "--relisten",
        dest="relisten",
        action="store_true",
        default=True,
        help="Re-ASR suspicious/repeated cues (default on with multipass)",
    )
    ap.add_argument(
        "--no-relisten",
        dest="relisten",
        action="store_false",
        help="Skip clip re-listen",
    )
    ap.add_argument(
        "--llm-correct",
        dest="llm_correct",
        action="store_true",
        default=None,
        help="After multipass, polish suspicious cues with --correct-model (default on for run)",
    )
    ap.add_argument(
        "--no-llm-correct",
        dest="llm_correct",
        action="store_false",
        help="Skip gemma ASR polish",
    )
    ap.add_argument(
        "--llm-all",
        action="store_true",
        help="With --llm-correct, polish every cue (not only suspicious)",
    )
    ap.add_argument(
        "--correct-model",
        default=None,
        help="ASR polish model; defaults to --chat-model (gemma4:e2b)",
    )
    ap.add_argument("--skip-translate", action="store_true")
    ap.add_argument("--skip-summary", action="store_true")
    ap.add_argument(
        "--skip-keypoints",
        action="store_true",
        help="Skip LLM key-point extraction from subtitles",
    )
    ap.add_argument("--reuse-audio", action="store_true")
    ap.add_argument("--from-srt", type=Path, default=None)
    ap.add_argument("--from-txt", type=Path, default=None)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--slice-start-sec",
        type=float,
        default=0.0,
        help="If ASR input is a clip cut from full media, add this offset to all cues",
    )
    ap.add_argument(
        "--sync-shift-ms",
        type=int,
        default=0,
        help="Global subtitle shift in ms after ASR (e.g. embed ads offset)",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    cmds = {"run", "transcribe", "translate", "summarize", "keypoints", "url", "shift-srt"}
    if argv and argv[0] not in cmds and not argv[0].startswith("-"):
        # bare URL → url command
        if re.match(r"https?://", argv[0], re.I):
            argv = ["url"] + argv
        else:
            argv = ["run"] + argv
    elif argv and argv[0].startswith("-"):
        argv = ["run"] + argv
    elif not argv:
        argv = ["run", "--help"]

    ap = argparse.ArgumentParser(
        description="Offline multipass ASR + TranslateGemma all-pack + gemma chat"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="transcribe + translate + summarize")
    p_run.add_argument("video", nargs="?", type=Path, default=None)
    add_common_args(p_run)
    p_run.set_defaults(llm_correct=True)

    p_tr = sub.add_parser("transcribe", help="audio/video -> source SRT/TXT")
    p_tr.add_argument("video", type=Path)
    add_common_args(p_tr)
    p_tr.set_defaults(skip_translate=True, skip_summary=True, llm_correct=True)

    p_tl = sub.add_parser("translate", help="existing SRT -> many languages")
    p_tl.add_argument("video", nargs="?", type=Path, default=None)
    add_common_args(p_tl)
    p_tl.set_defaults(skip_summary=True, llm_correct=False)

    p_sm = sub.add_parser("summarize", help="txt/srt -> summary in source language")
    p_sm.add_argument("video", nargs="?", type=Path, default=None)
    add_common_args(p_sm)
    p_sm.set_defaults(skip_translate=True, llm_correct=False)

    p_kp = sub.add_parser(
        "keypoints",
        help="srt/txt -> bullet key points (human-friendly) via chat model",
    )
    p_kp.add_argument("video", nargs="?", type=Path, default=None)
    add_common_args(p_kp)
    p_kp.set_defaults(skip_translate=True, skip_summary=True, llm_correct=False)

    p_url = sub.add_parser("url", help="yt-dlp fetch URL → multipass ASR (scheme A)")
    p_url.add_argument("url", help="Bilibili / Douyin / Kuaishou / YouTube / …")
    add_common_args(p_url)
    p_url.add_argument("--cookies-from-browser", default="chrome")
    p_url.add_argument("--no-cookies", action="store_true")
    p_url.add_argument("--cookies", type=Path, default=None)
    p_url.set_defaults(
        skip_translate=True,
        skip_summary=True,
        llm_correct=True,
        device="cpu",
    )

    p_shift = sub.add_parser("shift-srt", help="globally shift SRT timeline by milliseconds")
    p_shift.add_argument("--in", dest="in_srt", type=Path, required=True)
    p_shift.add_argument("--out", dest="out_srt", type=Path, default=None)
    p_shift.add_argument("--ms", dest="shift_ms", type=int, required=True)
    p_shift.add_argument("--reason", default="manual_shift")

    args = ap.parse_args(argv)
    # Resolve model aliases: polish model falls back to chat model
    if hasattr(args, "correct_model") and not getattr(args, "correct_model", None):
        args.correct_model = getattr(args, "chat_model", DEFAULT_CHAT) or DEFAULT_CHAT
    if getattr(args, "llm_correct", None) is None and args.cmd != "shift-srt":
        args.llm_correct = args.cmd in ("run", "transcribe", "url")
    return args


def chat_model(args: argparse.Namespace) -> str:
    """gemma4:e2b — glossary / post-correct / summarize / ASR polish."""
    return getattr(args, "chat_model", None) or DEFAULT_CHAT


def target_list(args: argparse.Namespace) -> list[str]:
    src = normalize_lang(args.source_lang)
    if args.target_lang:
        return resolve_targets(args.target_lang, src)
    return resolve_targets(args.langs, src)


def do_translate(
    args: argparse.Namespace,
    ollama: str,
    segs: list[dict],
    out_dir: Path,
    stem: str,
    glossary: dict,
) -> None:
    targets = target_list(args)
    if not targets:
        print("[translate] no target languages (pack empty or only source lang)")
        return
    workers = lang_workers(getattr(args, "lang_workers", None))
    print(
        f"[translate] {len(targets)} languages via {args.ollama_model} "
        f"(workers={workers}): {', '.join(targets)}"
    )
    job_subs_dir(out_dir)

    def _one(lang: str) -> None:
        tag = file_tag(lang)
        out_srt = locale_srt_path(out_dir, lang, stem)
        if out_srt.exists() and not args.force:
            locked_print(f"[skip] {out_srt.name} exists")
            return
        xx = translate_segments(
            ollama,
            args.ollama_model,
            segs,
            lang,
            source_lang=args.source_lang,
            glossary=glossary or None,
        )
        assert_timeline_aligned(segs, xx, label=f"{tag}.srt")
        warns = validate_srt_monotonic(xx)
        for w in warns:
            locked_print(f"[sync-warn] {tag}: {w}")
        write_srt(xx, out_srt)
        locked_print(f"[out] {out_srt}")

    if workers <= 1 or len(targets) <= 1:
        for lang in targets:
            _one(lang)
        return

    errors: list[tuple[str, BaseException]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, lang): lang for lang in targets}
        for fut in as_completed(futures):
            lang = futures[fut]
            try:
                fut.result()
            except BaseException as e:
                errors.append((lang, e))
                locked_print(f"[translate FAIL] {lang}: {type(e).__name__}: {e}")
    if errors:
        lang0, err0 = errors[0]
        raise RuntimeError(
            f"translate failed for {len(errors)} language(s); first={lang0}: {err0}"
        ) from err0


def do_keypoints(
    args: argparse.Namespace,
    ollama: str,
    text: str,
    out_dir: Path,
    stem: str,
) -> dict:
    lang = normalize_lang(args.summary_lang or args.source_lang)
    model = chat_model(args)
    print(f"[keypoints] language={lang} via {model}")
    data = summarize_keypoints(
        ollama,
        model,
        text,
        source_lang=args.source_lang,
        output_lang=lang,
    )
    summary_path = notes_locale_dir(out_dir, lang) / "summary.json"
    merge_keypoints_into_summary(summary_path, data)
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            write_summary(summary, out_dir, stem)
        except Exception:
            pass
    return data


def do_summary(
    args: argparse.Namespace,
    ollama: str,
    text: str,
    out_dir: Path,
    stem: str,
) -> None:
    summary_lang = normalize_lang(args.summary_lang or args.source_lang)
    model = chat_model(args)
    print(f"[summary] language={summary_lang} via {model}")
    data = summarize_text(
        ollama,
        model,
        text,
        source_lang=args.source_lang,
        summary_lang=summary_lang,
    )
    js, md = write_summary(data, out_dir, stem)
    print(f"[out] {js}")
    print(f"[out] {md}")
    already_listed = bool(normalize_key_points(data.get("key_points")))
    if not getattr(args, "skip_keypoints", False) and not already_listed:
        kp = summarize_keypoints(
            ollama,
            model,
            text,
            source_lang=args.source_lang,
            output_lang=summary_lang,
        )
        merge_keypoints_into_summary(js, kp)
        write_summary(json.loads(js.read_text(encoding="utf-8")), out_dir, stem)


def load_source_segments(args: argparse.Namespace) -> tuple[list[dict], Path, str]:
    if args.from_srt:
        srt_path = args.from_srt.resolve()
        if not srt_path.exists():
            raise FileNotFoundError(f"SRT not found: {srt_path}")
        if srt_path.parent.name == "subs":
            out_dir = (args.out_dir or srt_path.parent.parent).resolve()
            stem = "full_16k"
        else:
            out_dir = (args.out_dir or srt_path.parent).resolve()
            stem = srt_path.stem.rsplit("_", 1)[0] if "_" in srt_path.stem else srt_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        segs = parse_srt(srt_path)
        print(f"[srt] {len(segs)} cues from {srt_path}")
        return segs, out_dir, stem
    if args.from_txt:
        txt_path = args.from_txt.resolve()
        if not txt_path.exists():
            raise FileNotFoundError(f"TXT not found: {txt_path}")
        if txt_path.parent.name in {"subs", "media"}:
            out_dir = (args.out_dir or txt_path.parent.parent).resolve()
            stem = "full_16k"
        else:
            out_dir = (args.out_dir or txt_path.parent).resolve()
            stem = txt_path.stem.rsplit("_", 1)[0] if "_" in txt_path.stem else txt_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        return [], out_dir, stem
    raise FileNotFoundError("need --from-srt or --from-txt")


def transcribe_video(args: argparse.Namespace) -> tuple[list[dict], Path, str, Path]:
    if args.video is None:
        raise FileNotFoundError("video path required")
    video = args.video.resolve()
    if not video.exists():
        raise FileNotFoundError(f"Video not found: {video}")
    out_dir = (args.out_dir or video.parent / f"{video.stem}_subs").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    src_tag = file_tag(args.source_lang) if args.source_lang != "auto" else "en"
    media = job_media_dir(out_dir)
    job_subs_dir(out_dir)

    ffmpeg = find_ffmpeg()
    print(f"[env] ffmpeg={ffmpeg}")
    wav = media / f"{stem}.wav"
    if video.suffix.lower() == ".wav" and video.parent == out_dir and video != wav:
        if not wav.exists():
            shutil.move(str(video), str(wav))
        video = wav
    if is_usable_wav(wav) and args.reuse_audio:
        print(f"[audio] reuse -> {wav}")
    elif video.resolve() == wav.resolve():
        src = find_source_media(out_dir, wav=wav)
        if src is not None:
            print(f"[audio] extracting from {src.name} -> {wav}")
            extract_audio(ffmpeg, src, wav)
        elif is_usable_wav(wav):
            print(f"[audio] reuse -> {wav}")
        else:
            raise FileNotFoundError(
                f"no usable wav and no source media to extract from in {out_dir}"
            )
    else:
        print(f"[audio] extracting -> {wav}")
        extract_audio(ffmpeg, video, wav)
    if not is_usable_wav(wav):
        raise RuntimeError(
            f"invalid wav after extract ({wav.stat().st_size if wav.is_file() else 0} bytes): {wav}"
        )

    hotwords = load_hotwords(args.hotwords, args.hotwords_file)
    danmaku_meta = None
    if getattr(args, "bvid", None):
        try:
            from bili_assist import hotwords_from_danmaku

            dm_prompt, danmaku_meta = hotwords_from_danmaku(args.bvid)
            hotwords = "\n".join(x for x in (hotwords, dm_prompt) if x)
            dm_path = media / "danmaku_hotwords.json"
            dm_path.write_text(
                __import__("json").dumps(danmaku_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[out] {dm_path}")
        except Exception as e:
            print(f"[danmaku] skip ({e})")

    model_id = resolve_whisper_id(args.whisper_model)
    asr_lang = whisper_lang(args.source_lang)

    # Whisper first: unload chat models so CPU RAM is free
    stop_ollama_runners()

    detected_lang = "en"

    def _asr(device: str) -> tuple[list[dict], dict | None]:
        nonlocal detected_lang
        if args.multipass:
            compute = pick_compute_type(device, model_id)
            print(
                f"[whisper] multipass lang={args.source_lang} "
                f"on {device} compute={compute}..."
            )
            segs, raw = transcribe_multipass(
                wav,
                model_id,
                device,
                compute,
                asr_lang,
                hotwords=hotwords,
                release_cuda=release_cuda,
                relisten=bool(getattr(args, "relisten", True)),
            )
            return segs, raw
        print(f"[whisper] single-pass lang={args.source_lang} on {device}...")
        segs, detected_lang = transcribe(wav, args.whisper_model, device, args.source_lang)
        return segs, None

    try:
        segs, raw = _asr(args.device)
    except Exception as e:
        if args.device == "cuda":
            print(f"[whisper] CUDA failed ({e}); falling back to CPU")
            segs, raw = _asr("cpu")
        else:
            raise
    if not segs:
        raise RuntimeError("No speech segments detected.")

    if raw is not None:
        raw_path = media / "multipass_raw.json"
        save_raw(
            raw_path,
            raw,
            meta={
                "source_lang": args.source_lang,
                "model": model_id,
                "hotwords": bool(hotwords),
                "bvid": getattr(args, "bvid", None),
                "relisten": bool(getattr(args, "relisten", True)),
                "scheme": "A/B/C + suspicious relisten + vote/rule",
            },
        )
        print(f"[out] {raw_path}")

        if args.llm_correct:
            print(
                f"[correct] llm polish via {args.correct_model} "
                f"({'all cues' if args.llm_all else 'suspicious only'})"
            )
            ensure_ollama_model(args.correct_model, role="chat")
            triples = [
                {
                    "a": s.get("a") or nearest_text(raw["A"], s["start"], s["end"]),
                    "b": s.get("b") or nearest_text(raw["B"], s["start"], s["end"]),
                    "c": s.get("c") or nearest_text(raw["C"], s["start"], s["end"]),
                }
                for s in segs
            ]
            dm_hint = None
            if danmaku_meta and danmaku_meta.get("prompt"):
                dm_hint = danmaku_meta["prompt"]
            elif hotwords:
                dm_hint = hotwords
            segs = llm_correct_segments(
                segs,
                OLLAMA_HOST,
                args.correct_model,
                work_triples=triples,
                only_suspicious=not bool(getattr(args, "llm_all", False)),
                danmaku_hints=dm_hint,
            )

    segs = strip_internal_fields(segs)

    slice_start = float(getattr(args, "slice_start_sec", 0.0) or 0.0)
    sync_shift_ms = int(getattr(args, "sync_shift_ms", 0) or 0)
    if slice_start:
        print(f"[sync] apply slice_start_sec={slice_start}")
        segs = apply_slice_offset(segs, slice_start)
    if sync_shift_ms:
        print(f"[sync] apply sync_shift_ms={sync_shift_ms}")
        segs = shift_segments(segs, sync_shift_ms)
    for w in validate_srt_monotonic(segs):
        print(f"[sync-warn] {w}")

    if args.source_lang == "auto":
        args.source_lang = coalesce_source_lang(detected_lang)
        src_tag = file_tag(args.source_lang)
        print(f"[lang] auto → {args.source_lang}")
    else:
        src_tag = file_tag(args.source_lang)

    srt = locale_srt_path(out_dir, args.source_lang, stem)
    write_srt(segs, srt)
    sync_path = media / "sync_meta.json"
    write_sync_meta(
        sync_path,
        {
            "canonical_source": str(video),
            "source_wav": str(wav),
            "source_lang": args.source_lang,
            "slice_start_sec": slice_start,
            "sync_shift_ms": sync_shift_ms,
            "cue_count": len(segs),
            "multipass": bool(args.multipass),
        },
    )
    print(f"[out] {srt}")
    print(f"[out] {sync_path}")
    return segs, out_dir, stem, srt


def main(argv: list[str] | None = None) -> int:
    clear_proxy_env()
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    cmd = args.cmd

    if cmd != "shift-srt":
        from llm.factory import configure_llm

        configure_llm(
            profile=getattr(args, "llm_profile", None),
            config_path=getattr(args, "llm_config", None),
            chat_model=getattr(args, "chat_model", None) or getattr(args, "correct_model", None),
            translate_model=getattr(args, "ollama_model", None),
        )

    if cmd == "shift-srt":
        in_srt = args.in_srt.resolve()
        if not in_srt.exists():
            print(f"error: SRT not found: {in_srt}", file=sys.stderr)
            return 1
        out_srt = (args.out_srt or in_srt.with_name(in_srt.stem + f"_shift{args.shift_ms}.srt")).resolve()
        segs = parse_srt(in_srt)
        shifted = shift_segments(segs, args.shift_ms)
        write_srt(shifted, out_srt)
        meta = out_srt.with_name(out_srt.stem + "_sync_meta.json")
        write_sync_meta(
            meta,
            {
                "source_srt": str(in_srt),
                "out_srt": str(out_srt),
                "sync_shift_ms": args.shift_ms,
                "reason": args.reason,
                "cue_count": len(shifted),
            },
        )
        print(f"[out] {out_srt}")
        print(f"[out] {meta}")
        return 0

    try:
        from llm.factory import get_role_config

        needs_local_ollama = False
        for role in ("chat", "translate"):
            try:
                if get_role_config(role).provider == "ollama":
                    needs_local_ollama = True
                    break
            except Exception:
                needs_local_ollama = True
                break

        if cmd in ("transcribe", "url") and not args.llm_correct:
            needs_local_ollama = False

        if needs_local_ollama:
            ollama = find_ollama()
            print(f"[env] ollama={ollama}")
        else:
            ollama = shutil.which("ollama") or ""
            print("[env] ollama binary optional (cloud/API profile)")

        if cmd in ("transcribe", "url") and args.llm_correct:
            print(f"[env] llm-correct deferred until after ASR ({args.correct_model})")
        elif cmd == "summarize":
            print(f"[env] chat model deferred ({chat_model(args)})")
        elif cmd == "keypoints":
            print(f"[env] chat model deferred ({chat_model(args)})")
        elif cmd == "translate":
            print(f"[env] translate model deferred ({args.ollama_model})")
        elif cmd == "run":
            print(
                f"[env] models: chat={chat_model(args)} "
                f"translate={args.ollama_model} langs={args.langs}"
            )
    except Exception as e:
        # transcribe/url can skip ollama unless --llm-correct
        if cmd in ("transcribe", "url") and not getattr(args, "llm_correct", False):
            ollama = ""
            print(f"[env] ollama skipped: {e}")
        else:
            print(f"error: {e}", file=sys.stderr)
            return 1

    try:
        if cmd == "url":
            from fetch_media import detect_bvid, fetch_to_wav

            url = args.url
            # default out dir under downloads/<platform_id>
            if args.out_dir is None:
                bid = detect_bvid(url) or re.sub(r"[^\w.-]+", "_", url)[-40:]
                args.out_dir = (Path(__file__).resolve().parent / "downloads" / bid).resolve()
            meta = fetch_to_wav(
                url,
                Path(args.out_dir),
                cookies_from_browser=None
                if getattr(args, "no_cookies", False)
                else getattr(args, "cookies_from_browser", "chrome"),
                cookies_file=getattr(args, "cookies", None),
                force=bool(args.force),
            )
            wav = Path(meta["wav"])
            args.video = wav
            if not args.bvid and meta.get("bvid"):
                args.bvid = meta["bvid"]
            args.reuse_audio = True
            # force overwrite subtitle outputs for url runs unless user reused
            args.force = True
            segs, out_dir, stem, srt = transcribe_video(args)
            print("[done]", srt)
            return 0

        if cmd == "summarize" and (args.from_txt or args.from_srt):
            ensure_ollama_model(chat_model(args))
            segs, out_dir, stem = load_source_segments(args)
            if args.from_txt:
                text = args.from_txt.read_text(encoding="utf-8")
            else:
                text = segments_plain_text(segs)
            do_summary(args, ollama, text, out_dir, stem)
            print("[done]")
            return 0

        if cmd == "keypoints" and (args.from_txt or args.from_srt):
            ensure_ollama_model(chat_model(args))
            segs, out_dir, stem = load_source_segments(args)
            if args.from_txt:
                text = args.from_txt.read_text(encoding="utf-8")
            else:
                text = segments_plain_text(segs)
            do_keypoints(args, ollama, text, out_dir, stem)
            print("[done]")
            return 0

        if cmd == "translate" or (cmd == "run" and args.from_srt):
            if args.auto or not args.skip_summary:
                ensure_ollama_model(chat_model(args))
            if not args.skip_translate:
                ensure_ollama_model(args.ollama_model, role="translate")
            segs, out_dir, stem = load_source_segments(args)
            glossary: dict = {}
            if args.auto:
                glossary = auto_build_glossary(
                    ollama, chat_model(args), segs, args.source_lang
                )
                gloss_path = job_media_dir(out_dir) / "glossary.json"
                gloss_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[out] {gloss_path}")
                segs = correct_segments(
                    ollama, chat_model(args), segs, glossary, args.source_lang
                )
                write_srt(segs, locale_srt_path(out_dir, args.source_lang, stem))
            do_tr = not args.skip_translate
            do_sum = cmd == "run" and not args.skip_summary
            do_kp = (
                cmd == "run"
                and args.skip_summary
                and not getattr(args, "skip_keypoints", False)
            )

            def _notes_from_srt() -> None:
                if do_sum:
                    do_summary(args, ollama, segments_plain_text(segs), out_dir, stem)
                elif do_kp:
                    do_keypoints(args, ollama, segments_plain_text(segs), out_dir, stem)

            if do_tr and (do_sum or do_kp):
                print("[run] fork: source notes ∥ subtitle translate")
                errs: list[tuple[str, BaseException]] = []
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futs = {
                        pool.submit(
                            do_translate, args, ollama, segs, out_dir, stem, glossary
                        ): "translate",
                        pool.submit(_notes_from_srt): "notes",
                    }
                    for fut in as_completed(futs):
                        name = futs[fut]
                        try:
                            fut.result()
                        except BaseException as e:
                            errs.append((name, e))
                            locked_print(f"[run FAIL] {name}: {type(e).__name__}: {e}")
                if errs:
                    raise RuntimeError(
                        f"fork failed ({errs[0][0]}): {errs[0][1]}"
                    ) from errs[0][1]
            elif do_tr:
                do_translate(args, ollama, segs, out_dir, stem, glossary)
            elif do_sum or do_kp:
                _notes_from_srt()
            print("[done]")
            return 0

        segs, out_dir, stem, srt = transcribe_video(args)
        if cmd == "transcribe":
            print("[done]")
            return 0

        if args.auto or not args.skip_summary:
            ensure_ollama_model(chat_model(args))
        if not args.skip_translate:
            ensure_ollama_model(args.ollama_model, role="translate")
        glossary = {}
        if args.auto:
            glossary = auto_build_glossary(
                ollama, chat_model(args), segs, args.source_lang
            )
            gloss_path = job_media_dir(out_dir) / "glossary.json"
            gloss_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[out] {gloss_path}")
            segs = correct_segments(
                ollama, chat_model(args), segs, glossary, args.source_lang
            )
            write_srt(segs, locale_srt_path(out_dir, args.source_lang, stem))

        do_tr = cmd == "run" and not args.skip_translate
        do_sum = cmd == "run" and not args.skip_summary
        do_kp = cmd == "run" and args.skip_summary and not getattr(args, "skip_keypoints", False)

        def _notes_branch() -> None:
            if do_sum:
                do_summary(args, ollama, segments_plain_text(segs), out_dir, stem)
            elif do_kp:
                do_keypoints(args, ollama, segments_plain_text(segs), out_dir, stem)

        if do_tr and (do_sum or do_kp):
            print("[run] fork: source notes ∥ subtitle translate")
            errs: list[tuple[str, BaseException]] = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = {
                    pool.submit(do_translate, args, ollama, segs, out_dir, stem, glossary): "translate",
                    pool.submit(_notes_branch): "notes",
                }
                for fut in as_completed(futs):
                    name = futs[fut]
                    try:
                        fut.result()
                    except BaseException as e:
                        errs.append((name, e))
                        locked_print(f"[run FAIL] {name}: {type(e).__name__}: {e}")
            if errs:
                raise RuntimeError(f"fork failed ({errs[0][0]}): {errs[0][1]}") from errs[0][1]
        elif do_tr:
            do_translate(args, ollama, segs, out_dir, stem, glossary)
        elif do_sum or do_kp:
            _notes_branch()
        print("[done]")
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from pathlib import Path
from uuid import uuid4
from typing import Any
import os
import re
import shutil
import subprocess
import threading

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# -------------------------------------------------------------------
# Paths and folders
# -------------------------------------------------------------------

BASE = Path(__file__).resolve().parents[1]
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
FRONTEND_DIR = BASE.parent / "frontend" / "out"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------

app = FastAPI(title="ClipGenius AI API", version="0.3.0")

allowed_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

# Serve Next.js exported assets (CSS/JS). The frontend build creates this folder.
NEXT_ASSETS_DIR = FRONTEND_DIR / "_next"
if NEXT_ASSETS_DIR.exists():
    app.mount("/_next", StaticFiles(directory=str(NEXT_ASSETS_DIR)), name="next-static")

# -------------------------------------------------------------------
# In-memory job store and Whisper model cache
# -------------------------------------------------------------------

JOBS: dict[str, dict[str, Any]] = {}
MODEL = None
MODEL_LOCK = threading.Lock()


def get_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is not installed or not available on PATH.")
    return path


def get_whisper_model():
    """Load the small Whisper model once per running process."""
    global MODEL
    with MODEL_LOCK:
        if MODEL is None:
            from faster_whisper import WhisperModel

            model_name = os.getenv("WHISPER_MODEL", "tiny")
            device = os.getenv("WHISPER_DEVICE", "cpu")
            compute = os.getenv(
                "WHISPER_COMPUTE_TYPE",
                "int8" if device == "cpu" else "float16",
            )
            MODEL = WhisperModel(
                model_name,
                device=device,
                compute_type=compute,
                cpu_threads=1,
                num_workers=1,
            )
    return MODEL


# -------------------------------------------------------------------
# Video and caption helpers
# -------------------------------------------------------------------

def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            get_binary("ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not inspect the uploaded video.")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Could not read the video duration.") from exc


def clean_caption(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def choose_language(preference: str) -> str | None:
    if preference == "auto" or preference == "hinglish":
        # Hinglish may contain both Hindi and English; allow auto-detection.
        return None
    if preference in ("hi", "en"):
        return preference
    return None


def transcribe(path: Path, preference: str):
    """
    Transcribe without word-level timestamps to reduce processing overhead.
    This is a lighter configuration for small-memory hosting.
    """
    model = get_whisper_model()
    language = choose_language(preference)

    segments_iter, info = model.transcribe(
        str(path),
        language=language,
        task="transcribe",
        vad_filter=True,
        word_timestamps=True,
        beam_size=1,
        condition_on_previous_text=False,
    )

    segments = []
    for seg in segments_iter:
        text = clean_caption(seg.text)
        if text:
            segments.append(
                {
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "text": text,
                    "words": [
                        {
                            "start": float(word.start),
                            "end": float(word.end),
                            "text": clean_caption(word.word),
                        }
                        for word in (seg.words or [])
                        if word.start is not None and word.end is not None
                    ],
                }
            )

    detected_language = getattr(info, "language", "unknown")
    return segments, detected_language


HOOK_TERMS = {
    "secret", "mistake", "important", "why", "how", "never", "always",
    "best", "worst", "truth", "learn", "tip", "reason", "problem",
    "solution", "amazing", "सबसे", "क्यों", "कैसे", "जरूरी", "गलती",
    "सच", "तरीका", "ध्यान", "याद", "मतलब", "समाधान", "फायदा",
}


def score_candidate(text: str, duration: float, segments=None) -> float:
    """Heuristic highlight score using speech density, hooks and sentence cues."""
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return 0.0

    unique_ratio = len(set(words)) / max(len(words), 1)
    speech_rate = len(words) / max(duration, 1)
    hook_hits = sum(1 for word in words if word in HOOK_TERMS)
    score = min(speech_rate * 1.25, 12.0)
    score += min(hook_hits * 2.0, 10.0)
    score += 2.5 if "?" in text or "？" in text else 0.0
    score += 1.5 if "!" in text or "！" in text else 0.0
    score += 1.0 if len(words) >= 18 else 0.0
    score += min(unique_ratio * 2.0, 2.0)

    if segments:
        pauses = sum(
            1 for left, right in zip(segments, segments[1:])
            if 0 <= right["start"] - left["end"] <= 0.8
        )
        score += min(pauses * 0.15, 1.5)

    return round(score, 2)


def build_candidates(segments, total_duration: float, requested_duration: int):
    """Score overlapping time windows, then keep the strongest non-overlapping moments."""
    target = min(float(requested_duration), total_duration)
    if target < 8:
        return []

    stride = max(3.0, min(6.0, target / 5.0))
    starts = []
    cursor = 0.0
    while cursor + min(target, total_duration) <= total_duration + 0.01:
        starts.append(cursor)
        cursor += stride
    final_start = max(0.0, total_duration - target)
    if not starts or abs(starts[-1] - final_start) > 1.0:
        starts.append(final_start)

    candidates = []
    for start in starts:
        end = min(total_duration, start + target)
        selected = [
            item for item in segments
            if item["end"] > start and item["start"] < end
        ]
        text = " ".join(item["text"] for item in selected)
        if not text.strip() or len(re.findall(r"\w+", text, flags=re.UNICODE)) < 5:
            continue

        # Trim quiet leading/trailing space while preserving a little context.
        first_speech = min(item["start"] for item in selected)
        last_speech = max(item["end"] for item in selected)
        clip_start = max(0.0, min(start + 1.0, first_speech - 0.6))
        clip_end = min(total_duration, max(end - 1.0, last_speech + 0.8))
        if clip_end - clip_start < min(8.0, target * 0.5):
            clip_start, clip_end = start, end

        candidates.append({
            "start": clip_start,
            "end": clip_end,
            "text": text,
            "score": score_candidate(text, clip_end - clip_start, selected),
        })

    candidates.sort(key=lambda item: item["score"], reverse=True)
    picked = []
    for item in candidates:
        if all(
            item["end"] <= previous["start"] + 3
            or item["start"] >= previous["end"] - 3
            for previous in picked
        ):
            picked.append(item)
        if len(picked) >= 10:
            break

    return picked


def srt_time(seconds: float) -> str:
    milliseconds = int(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def write_srt(segments, start: float, end: float, target: Path):
    """Write captions using Whisper word timings, with a safe phrase-timing fallback."""
    lines = []
    index = 1

    def add_caption(text: str, caption_start: float, caption_end: float):
        nonlocal index
        text = clean_caption(text)
        caption_start = max(start, caption_start)
        caption_end = min(end, caption_end)
        if not text or caption_end <= caption_start:
            return
        lines.extend([
            str(index),
            f"{srt_time(caption_start - start)} --> {srt_time(caption_end - start)}",
            text,
            "",
        ])
        index += 1

    for seg in segments:
        seg_start = max(start, seg["start"])
        seg_end = min(end, seg["end"])
        if seg_end <= seg_start:
            continue

        words = [
            word for word in (seg.get("words") or [])
            if word.get("end", 0) > start and word.get("start", 0) < end
            and word.get("text")
        ]

        if words:
            group = []
            group_chars = 0
            for word in words:
                token = word["text"]
                if group and (len(group) >= 4 or group_chars + len(token) > 30):
                    add_caption(
                        "".join(item["text"] for item in group),
                        group[0]["start"],
                        group[-1]["end"],
                    )
                    group = []
                    group_chars = 0
                group.append(word)
                group_chars += len(token)
            if group:
                add_caption(
                    "".join(item["text"] for item in group),
                    group[0]["start"],
                    group[-1]["end"],
                )
            continue

        # Fallback for segments where the recognizer did not return word timings.
        caption = clean_caption(seg["text"])
        tokens = caption.split()
        chunks = []
        current = []
        current_len = 0
        for token in tokens:
            extra = len(token) + (1 if current else 0)
            if current and (len(current) >= 6 or current_len + extra > 34):
                chunks.append(" ".join(current))
                current = [token]
                current_len = len(token)
            else:
                current.append(token)
                current_len += extra
        if current:
            chunks.append(" ".join(current))

        weights = [max(len(chunk), 1) for chunk in chunks]
        total_weight = sum(weights) or 1
        span = seg_end - seg_start
        elapsed = 0.0
        for chunk, weight in zip(chunks, weights):
            phrase_duration = span * weight / total_weight
            phrase_start = seg_start + elapsed
            phrase_end = min(seg_end, phrase_start + phrase_duration)
            add_caption(chunk, phrase_start, phrase_end)
            elapsed += phrase_duration

    target.write_text("\n".join(lines), encoding="utf-8-sig")


def escape_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    return value



def render_clip(
    source: Path,
    job_dir: Path,
    index: int,
    candidate: dict,
    all_segments,
):
    ffmpeg = get_binary("ffmpeg")
    start = candidate["start"]
    end = candidate["end"]

    srt_path = job_dir / f"clip_{index:02d}.srt"
    output_path = job_dir / f"clip_{index:02d}.mp4"

    write_srt(all_segments, start, end, srt_path)

    subtitle_filter = (
        f"subtitles='{escape_filter_path(srt_path)}':"
        "force_style='FontName=Noto Sans Devanagari,FontSize=18,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=100'"
    )
    # Keep rendering stable on the 512 MB free instance.
    # Face-aware sampling is deferred until sufficient memory is available.
    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,{subtitle_filter}"
    )

    command = [
        ffmpeg,
        "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(end - start),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg export failed: {result.stderr[-1800:]}")

    return {
        "name": output_path.name,
        "url": f"/outputs/{job_dir.name}/{output_path.name}",
        "start_seconds": round(start, 1),
        "duration_seconds": round(end - start, 1),
        "score": candidate["score"],
        "framing_mode": "center-crop",
        "transcript_preview": candidate["text"][:180],
    }


# -------------------------------------------------------------------
# Background processing
# -------------------------------------------------------------------

def process_job(job_id: str, count: int, duration: int, language: str):
    source_matches = list(UPLOAD_DIR.glob(f"{job_id}.*"))
    if not source_matches:
        JOBS[job_id].update(
            status="failed",
            progress=100,
            error="Uploaded source file was not found.",
            message="Processing failed.",
        )
        return

    source = source_matches[0]

    try:
        JOBS[job_id].update(
            status="transcribing",
            progress=10,
            message="Transcribing speech with Whisper…",
        )

        total_duration = probe_duration(source)
        segments, detected = transcribe(source, language)

        if not segments:
            raise RuntimeError(
                "No speech was detected. Try a video with clearer spoken audio."
            )

        JOBS[job_id].update(
            status="analysing",
            progress=40,
            detected_language=detected,
            transcript_segments=len(segments),
            message="Ranking promising moments…",
        )

        candidates = build_candidates(segments, total_duration, duration)
        if not candidates:
            raise RuntimeError(
                "Could not find enough speech segments to create clips."
            )

        selected = candidates[:count]
        job_dir = OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for index, candidate in enumerate(selected, 1):
            JOBS[job_id].update(
                status="rendering",
                progress=40 + int(50 * index / len(selected)),
                message=f"Rendering clip {index} of {len(selected)}…",
            )
            results.append(
                render_clip(source, job_dir, index, candidate, segments)
            )

        JOBS[job_id].update(
            status="completed",
            progress=100,
            message="Your clips are ready.",
            detected_language=detected,
            clips=results,
            note=(
                "Clips are ranked using transcript-based heuristics. "
                "Portrait framing currently uses a stable center crop. "
                "Virality is not guaranteed. Hinglish transcription quality "
                "depends on the speech and Whisper model."
            ),
        )

    except Exception as exc:
        JOBS[job_id].update(
            status="failed",
            progress=100,
            error=str(exc),
            message="Processing failed.",
        )


# -------------------------------------------------------------------
# API routes
# -------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": "ClipGenius AI",
        "engine": "FFmpeg + faster-whisper",
    }


@app.get("/")
def homepage():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return {
            "app": "ClipGenius AI API",
            "status": "ok",
            "message": "Frontend build not found.",
        }
    return FileResponse(index_file)


@app.post("/api/import-url")
def import_video_url(payload: dict[str, str]):
    """Import a direct video file URL from an authorized source."""
    import ipaddress
    import socket
    from urllib.parse import urlparse, unquote
    import requests

    source_url = (payload.get("url") or "").strip()
    if not source_url or len(source_url) > 2048:
        raise HTTPException(status_code=400, detail="Please provide a valid direct video URL.")

    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Use a secure HTTPS direct video-file URL.")

    host = parsed.hostname.lower()
    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        raise HTTPException(status_code=422, detail="A YouTube watch link is a video page, not a direct video file. Use an authorized direct MP4/MOV URL or upload the original video.")

    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("Non-public host")
    except Exception:
        raise HTTPException(status_code=400, detail="This video URL host is not publicly accessible.")

    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v"}:
        raise HTTPException(status_code=400, detail="The direct URL must end in .mp4, .mov, or .m4v.")

    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "150"))
    max_upload_bytes = max_upload_mb * 1024 * 1024
    job_id = uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{job_id}{suffix}"
    total_bytes = 0

    try:
        with requests.get(source_url, stream=True, allow_redirects=False, timeout=(10, 30)) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(status_code=400, detail="Redirected URLs are not supported. Use the final direct file URL.")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                raise HTTPException(status_code=415, detail="The URL did not return a recognized video file.")
            declared_size = response.headers.get("content-length")
            if declared_size and int(declared_size) > max_upload_bytes:
                raise HTTPException(status_code=413, detail=f"Video exceeds the {max_upload_mb} MB import limit.")
            with saved_path.open("wb") as buffer:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_upload_bytes:
                        raise HTTPException(status_code=413, detail=f"Video exceeds the {max_upload_mb} MB import limit.")
                    buffer.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="The video URL returned an empty file.")
        probe_duration(saved_path)
    except HTTPException:
        saved_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not import this video URL: {exc}")

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "uploaded",
        "progress": 0,
        "filename": saved_path.name,
        "message": "Direct video import complete. Ready to generate.",
    }
    return JOBS[job_id]


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    allowed_extensions = {".mp4", ".mov", ".m4v"}
    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Please upload an MP4, MOV, or M4V video.",
        )

    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "150"))
    max_upload_bytes = max_upload_mb * 1024 * 1024

    job_id = uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{job_id}{suffix}"
    total_bytes = 0

    try:
        with saved_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_upload_bytes:
                    saved_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Video exceeds the {max_upload_mb} MB upload limit.",
                    )
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")
    finally:
        await file.close()

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "uploaded",
        "progress": 0,
        "filename": file.filename,
        "message": "Upload complete. Ready to process.",
    }
    return JOBS[job_id]


@app.post("/api/jobs/{job_id}/generate")
def generate(
    job_id: str,
    background_tasks: BackgroundTasks,
    count: int = 5,
    duration: int = 30,
    language: str = "auto",
):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found.")

    if count < 5 or count > 10:
        raise HTTPException(
            status_code=400,
            detail="Choose between 5 and 10 shorts.",
        )

    if duration not in (15, 30, 45, 60, 90):
        raise HTTPException(
            status_code=400,
            detail="Duration must be 15, 30, 45, 60, or 90 seconds.",
        )

    if language not in ("auto", "hi", "en", "hinglish"):
        raise HTTPException(
            status_code=400,
            detail="Language must be auto, hi, en, or hinglish.",
        )

    JOBS[job_id].update(
        status="queued",
        progress=2,
        message="Processing queued.",
    )
    background_tasks.add_task(process_job, job_id, count, duration, language)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found.")
    return dict(JOBS[job_id])

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
        word_timestamps=False,
        beam_size=1,
        condition_on_previous_text=False,
        cpu_threads=1,
        num_workers=1,
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
                    "words": [],
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


def score_candidate(text: str, duration: float) -> float:
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return 0.0

    score = min(len(words) / max(duration, 1) * 8, 10)
    score += min(sum(1 for word in words if word in HOOK_TERMS) * 1.8, 8)
    score += 2.0 if "?" in text or "？" in text else 0
    score += 1.5 if "!" in text else 0
    score += 1.0 if len(text) > 100 else 0
    return round(score, 2)


def build_candidates(segments, total_duration: float, requested_duration: int):
    target = min(float(requested_duration), total_duration)
    candidates = []

    for seg in segments:
        start = max(0.0, seg["start"] - 1.0)
        end = min(total_duration, start + target)

        if end - start < min(8.0, target * 0.5):
            continue

        selected = [
            item for item in segments
            if item["end"] > start and item["start"] < end
        ]
        text = " ".join(item["text"] for item in selected)
        if not text.strip():
            continue

        candidates.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "score": score_candidate(text, end - start),
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)

    picked = []
    for item in candidates:
        no_near_duplicate = all(
            item["end"] <= previous["start"] + 4
            or item["start"] >= previous["end"] - 4
            for previous in picked
        )
        if no_near_duplicate:
            picked.append(item)
        if len(picked) >= 20:
            break

    return picked


def srt_time(seconds: float) -> str:
    milliseconds = int(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def write_srt(segments, start: float, end: float, target: Path):
    lines = []
    index = 1

    for seg in segments:
        clip_start = max(start, seg["start"])
        clip_end = min(end, seg["end"])
        if clip_end <= clip_start:
            continue

        caption = clean_caption(seg["text"])
        if not caption:
            continue

        lines.extend(
            [
                str(index),
                f"{srt_time(clip_start - start)} --> {srt_time(clip_end - start)}",
                caption,
                "",
            ]
        )
        index += 1

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
        "force_style='FontName=Arial,FontSize=18,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=100'"
    )
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

    if count < 1 or count > 20:
        raise HTTPException(
            status_code=400,
            detail="Short count must be between 1 and 20.",
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

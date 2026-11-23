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


@app.post("/api/opus/projects")
def create_opus_project(payload: dict[str, Any]):
    """Submit a supported public video URL to the configured OpusClip API."""
    import requests
    from urllib.parse import urlparse

    api_key = os.getenv("OPUSCLIP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="YouTube link processing is not connected yet. Add OPUSCLIP_API_KEY in Render Environment settings."
        )

    video_url = str(payload.get("url", "")).strip()
    if not video_url or len(video_url) > 2048:
        raise HTTPException(status_code=400, detail="Paste a valid video URL.")

    parsed = urlparse(video_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Please use a valid HTTPS video link.")

    host = parsed.hostname.lower()
    is_youtube = (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )
    if not is_youtube:
        raise HTTPException(status_code=400, detail="This integration currently accepts YouTube video links only.")

    duration = int(payload.get("duration", 30))
    duration_ranges = {
        15: [[0, 30]],
        30: [[0, 30]],
        45: [[30, 60]],
        60: [[30, 60]],
        90: [[60, 90]],
    }
    if duration not in duration_ranges:
        duration = 30

    language = str(payload.get("language", "auto"))
    if language not in ("auto", "hi", "en"):
        language = "auto"

    request_body = {
        "videoUrl": video_url,
        "curationPref": {
            "clipDurations": duration_ranges[duration],
            "genre": "Auto",
        },
        "importPref": {"sourceLang": language},
        "renderPref": {
            "layoutAspectRatio": "portrait",
            "enableCrop": True,
            "enableCaption": True,
            "enableUppercase": False,
            "enableHighlight": True,
            "enableEmoji": False,
        },
    }

    try:
        response = requests.post(
            "https://api.opus.pro/api/clip-projects",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=45,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text[:500]}
        if response.status_code >= 400:
            detail = data.get("errorMessage") or data.get("message") or data.get("error") or f"OpusClip API returned HTTP {response.status_code}."
            raise HTTPException(status_code=502, detail=str(detail)[:700])
        project_id = data.get("projectId") or data.get("id") or (data.get("data") or {}).get("projectId") or (data.get("data") or {}).get("id")
        if not project_id:
            raise HTTPException(status_code=502, detail="OpusClip accepted an unexpected response. Please check API access and try again.")
        return {
            "project_id": project_id,
            "stage": data.get("stage") or (data.get("data") or {}).get("stage") or "QUEUED",
            "message": "Video submitted to the AI clipping service.",
        }
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach the AI clipping service: {exc}")


@app.get("/api/opus/projects/{project_id}")
def get_opus_project(project_id: str):
    import requests
    api_key = os.getenv("OPUSCLIP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="OpusClip API key is not configured on the server.")
    try:
        response = requests.get(
            f"https://api.opus.pro/api/clip-projects/{project_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        data = response.json()
        if response.status_code >= 400:
            detail = data.get("errorMessage") or data.get("message") or f"OpusClip API returned HTTP {response.status_code}."
            raise HTTPException(status_code=502, detail=str(detail)[:700])
        return data
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not check AI project status: {exc}")


@app.get("/api/opus/projects/{project_id}/clips")
def get_opus_project_clips(project_id: str):
    import requests
    api_key = os.getenv("OPUSCLIP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="OpusClip API key is not configured on the server.")
    try:
        response = requests.get(
            "https://api.opus.pro/api/exportable-clips",
            params={"q": "findByProjectId", "projectId": project_id, "pageNum": 1, "pageSize": 50},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        data = response.json()
        if response.status_code >= 400:
            detail = data.get("errorMessage") or data.get("message") or f"OpusClip API returned HTTP {response.status_code}."
            raise HTTPException(status_code=502, detail=str(detail)[:700])
        if isinstance(data, dict):
            data = data.get("data", data.get("list", []))
            if isinstance(data, dict):
                data = data.get("list", [])
        return {"clips": data if isinstance(data, list) else []}
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not retrieve generated clips: {exc}")


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

from pathlib import Path
from uuid import uuid4
from typing import Any
import os, shutil, subprocess, threading, re, json, time

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parents[1]
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipGenius AI API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
FRONTEND_DIR = BASE.parent / "frontend" / "out"
app.mount("/_next", StaticFiles(directory=str(FRONTEND_DIR / "_next")), name="next-static")
JOBS: dict[str, dict[str, Any]] = {}
MODEL = None
MODEL_LOCK = threading.Lock()

def get_binary(name: str):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is not installed or not available on PATH.")
    return path

def get_whisper_model():
    global MODEL
    with MODEL_LOCK:
        if MODEL is None:
            from faster_whisper import WhisperModel
            model_name = os.getenv("WHISPER_MODEL", "tiny")
            device = os.getenv("WHISPER_DEVICE", "cpu")
            compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
            MODEL = WhisperModel(model_name, device=device, compute_type=compute)
        return MODEL

def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [get_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("Could not inspect the uploaded video.")
    return float(result.stdout.strip())

def clean_caption(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def choose_language(preference: str, detected: str | None) -> str | None:
    if preference == "auto":
        return None
    if preference in ("hi", "en"):
        return preference
    # Hinglish is code-switched; let Whisper detect the speech language.
    return None

def transcribe(path: Path, preference: str):
def transcribe(path: Path, preference: str):
    model = get_whisper_model()
    lang = choose_language(preference, None)

    segments_iter, info = model.transcribe(
        str(path),
        language=lang,
        task="transcribe",
        vad_filter=True,
        word_timestamps=False,
        beam_size=1,
        condition_on_previous_text=False,
        cpu_threads=1,
        num_workers=1
    )

    segments = []
    for seg in segments_iter:
        text = clean_caption(seg.text)
        if text:
            segments.append({
                "start": float(seg.start),
                "end": float(seg.end),
                "text": text,
                "words": []
            })

    return segments, getattr(info, "language", "unknown")
)
    )
    segments = []
    for seg in segments_iter:
        text = clean_caption(seg.text)
        if text:
            words = []
            if getattr(seg, "words", None):
                for word in seg.words:
                    words.append({
                        "start": float(word.start), "end": float(word.end),
                        "text": word.word
                    })
            segments.append({
                "start": float(seg.start), "end": float(seg.end),
                "text": text, "words": words
            })
    return segments, getattr(info, "language", "unknown")

HOOK_TERMS = {
    "secret", "mistake", "important", "why", "how", "never", "always",
    "best", "worst", "truth", "learn", "tip", "reason", "problem",
    "solution", "amazing", "सबसे", "क्यों", "कैसे", "जरूरी", "गलती",
    "सच", "तरीका", "ध्यान", "याद", "मतलब", "समाधान", "फायदा"
}

def score_candidate(text: str, duration: float) -> float:
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return 0.0
    score = min(len(words) / max(duration, 1) * 8, 10)
    score += min(sum(1 for w in words if w in HOOK_TERMS) * 1.8, 8)
    score += 2.0 if "?" in text or "？" in text else 0
    score += 1.5 if "!" in text else 0
    score += 1.0 if len(text) > 100 else 0
    return round(score, 2)

def build_candidates(segments, total_duration: float, requested_duration: int):
    # Create candidate windows beginning at transcript segment boundaries.
    target = min(float(requested_duration), total_duration)
    candidates = []
    for seg in segments:
        start = max(0.0, seg["start"] - 1.0)
        end = min(total_duration, start + target)
        if end - start < min(8.0, target * 0.5):
            continue
        selected = [s for s in segments if s["end"] > start and s["start"] < end]
        text = " ".join(s["text"] for s in selected)
        if not text.strip():
            continue
        candidates.append({
            "start": start, "end": end, "text": text,
            "score": score_candidate(text, end - start)
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    picked = []
    for item in candidates:
        # Avoid near-duplicate overlapping clips.
        if all(item["end"] <= p["start"] + 4 or item["start"] >= p["end"] - 4 for p in picked):
            picked.append(item)
        if len(picked) >= 20:
            break
    return picked

def srt_time(seconds: float) -> str:
    ms = int(max(0, seconds) * 1000)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{milli:03}"

def write_srt(segments, start: float, end: float, target: Path):
    lines, idx = [], 1
    for seg in segments:
        a, b = max(start, seg["start"]), min(end, seg["end"])
        if b <= a:
            continue
        caption = clean_caption(seg["text"])
        if not caption:
            continue
        lines.extend([str(idx), f"{srt_time(a-start)} --> {srt_time(b-start)}", caption, ""])
        idx += 1
    target.write_text("\n".join(lines), encoding="utf-8-sig")

def escape_filter_path(path: Path) -> str:
    # FFmpeg filter parser escaping for Windows drive letters and punctuation.
    value = path.resolve().as_posix()
    value = value.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")
    return value

def render_clip(source: Path, job_dir: Path, idx: int, candidate: dict, all_segments, duration: int):
    ffmpeg = get_binary("ffmpeg")
    start, end = candidate["start"], candidate["end"]
    srt = job_dir / f"clip_{idx:02d}.srt"
    out = job_dir / f"clip_{idx:02d}.mp4"
    write_srt(all_segments, start, end, srt)
    subtitle_filter = f"subtitles='{escape_filter_path(srt)}':force_style='FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=100'"
    vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,{subtitle_filter}"
    cmd = [
        ffmpeg, "-y", "-ss", str(start), "-i", str(source), "-t", str(end-start),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(f"FFmpeg export failed: {run.stderr[-1800:]}")
    return {
        "name": out.name,
        "url": f"/outputs/{job_dir.name}/{out.name}",
        "start_seconds": round(start, 1),
        "duration_seconds": round(end-start, 1),
        "score": candidate["score"],
        "transcript_preview": candidate["text"][:180]
    }

def process_job(job_id: str, count: int, duration: int, language: str):
    source_matches = list(UPLOAD_DIR.glob(f"{job_id}.*"))
    if not source_matches:
        JOBS[job_id].update(status="failed", error="Uploaded source file was not found.")
        return
    source = source_matches[0]
    try:
        JOBS[job_id].update(status="transcribing", progress=10, message="Transcribing speech with Whisper…")
        total = probe_duration(source)
        segments, detected = transcribe(source, language)
        if not segments:
            raise RuntimeError("No speech was detected. Try a video with clearer spoken audio.")
        JOBS[job_id].update(status="analysing", progress=40, detected_language=detected,
                             transcript_segments=len(segments), message="Ranking promising moments…")
        candidates = build_candidates(segments, total, duration)
        if not candidates:
            raise RuntimeError("Could not find enough speech segments to create clips.")
        selected = candidates[:count]
        job_dir = OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i, candidate in enumerate(selected, 1):
            JOBS[job_id].update(status="rendering", progress=40 + int(50*i/len(selected)),
                                message=f"Rendering clip {i} of {len(selected)}…")
            results.append(render_clip(source, job_dir, i, candidate, segments, duration))
        JOBS[job_id].update(
            status="completed", progress=100, message="Your clips are ready.",
            detected_language=detected, clips=results,
            note="Clips are ranked using transcript-based heuristics. Virality is not guaranteed. Hinglish code-switching quality depends on the speech and Whisper model."
        )
    except Exception as exc:
        JOBS[job_id].update(status="failed", progress=100, error=str(exc), message="Processing failed.")

@app.get("/api/health")
def health():
    return {"status": "ok", "app": "ClipGenius AI", "engine": "FFmpeg + faster-whisper"}

@app.get("/")
def homepage():
    from fastapi.responses import FileResponse
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return {"app": "ClipGenius AI API", "status": "ok", "message": "Frontend build not found."}
    return FileResponse(index)

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    allowed = {".mp4", ".mov", ".m4v"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail="Please upload an MP4, MOV, or M4V video.")
    job_id = uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{job_id}{suffix}"
    try:
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")
    JOBS[job_id] = {"job_id": job_id, "status": "uploaded", "progress": 0,
                    "filename": file.filename, "message": "Upload complete. Ready to process."}
    return JOBS[job_id]

@app.post("/api/jobs/{job_id}/generate")
def generate(job_id: str, background_tasks: BackgroundTasks,
             count: int = 5, duration: int = 30, language: str = "auto"):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found.")
    if count < 1 or count > 20:
        raise HTTPException(status_code=400, detail="Short count must be between 1 and 20.")
    if duration not in (15, 30, 45, 60, 90):
        raise HTTPException(status_code=400, detail="Duration must be 15, 30, 45, 60, or 90 seconds.")
    if language not in ("auto", "hi", "en", "hinglish"):
        raise HTTPException(status_code=400, detail="Language must be auto, hi, en, or hinglish.")
    JOBS[job_id].update(status="queued", progress=2, message="Processing queued.")
    background_tasks.add_task(process_job, job_id, count, duration, language)
    return {"job_id": job_id, "status": "queued"}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found.")
    data = dict(JOBS[job_id])
    for clip in data.get("clips", []):
        clip["url"] = clip["url"]
    return data

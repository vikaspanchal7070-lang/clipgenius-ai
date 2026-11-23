# ClipGenius AI — AI Shorts Maker MVP

## Included in this milestone
- Next.js dark-premium web dashboard
- FastAPI video upload and background processing job
- faster-whisper local transcription (Hindi/English; Hinglish is best-effort code-switch transcription)
- Transcript-based highlight candidate scoring
- FFmpeg 9:16 video rendering
- SRT subtitles burned into exported MP4 clips
- Job progress endpoint and clip downloads

## Requirements
- Python 3.10+
- Node.js 20+
- FFmpeg + FFprobe installed and available on PATH
- Internet connection on first run to download the Whisper model

## Start backend (Windows PowerShell)
```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Start frontend (second terminal)
```powershell
cd frontend
npm install
$env:NEXT_PUBLIC_API_URL="http://localhost:8000"
npm run dev
```
Open http://localhost:3000

## Whisper model settings
Default model is `base` (CPU int8). For better accuracy, try `small`; it will be slower and use more memory.
Set `WHISPER_MODEL`, `WHISPER_DEVICE`, and `WHISPER_COMPUTE_TYPE` as environment variables.

## Limitations in this MVP
- The first clip ranking is transcript-based heuristics, not a trained virality predictor or guaranteed viral score.
- Hinglish code-switching is best-effort; Whisper may normalize some words into Hindi or English.
- Vertical crop is centered, not face-aware. Smart speaker tracking is a future milestone.
- YouTube URL ingestion, accounts, cloud storage, payments, production job queue, retention cleanup and multi-worker job persistence are not included yet.
- Jobs are stored in process memory. Use Redis/Celery and persistent database before public launch.
- Do not process videos unless you own them or have permission. YouTube import must respect platform terms and authorized access.

## Security and deployment notes
This is a local development MVP, not production-hardened. Add upload size limits, malware scanning, authentication, rate limiting, object storage, database-backed job state, cleanup policies, and private download URLs before public release.

## Free-tier cloud test deployment (Render)
1. Push this project folder to a GitHub repository.
2. In Render, create a new Blueprint and connect that repository.
3. Select the `render.yaml` blueprint and deploy the `free` service.
4. Wait for the first build and Whisper tiny model download. The service URL will be shown by Render.
5. Open `https://YOUR-RENDER-SERVICE.onrender.com` and test with a short MP4 first.

### Free-tier caveats
- Render free web services sleep after 15 minutes without traffic and can take about a minute to wake.
- Free service storage is ephemeral. Uploaded source videos and generated clips may disappear after restarts, redeploys or spin-down.
- Free instances have limited memory. This deployment uses Whisper `tiny` on CPU to reduce memory use; processing may be slow and transcription accuracy lower than larger models.
- Keep test uploads short and small. This is a public demo, not suitable for private or sensitive videos.
- Do not add paid services or enter payment details for this test. If Render asks for a paid plan, stop and do not upgrade.

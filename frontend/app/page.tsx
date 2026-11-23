
"use client";

import { useState } from "react";
import {
  Sparkles,
  Play,
  UploadCloud,
  Link2,
  Scissors,
  Captions,
  Smartphone,
  Layers3,
  Zap,
  CheckCircle2,
  Film,
  Loader2,
  Download,
  WandSparkles,
} from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL || "";

type Clip = {
  name: string;
  url: string;
  start_seconds: number;
  duration_seconds: number;
  score: number;
  transcript_preview: string;
};

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState("");
  const [count, setCount] = useState("5");
  const [duration, setDuration] = useState("30");
  const [language, setLanguage] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [clips, setClips] = useState<Clip[]>([]);
  const [tab, setTab] = useState<"upload" | "youtube">("upload");
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [progress, setProgress] = useState(0);
  const [detected, setDetected] = useState("");
  const [previewId, setPreviewId] = useState("");
  const [permissionConfirmed, setPermissionConfirmed] = useState(false);


  function previewYouTube() {
    const match = youtubeUrl.trim().match(
      /(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|embed\/|shorts\/))([A-Za-z0-9_-]{11})/
    );

    if (!match) {
      setPreviewId("");
      setMessage("Please enter a valid YouTube video link.");
      return;
    }

    setPreviewId(match[1]);
    setMessage(
      "Video preview loaded. Upload an authorized MP4 source to create Shorts."
    );
  }

  async function importDirectUrl() {
    if (!youtubeUrl.trim()) {
      setMessage("Paste a direct HTTPS MP4, MOV or M4V video URL first.");
      return;
    }

    setBusy(true);
    setMessage("Importing video from the authorized direct URL…");
    setClips([]);

    try {
      const r = await fetch(`${API}/api/import-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: youtubeUrl.trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw Error(d.detail || "Could not import this video URL.");
      setJobId(d.job_id);
      setFile(null);
      setProgress(0);
      setMessage("Video imported successfully. Choose your settings and click Generate AI Shorts.");
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Video URL import failed.");
    } finally {
      setBusy(false);
    }
  }

  async function upload() {
    if (!file) {
      setMessage("Choose an MP4 or MOV file first.");
      return;
    }

    setBusy(true);
    setMessage("Uploading video…");
    setClips([]);

    try {
      const fd = new FormData();
      fd.append("file", file);

      const r = await fetch(`${API}/api/upload`, {
        method: "POST",
        body: fd,
      });

      const d = await r.json();

      if (!r.ok) throw Error(d.detail || "Upload failed");

      setJobId(d.job_id);
      setMessage("Upload complete. Ready to generate.");
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function generate() {
    if (!jobId) {
      setMessage("Upload your video first.");
      return;
    }

    setBusy(true);
    setClips([]);
    setProgress(2);
    setMessage("Starting transcription and clip analysis…");

    try {
      const r = await fetch(
        `${API}/api/jobs/${jobId}/generate?count=${count}&duration=${duration}&language=${language}`,
        { method: "POST" }
      );

      const d = await r.json();

      if (!r.ok) throw Error(d.detail || "Could not start job");

      let done = false;

      while (!done) {
        await new Promise((res) => setTimeout(res, 2500));

        const sr = await fetch(`${API}/api/jobs/${jobId}`);
        const st = await sr.json();

        setProgress(st.progress || 0);
        setMessage(st.message || st.status);

        if (st.detected_language) {
          setDetected(st.detected_language);
        }

        if (st.status === "completed") {
          setClips(st.clips || []);
          setMessage(st.note || "Clips ready.");
          done = true;
        } else if (st.status === "failed") {
          throw Error(st.error || "Processing failed");
        }
      }
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Processing failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <nav className="nav wrap">
        <a className="brand" href="#">
          <span className="brand-mark">
            <Play size={19} fill="currentColor" />
          </span>
          <span>
            ClipGenius <b>AI</b>
          </span>
        </a>

        <div className="navlinks">
          <a href="#how">How it works</a>
          <a href="#features">Features</a>
          <a href="#studio">Studio</a>
        </div>

        <a className="nav-cta" href="#studio">
          <Sparkles size={16} /> Open Studio
        </a>
      </nav>

      <section className="hero wrap">
        <div className="hero-copy">
          <div className="eyebrow">
            <Sparkles size={15} /> AI-POWERED VIDEO REPURPOSING
          </div>

          <h1>
            Turn long videos into{" "}
            <span>scroll-stopping shorts.</span>
          </h1>

          <p className="lead">
            Upload a video and create vertical clips for YouTube Shorts,
            Instagram Reels and TikTok — all from one simple workspace.
          </p>

          <div className="checks">
            <span><CheckCircle2 /> AI clip ranking</span>
            <span><CheckCircle2 /> Multilingual captions</span>
            <span><CheckCircle2 /> Vertical export</span>
          </div>

          <a className="primary" href="#studio">
            <WandSparkles size={18} /> Create your shorts →
          </a>

          <p className="micro">
            Early MVP · MP4 upload · Hindi, English and Hinglish workflow
          </p>
        </div>

        <div className="hero-art">
          <div className="glow"></div>

          <div className="video-window">
            <div className="window-top">
              <span></span><span></span><span></span>
              <label>CLIPGENIUS STUDIO</label>
            </div>

            <div className="video-scene">
              <div className="play-circle">
                <Play fill="currentColor" />
              </div>
              <div className="scene-caption">
                YOUR NEXT <b>GREAT CLIP</b>
              </div>
              <div className="time">AI VIDEO</div>
            </div>

            <div className="timeline">
              <i></i><i></i><i></i><i></i><i></i>
            </div>
          </div>

          <div className="floating-card">
            <span className="float-icon"><Zap /></span>
            <div>
              <b>Smart clip workflow</b>
              <small>Transcribe · Rank · Export</small>
            </div>
            <span className="live-dot"></span>
          </div>

          <div className="vertical-card">
            <div className="vertical-screen">
              <span className="tag">9:16</span>
              <div className="bars"><i /><i /><i /></div>
              <b>
                SHORT<br />FORM<br /><em>READY</em>
              </b>
              <small>CAPTIONS INCLUDED</small>
            </div>
          </div>
        </div>
      </section>

      <section className="trust wrap">
        <span>BUILT FOR</span>
        <b>▶ YouTube Shorts</b>
        <b>◎ Instagram Reels</b>
        <b>▷ TikTok</b>
        <b>f Facebook Reels</b>
      </section>

      <section className="studio-section wrap" id="studio">
        <div className="section-heading">
          <div>
            <div className="eyebrow">YOUR CREATIVE WORKSPACE</div>
            <h2>
              Make your first <span>shorts.</span>
            </h2>
            <p>
              Upload a video you own or have permission to process. The engine
              transcribes speech, ranks moments and renders vertical clips.
            </p>
          </div>

          <div className="studio-status">
            <i /> PROCESSING ENGINE
          </div>
        </div>

        <div className="studio">
          <div className="studio-top">
            <div className="studio-title">
              <span className="mini-mark"><Film /></span>
              <div>
                <b>New video project</b>
                <small>
                  MP4 upload · Whisper transcription · FFmpeg export
                </small>
              </div>
            </div>

            <span className="step">STEP 01 / 03</span>
          </div>

          <div className="tabs">
            <button
              className={tab === "upload" ? "active" : ""}
              onClick={() => setTab("upload")}
            >
              <UploadCloud /> Upload video
            </button>

            <button
              className={tab === "youtube" ? "active" : ""}
              onClick={() => setTab("youtube")}
            >
              <Link2 /> YouTube URL
            </button>
          </div>

          {tab === "upload" ? (
            <label className="dropzone">
              <input
                type="file"
                accept="video/mp4,video/quicktime,.m4v"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />

              <span className="upload-icon"><UploadCloud /></span>

              <b>
                {file ? file.name : "Drop your video here, or browse"}
              </b>

              <small>
                {file
                  ? `${(file.size / 1024 / 1024).toFixed(1)} MB selected`
                  : "MP4, MOV or M4V · Select a file from your device"}
              </small>

              <span className="browse">Browse files</span>
            </label>
          ) : (
            <div className="url-panel">
              <Link2 />

              <input
                value={youtubeUrl}
                onChange={(e) => {
                  setYoutubeUrl(e.target.value);
                  setPreviewId("");
                  setPermissionConfirmed(false);
                }}
                placeholder="Paste a YouTube URL or direct MP4/MOV link"
              />

              <button
                className="secondary"
                type="button"
                onClick={previewYouTube}
              >
                Preview video
              </button>

              <button
                className="secondary"
                type="button"
                onClick={importDirectUrl}
                disabled={busy}
              >
                Import direct video URL
              </button>

              <small>
                Preview a YouTube video, or import an authorized direct MP4/MOV file to generate Shorts.
              </small>

              {previewId && (
                <>
                  <div
                    style={{
                      position: "relative",
                      paddingBottom: "56.25%",
                      height: 0,
                      overflow: "hidden",
                      width: "100%",
                      marginTop: 16,
                    }}
                  >
                    <iframe
                      title="YouTube video preview"
                      src={`https://www.youtube-nocookie.com/embed/${previewId}`}
                      allowFullScreen
                      style={{
                        position: "absolute",
                        inset: 0,
                        width: "100%",
                        height: "100%",
                        border: 0,
                      }}
                    />
                  </div>

                  <label
                    style={{
                      display: "flex",
                      gap: 10,
                      marginTop: 16,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={permissionConfirmed}
                      onChange={(e) =>
                        setPermissionConfirmed(e.target.checked)
                      }
                    />
                    I confirm I have permission to edit and reuse this video.
                  </label>

                  <button
                    className="primary"
                    type="button"
                    disabled={!permissionConfirmed}
                    onClick={() => setTab("upload")}
                  >
                    Continue to video upload
                  </button>

                  <small>
                    Upload the original MP4 file supplied by the creator or
                    another authorized source.
                  </small>
                </>
              )}
            </div>
          )}

          <div className="settings">
            <label>
              <span><Layers3 /> Number of shorts</span>
              <select
                value={count}
                onChange={(e) => setCount(e.target.value)}
              >
                {["1", "3", "5", "10", "15", "20"].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </label>

            <label>
              <span><Scissors /> Clip duration</span>
              <select
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
              >
                {["15", "30", "45", "60", "90"].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </label>

            <label>
              <span><Smartphone /> Aspect ratio</span>
              <div className="static-select">9:16 · Portrait</div>
            </label>

            <label>
              <span><Captions /> Caption language</span>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="auto">Auto detect</option>
                <option value="hi">Hindi</option>
                <option value="en">English</option>
                <option value="hinglish">Hinglish</option>
              </select>
            </label>
          </div>

          <div className="actions">
            <button
              className="secondary"
              onClick={upload}
              disabled={busy || !file}
            >
              <UploadCloud size={17} /> Upload video
            </button>

            <button
              className="primary generate"
              onClick={generate}
              disabled={busy || !jobId}
            >
              <WandSparkles size={17} />
              {busy ? "Processing…" : "Generate AI Shorts"}
              {busy && <Loader2 className="spin" size={16} />}
            </button>
          </div>

          {message && (
            <div className="notice">
              {message}

              {busy && (
                <div className="progress">
                  <div style={{ width: `${progress}%` }} />
                </div>
              )}

              {detected && (
                <small>Detected speech language: {detected}</small>
              )}
            </div>
          )}

          {clips.length > 0 && (
            <div className="results">
              <div className="results-head">
                <div>
                  <b>Your generated clips</b>
                  <small>
                    Transcript-ranked moments · captioned MP4 exports
                  </small>
                </div>
                <span>{clips.length} READY</span>
              </div>

              <div className="clip-grid">
                {clips.map((c) => (
                  <div className="clip" key={c.name}>
                    <video src={c.url} controls playsInline />

                    <div className="clip-meta">
                      <b>{c.name} · Score {c.score}</b>
                      <small>
                        {c.duration_seconds}s · starts at {c.start_seconds}s
                      </small>
                      <p>{c.transcript_preview}</p>
                      <a href={c.url} download>
                        <Download size={14} /> Download MP4
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </section>

      <section className="features wrap" id="features">
        <div className="section-heading center">
          <div className="eyebrow">THE CLIPGENIUS WORKFLOW</div>
          <h2>
            One video. <span>Multiple stories.</span>
          </h2>
          <p>
            Open-source transcription and video processing power the first
            release.
          </p>
        </div>

        <div className="feature-grid">
          <article>
            <div className="feature-icon"><Sparkles /></div>
            <h3>Transcript-based highlights</h3>
            <p>
              Rank speech segments using keyword, pacing and engagement
              heuristics.
            </p>
            <small>EARLY AI ENGINE</small>
          </article>

          <article>
            <div className="feature-icon"><Captions /></div>
            <h3>Multilingual captions</h3>
            <p>
              Whisper transcription for Hindi and English; Hinglish
              code-switching is best-effort.
            </p>
            <small>WHISPER</small>
          </article>

          <article>
            <div className="feature-icon"><Smartphone /></div>
            <h3>Vertical video export</h3>
            <p>
              FFmpeg renders portrait 9:16 video with burned-in subtitles.
            </p>
            <small>FFMPEG</small>
          </article>

          <article>
            <div className="feature-icon"><Layers3 /></div>
            <h3>Multiple clips</h3>
            <p>
              Create several non-overlapping candidate clips from one source.
            </p>
            <small>MP4 EXPORT</small>
          </article>
        </div>
      </section>

      <section className="how wrap" id="how">
        <div className="eyebrow">SIMPLE BY DESIGN</div>
        <h2>
          Three steps. <span>More stories.</span>
        </h2>

        <div className="how-grid">
          <div>
            <strong>01</strong>
            <h3>Upload</h3>
            <p>Bring in a long video you own or have permission to use.</p>
          </div>

          <div>
            <strong>02</strong>
            <h3>Transcribe & rank</h3>
            <p>
              Whisper transcribes speech; the first ranking engine scores
              transcript windows.
            </p>
          </div>

          <div>
            <strong>03</strong>
            <h3>Export & share</h3>
            <p>
              Review vertical clips with burned-in captions and download MP4
              files.
            </p>
          </div>
        </div>
      </section>

      <footer className="footer wrap">
        <a className="brand" href="#">
          <span className="brand-mark">
            <Play size={17} fill="currentColor" />
          </span>
          <span>
            ClipGenius <b>AI</b>
          </span>
        </a>

        <span>Make every moment count.</span>
        <small>© {new Date().getFullYear()} ClipGenius AI · MVP</small>
      </footer>
    </main>
  );
}

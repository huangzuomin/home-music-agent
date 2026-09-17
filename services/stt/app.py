#!/usr/bin/env python3
"""Home Music Agent MVP - STT service (sherpa-onnx + SenseVoice, Chinese-first)."""
import io
import json
import os
import time
import wave
from datetime import datetime

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/sense-voice")
NUM_THREADS = int(os.environ.get("NUM_THREADS", "2"))
LANGUAGE = os.environ.get("STT_LANGUAGE", "auto")
TRANSCRIPT_DIR = os.environ.get("TRANSCRIPT_DIR", "/transcripts")
TARGET_SR = 16000

app = FastAPI(title="Home Music Agent STT", version="0.1.0")

# PWA 前端跨域访问（局域网部署，页面在 :8400）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_state = {"recognizer": None, "meta": {}}


def get_recognizer():
    if _state["recognizer"] is None:
        t0 = time.time()
        model = os.path.join(MODEL_DIR, "model.int8.onnx")
        if not os.path.exists(model):
            model = os.path.join(MODEL_DIR, "model.onnx")
        tokens = os.path.join(MODEL_DIR, "tokens.txt")
        kwargs = dict(model=model, tokens=tokens, num_threads=NUM_THREADS,
                      use_itn=True, debug=False)
        try:
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(language=LANGUAGE, **kwargs)
        except TypeError:
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)
        _state["recognizer"] = rec
        _state["meta"] = {
            "model": os.path.basename(model),
            "language": LANGUAGE,
            "num_threads": NUM_THREADS,
            "load_seconds": round(time.time() - t0, 2),
            "sherpa_onnx": getattr(sherpa_onnx, "__version__", "unknown"),
        }
    return _state["recognizer"]


def decode_wav(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise HTTPException(status_code=400, detail="unsupported sample width %s" % sw)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != TARGET_SR and len(a):
        idx = np.arange(int(len(a) * TARGET_SR / sr)) * (sr / TARGET_SR)
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
        sr = TARGET_SR
    return sr, np.ascontiguousarray(a, dtype=np.float32)


def log_transcript(record):
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        path = os.path.join(TRANSCRIPT_DIR, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


@app.get("/")
def root():
    return {"service": "home-music-agent-stt", "endpoints": ["/health", "/transcribe"]}


@app.get("/health")
def health():
    return {"status": "ok", "model_dir": MODEL_DIR,
            "loaded": _state["recognizer"] is not None, **_state["meta"]}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    t0 = time.time()
    try:
        sr, samples = decode_wav(data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="not a readable wav: %s" % exc)
    if len(samples) == 0:
        raise HTTPException(status_code=400, detail="zero-length audio")
    rec = get_recognizer()
    stream = rec.create_stream()
    stream.accept_waveform(sr, samples)
    rec.decode_stream(stream)
    text = stream.result.text.strip()
    elapsed = time.time() - t0
    audio_s = len(samples) / sr
    result = {
        "text": text,
        "audio_seconds": round(audio_s, 3),
        "elapsed_seconds": round(elapsed, 3),
        "rtf": round(elapsed / audio_s, 3) if audio_s else None,
        "filename": file.filename,
    }
    log_transcript(dict(result, ts=datetime.now().isoformat(timespec="seconds")))
    return result

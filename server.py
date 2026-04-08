#!/usr/bin/env python3
"""
server.py — Dictation control server + UI (dev branch)
New features: real-time overlay, language selection, UI shortcut, onboarding, dark/light mode
"""

from flask import Flask, jsonify, request
import logging, threading, json, os, sys, tempfile, subprocess, urllib.request, time, re, socket as _socket
import numpy as np, sounddevice as sd, scipy.io.wavfile as wavfile
from pynput import keyboard as kb
from pynput import mouse as ms
from datetime import date

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

_DATA_DIR   = os.environ.get("APP_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_DATA_DIR, 'config.json')
STATS_FILE  = os.path.join(_DATA_DIR, 'stats.json')
ERROR_LOG   = os.path.join(_DATA_DIR, 'error.log')
HISTORY_FILE = os.path.join(_DATA_DIR, 'history.json')
LAUNCH_AGENT_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.dictate.app.plist")

def _log_error(msg):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
SAMPLE_RATE     = 16000
OLLAMA_URL      = "http://localhost:11434/api/generate"
APP_VERSION     = "1.7.0"
GITHUB_RAW      = "https://raw.githubusercontent.com/mcolfax/dictate/main"
MAX_RECORD_SECS = 120

# Supported languages (Whisper language codes)
LANGUAGES = {
    "Auto-detect": None,
    "English": "en", "Spanish": "es", "French": "fr", "German": "de",
    "Italian": "it", "Portuguese": "pt", "Dutch": "nl", "Russian": "ru",
    "Japanese": "ja", "Korean": "ko", "Chinese": "zh", "Arabic": "ar",
    "Hindi": "hi", "Turkish": "tr", "Polish": "pl", "Swedish": "sv",
    "Danish": "da", "Norwegian": "no", "Finnish": "fi",
}

DEFAULT_CONFIG = {
    "mode":             "toggle",
    "hotkey":           "cmd+shift+alt",
    "hotkey_label":     "⌘⇧⌥",
    "hotkey_type":      "combo",
    "ui_shortcut":      None,
    "ui_shortcut_label": None,
    "whisper_model":    "mlx-community/whisper-small-mlx",
    "ollama_model":     "llama3.2",
    "tone":             "neutral",
    "cleanup":          True,
    "clipboard_only":   False,
    "sound_feedback":   True,
    "sound_start":      "Tink",
    "sound_stop":       "Pop",
    "sound_done":       "Glass",
    "pause_detection":  True,
    "pause_seconds":    2.0,
    "vocabulary":       [],
    "app_tones":        {},
    "transcribe_language": None,   # None = auto-detect
    "paste_language":   "en",      # Target language for paste
    "overlay_enabled":  True,
    "overlay_x":        None,
    "overlay_y":        None,
    "theme":            "system",  # "system", "dark", "light"
    "onboarding_done":  False,
    "mic_device":       None,      # None = system default; device name string to override
    "remove_fillers":   False,     # Strip um/uh/er filler words
    "launch_at_login":  False,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        return {**DEFAULT_CONFIG, **json.load(open(CONFIG_FILE))}
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

config = load_config()

def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            return json.load(open(HISTORY_FILE))
    except Exception:
        pass
    return []

def save_history(h):
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(h[:100], f)  # keep last 100
    except Exception:
        pass

# ── STATS ─────────────────────────────────────────────────────────────────────

def load_stats():
    today = str(date.today())
    if os.path.exists(STATS_FILE):
        s = json.load(open(STATS_FILE))
        if s.get("date") != today:
            # Roll over: archive today's counts into daily history
            daily = s.get("daily", {})
            if s.get("date") and s.get("words_today", 0) > 0:
                daily[s["date"]] = s.get("words_today", 0)
            # Trim to last 30 days
            if len(daily) > 30:
                for old in sorted(daily)[:-30]:
                    del daily[old]
            s = {"date": today, "words_today": 0, "sessions_today": 0,
                 "words_total": s.get("words_total", 0),
                 "sessions_total": s.get("sessions_total", 0),
                 "daily": daily}
        elif "daily" not in s:
            s["daily"] = {}
    else:
        s = {"date": today, "words_today": 0, "sessions_today": 0,
             "words_total": 0, "sessions_total": 0, "daily": {}}
    return s

def save_stats(s):
    with open(STATS_FILE, 'w') as f:
        json.dump(s, f, indent=2)

def record_transcription_stats(text):
    s = load_stats()
    words = len(text.split())
    s["words_today"] += words; s["sessions_today"] += 1
    s["words_total"] += words; s["sessions_total"] += 1
    save_stats(s)

def get_weekly_stats():
    """Return list of {date, words} for last 7 days including today."""
    from datetime import timedelta
    s = load_stats()
    daily = s.get("daily", {})
    today = date.today()
    result = []
    for i in range(6, -1, -1):
        d = str(today - timedelta(days=i))
        result.append({"date": d, "words": daily.get(d, 0) if d != str(today) else s.get("words_today", 0)})
    return result

# ── STATE ─────────────────────────────────────────────────────────────────────

state = {
    "enabled":           False,
    "recording":         False,
    "transcribing":      False,
    "capturing":         False,
    "capturing_type":    "combo",  # "combo" | "keyboard" | "mouse"
    "capture_warning":   None,     # warning message during keyboard capture
    "capture_error":     None,     # error message during keyboard capture
    "kb_preview":        "",       # live preview string during keyboard capture
    "capturing_ui":      False,   # Capturing UI shortcut
    "mic_testing":       False,
    "mic_level":         0,
    "history":           load_history(),
    "_recorded_frames":  [],
    "overlay_text":      "",      # Real-time overlay text
    "partial_chunks":    [],      # Accumulated partial transcriptions
}

_recording_thread  = None
_partial_thread    = None
_stop_event        = threading.Event()
_stop_partial      = threading.Event()
_kb_listener       = None
_ms_listener       = None
_lock              = threading.Lock()
_hold_active       = False  # True while hold key/button is physically down
_last_sound_time   = 0.0
_persistent_stream = None
_overlay_proc      = None
OVERLAY_SOCKET     = os.path.join(_DATA_DIR, "overlay.sock")

# ── SOUND ─────────────────────────────────────────────────────────────────────

SYSTEM_SOUNDS_DIR = "/System/Library/Sounds"
SYSTEM_SOUNDS = sorted([
    os.path.splitext(f)[0]
    for f in os.listdir(SYSTEM_SOUNDS_DIR) if f.endswith(".aiff")
])

def _sound_path(name: str) -> str:
    return os.path.join(SYSTEM_SOUNDS_DIR, f"{name}.aiff")

def play_sound(event: str):
    """Play the user-configured sound for 'start', 'stop', 'done', or 'error'."""
    if not config.get("sound_feedback", True): return
    defaults = {"start": "Tink", "stop": "Pop", "done": "Glass", "error": "Basso"}
    name = config.get(f"sound_{event}", defaults.get(event))
    if not name or name == "None": return
    path = _sound_path(name)
    if os.path.exists(path):
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── OVERLAY ───────────────────────────────────────────────────────────────────

def _send_overlay(text: str) -> bool:
    """Send a text update to the overlay subprocess via Unix socket. Returns True on success."""
    try:
        conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.settimeout(0.5)
        conn.connect(OVERLAY_SOCKET)
        conn.sendall(json.dumps({"text": text}).encode("utf-8"))
        conn.close()
        return True
    except Exception:
        return False

def _build_overlay_bundle():
    """Build ~/.dictate/Overlay.app if missing or overlay.py is newer."""
    bundle  = os.path.join(_DATA_DIR, "Overlay.app")
    macos   = os.path.join(bundle, "Contents", "MacOS")
    exe     = os.path.join(macos, "Overlay")
    plist   = os.path.join(bundle, "Contents", "Info.plist")
    script  = os.path.join(_DATA_DIR, "overlay.py")

    needs_build = (
        not os.path.exists(exe) or
        not os.path.exists(plist) or
        (os.path.exists(script) and os.path.getmtime(script) > os.path.getmtime(exe))
    )
    if not needs_build:
        return exe

    os.makedirs(macos, exist_ok=True)
    with open(plist, "w") as f:
        f.write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>Overlay</string>
    <key>CFBundleIdentifier</key><string>com.dictate.overlay</string>
    <key>CFBundleName</key><string>DictateOverlay</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>""")

    python_exe = os.path.abspath(sys.executable)
    with open(exe, "w") as f:
        f.write(f"""#!/bin/bash
export APP_DATA_DIR="{_DATA_DIR}"
exec "{python_exe}" "{script}"
""")
    os.chmod(exe, 0o755)
    return exe

def _kill_stale_overlays():
    """Kill every lingering overlay.py process (handles multi-session zombies)."""
    try:
        r = subprocess.run(["pgrep", "-f", "overlay.py"], capture_output=True, text=True)
        for pid_str in r.stdout.strip().splitlines():
            try: os.kill(int(pid_str), 9)
            except Exception: pass
    except Exception:
        pass
    # Remove stale socket so the new process can bind cleanly
    try:
        if os.path.exists(OVERLAY_SOCKET): os.unlink(OVERLAY_SOCKET)
    except Exception:
        pass

def show_overlay():
    """Start the overlay subprocess via a mini .app bundle (no dock icon)."""
    global _overlay_proc
    if not config.get("overlay_enabled", True):
        return

    # Check whether an existing overlay is already reachable on the socket
    if os.path.exists(OVERLAY_SOCKET):
        try:
            test = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            test.settimeout(0.2)
            test.connect(OVERLAY_SOCKET)
            test.close()
            return  # Overlay alive and reachable — nothing to do
        except Exception:
            pass  # Socket file exists but nothing is listening — fall through

    # Socket unreachable: kill any zombie overlay processes and spawn fresh
    _kill_stale_overlays()
    _overlay_proc = None

    try:
        exe = _build_overlay_bundle()
        _overlay_proc = subprocess.Popen(
            [exe],
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(_DATA_DIR, "overlay_error.log"), "a"),
        )
        # Poll for socket readiness instead of fixed sleep
        for _ in range(20):
            if os.path.exists(OVERLAY_SOCKET):
                break
            time.sleep(0.1)
    except Exception as e:
        print(f"⚠️  Overlay launch error: {e}")

def notify_overlay(text: str):
    state["overlay_text"] = text  # Keep state in sync for /api/status
    if not config.get("overlay_enabled", True):
        return
    global _overlay_proc
    # Try sending; if it fails, kill any stale process and restart
    if not _send_overlay(text):
        if _overlay_proc is not None:
            try: _overlay_proc.kill()
            except Exception: pass
        _overlay_proc = None
        show_overlay()
        _send_overlay(text)

def hide_overlay_display():
    state["overlay_text"] = ""
    _send_overlay("")

_last_level_send = 0.0

def notify_overlay_level(level: float):
    """Send audio level (0.0–1.0) to overlay for waveform animation. Throttled to ~15/s."""
    global _last_level_send
    now = time.monotonic()
    if now - _last_level_send < 0.067:
        return
    _last_level_send = now
    if not config.get("overlay_enabled", True):
        return
    try:
        conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.settimeout(0.1)
        conn.connect(OVERLAY_SOCKET)
        conn.sendall(json.dumps({"level": round(float(level), 3)}).encode("utf-8"))
        conn.close()
    except Exception:
        pass

# ── FRONTMOST APP ─────────────────────────────────────────────────────────────

def get_frontmost_app():
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=1)
        return result.stdout.strip()
    except Exception:
        return ""

# ── VOCABULARY ────────────────────────────────────────────────────────────────

def apply_vocabulary(text):
    for entry in config.get("vocabulary", []):
        src = entry.get("from", "")
        dst = entry.get("to", "")
        if src:
            text = re.sub(r'(?i)\b' + re.escape(src) + r'\b', dst, text)
    return text

_FILLER_RE = re.compile(
    r'\b(um+h?|uh+|er+|ah+|hmm+|mhm+)\b[,\s]*'
    r'|\b(like|you\s+know|basically|literally|right|okay|so)\b(?=\s*,|\s+(?:um|uh|like|and|but|I|we|it|the|a)\b)',
    re.IGNORECASE
)

def remove_fillers(text):
    if not config.get("remove_fillers", False):
        return text
    cleaned = _FILLER_RE.sub(' ', text)
    return re.sub(r'[ \t]+', ' ', cleaned).strip()

# ── AUDIO STREAM ──────────────────────────────────────────────────────────────

def _check_mic_permission():
    """Returns True if mic access is granted or undetermined. Shows dialog if denied."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        # 0=notDetermined (let sounddevice prompt natively), 1=restricted, 2=denied, 3=authorized
        if status == 2:
            subprocess.Popen([
                "osascript", "-e",
                'display dialog "Dictate needs microphone access.\\n\\nGo to System Settings → Privacy & Security → Microphone and enable Dictate." '
                'buttons {"Open Settings", "Cancel"} default button "Open Settings" with title "Microphone Access Required"\n'
                'if button returned of result is "Open Settings" then\n'
                '  do shell script "open \\"x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone\\""\n'
                'end if'
            ])
            return False
        return True  # notDetermined, restricted, or authorized — let sounddevice handle it
    except Exception:
        return True  # Can't check — let sounddevice try and fail naturally

def _resolve_mic_device():
    """Return the sounddevice device index for the configured mic, or None for system default."""
    name = config.get("mic_device")
    if not name:
        return None
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and d["name"] == name:
                return i
    except Exception:
        pass
    return None  # Fall back to system default if not found

def _ensure_stream():
    global _persistent_stream
    if _persistent_stream is None or not _persistent_stream.active:
        if not _check_mic_permission():
            return
        try:
            device = _resolve_mic_device()
            _persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                dtype="int16", blocksize=1600,
                device=device,
            )
            _persistent_stream.start()
        except Exception as e:
            print(f"⚠️  Stream init error: {e}")
            if "invalid" in str(e).lower() or "permission" in str(e).lower():
                _check_mic_permission()

def _close_stream():
    global _persistent_stream
    if _persistent_stream is not None:
        try:
            _persistent_stream.stop()
            _persistent_stream.close()
        except Exception:
            pass
        _persistent_stream = None

# ── PARTIAL TRANSCRIPTION (real-time overlay) ─────────────────────────────────

def _partial_transcribe_worker():
    """Transcribe audio in chunks every N seconds for real-time overlay."""
    chunk_interval = 3.0  # seconds between partial transcriptions
    last_chunk_time = time.time()
    accumulated = []

    while not _stop_partial.is_set() and state["recording"]:
        time.sleep(0.1)
        now = time.time()
        if now - last_chunk_time >= chunk_interval and state["_recorded_frames"]:
            # Grab frames so far
            frames = list(state["_recorded_frames"])
            if frames:
                try:
                    audio = np.concatenate(frames, axis=0).flatten()
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        wavfile.write(f.name, SAMPLE_RATE, audio)
                        tmp = f.name
                    lang = config.get("transcribe_language") or None
                    lang_arg = f', language="{lang}"' if lang else ''
                    cmd = ["arch", "-arm64", sys.executable, "-c", f"""
import mlx_whisper, json, sys
result = mlx_whisper.transcribe(sys.argv[1]{lang_arg})
print(json.dumps({{"text": result["text"]}}))
""", tmp]
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    os.unlink(tmp)
                    if proc.returncode != 0:
                        last_chunk_time = now
                        continue
                    partial = json.loads(proc.stdout.strip())["text"].strip()
                    if partial:
                        notify_overlay(partial)
                        state["partial_chunks"] = [partial]
                except Exception as e:
                    pass
            last_chunk_time = now

# ── MAIN RECORDING ────────────────────────────────────────────────────────────

def _record_worker():
    global _last_sound_time, _persistent_stream
    _last_sound_time = time.time()
    all_frames = []

    _ensure_stream()
    if _persistent_stream is None:
        return

    try:
        while not _stop_event.is_set() and state["recording"]:
            chunk, _ = _persistent_stream.read(1600)
            all_frames.append(chunk.copy())
            state["_recorded_frames"] = all_frames  # Keep updated for partial thread

            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            notify_overlay_level(min(1.0, rms / 8000.0))

            if config.get("pause_detection", True) and config.get("mode") not in ("toggle", "hold"):
                if rms > 200:
                    _last_sound_time = time.time()
                elif time.time() - _last_sound_time >= float(config.get("pause_seconds", 2.0)):
                    print("🤫 Silence — auto-stopping")
                    state["recording"] = False
                    break

            if len(all_frames) * 0.1 >= MAX_RECORD_SECS:
                state["recording"] = False
                break
    except Exception as e:
        print(f"⚠️  Audio error: {e}")
        _persistent_stream = None

    state["_recorded_frames"] = all_frames

def start_recording():
    global _recording_thread, _partial_thread
    with _lock:
        if state["recording"]: return
        state["recording"]        = True
        state["_recorded_frames"] = []
        state["partial_chunks"]   = []

    _stop_event.clear()
    _stop_partial.clear()
    _ensure_stream()
    play_sound("start")
    notify_overlay("Listening…")
    print("🎙️  Recording…")

    _recording_thread = threading.Thread(target=_record_worker, daemon=True)
    _recording_thread.start()

    if config.get("overlay_enabled", True):
        _partial_thread = threading.Thread(target=_partial_transcribe_worker, daemon=True)
        _partial_thread.start()

def stop_and_transcribe():
    global _recording_thread
    with _lock:
        if not state["recording"]: return
        state["recording"] = False

    _stop_event.set()
    _stop_partial.set()
    if _recording_thread:
        _recording_thread.join(timeout=2)
    _close_stream()
    play_sound("stop")
    notify_overlay("Processing…")

    frames = state.get("_recorded_frames", [])
    if not frames:
        hide_overlay_display()
        return
    state["transcribing"] = True
    print("⏳ Transcribing…")
    audio_data = np.concatenate(frames, axis=0).flatten()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wavfile.write(f.name, SAMPLE_RATE, audio_data)
        tmp_path = f.name

    try:
        import sys as _sys
        lang = config.get("transcribe_language") or None
        lang_arg = f', language="{lang}"' if lang else ''
        cmd = ["arch", "-arm64", _sys.executable, "-c", f"""
import mlx_whisper, json, sys
result = mlx_whisper.transcribe(sys.argv[1]{lang_arg})
print(json.dumps({{"text": result["text"], "language": result.get("language", "en")}}))
""", tmp_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise Exception(f"Whisper failed: {proc.stderr[-200:]}")
        result = json.loads(proc.stdout.strip())
        raw_text = result["text"].strip()
        detected_lang = result.get("language", "en")

        if not raw_text: return
        corrected   = apply_vocabulary(raw_text)
        corrected   = remove_fillers(corrected)
        active_app  = get_frontmost_app()
        app_tones   = config.get("app_tones", {})
        active_tone = app_tones.get(active_app, config.get("tone", "neutral"))
        print(f"📝 Raw: {raw_text} (lang: {detected_lang})")

        paste_lang = config.get("paste_language", "en")
        needs_translation = paste_lang and paste_lang != detected_lang

        if (config.get("cleanup", True) and len(corrected.split()) > 3) or needs_translation:
            final = cleanup_with_ollama(corrected, active_tone, detected_lang, paste_lang)
            print(f"✨ Final: {final}")
        else:
            final = corrected

        output_text(final)
        record_transcription_stats(final)
        entry = {"raw": raw_text, "cleaned": final, "ts": time.strftime("%H:%M:%S"),
                 "app": active_app, "cleanup_used": config.get("cleanup", True),
                 "tone_used": active_tone, "lang": detected_lang}
        state["history"].insert(0, entry)
        state["history"] = state["history"][:100]
        save_history(state["history"])
        play_sound("done")
    except Exception as e:
        _log_error(f"Transcription: {e}")
        print(f"❌ Error: {e}")
        play_sound("error")
    finally:
        try: os.unlink(tmp_path)
        except Exception: pass
        state["transcribing"] = False
        hide_overlay_display()

# ── MIC TEST ──────────────────────────────────────────────────────────────────

def _mic_test_worker():
    _ensure_stream()
    if _persistent_stream is None: return
    try:
        while state["mic_testing"]:
            chunk, _ = _persistent_stream.read(1600)
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            state["mic_level"] = min(100, int((rms / 32768) * 800))
    except Exception as e:
        print(f"⚠️  Mic test error: {e}")

def start_mic_test():
    if state["mic_testing"]: return
    state["mic_testing"] = True; state["mic_level"] = 0
    threading.Thread(target=_mic_test_worker, daemon=True).start()

def stop_mic_test():
    state["mic_testing"] = False; state["mic_level"] = 0

# ── CLEANUP + TRANSLATION ─────────────────────────────────────────────────────

TONE_INSTRUCTIONS = {
    "neutral":      "Make no style changes beyond fixing errors.",
    "professional": "Also make the language slightly more formal where natural.",
    "casual":       "Also keep the tone relaxed and conversational.",
    "concise":      "Also trim any redundant words, but do not change the meaning.",
}

def cleanup_with_ollama(text, tone_override=None, source_lang="en", target_lang="en"):
    tone_key = tone_override or config.get("tone", "neutral")
    tone     = TONE_INSTRUCTIONS.get(tone_key, TONE_INSTRUCTIONS["neutral"])

    translating = source_lang and target_lang and source_lang != target_lang

    if translating:
        lang_names = {
            "en": "English", "de": "German", "fr": "French", "es": "Spanish",
            "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
            "zh": "Chinese", "nl": "Dutch", "ru": "Russian", "ar": "Arabic",
            "hi": "Hindi", "tr": "Turkish", "pl": "Polish", "sv": "Swedish",
        }
        src_name = lang_names.get(source_lang, source_lang)
        dst_name = lang_names.get(target_lang, target_lang)
        prompt = (
            f"Translate this exact text from {src_name} to {dst_name}.\n"
            f"Rules: output ONLY the {dst_name} translation. No explanations. No extra text.\n\n"
            f"{src_name}: {text}\n"
            f"{dst_name}:"
        )
        model = config["ollama_model"]
        if model in ("qwen2.5:0.5b", "qwen2.5:1.5b", "llama3.2:1b"):
            model = "llama3.2"
    else:
        prompt = (
            f"You are a transcription corrector. The text below was spoken aloud and auto-transcribed. "
            f"Your job is ONLY to fix typos, capitalization, and punctuation — nothing else. "
            f"Do NOT rephrase, summarize, answer, interpret, or change the meaning in any way. "
            f"Do NOT add any words that were not spoken. "
            f"Output ONLY the corrected spoken words. {tone}\n\n"
            f"Transcription: {text}\n\nCorrected transcription:"
        )
        model = config["ollama_model"]

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["response"].strip()
    except Exception as e:
        print(f"⚠️  Ollama error: {e}")
        return text

# ── OUTPUT ────────────────────────────────────────────────────────────────────

def output_text(text):
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    if config.get("clipboard_only", False):
        print("📋 Copied to clipboard")
        return
    subprocess.run(["osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down'], check=True)
    print("✅ Injected!")

# ── TRIGGER LOGIC ─────────────────────────────────────────────────────────────

def _hold_record():
    """Start recording for hold mode, then immediately stop if key was released
    before the recording thread had a chance to start (race condition fix)."""
    start_recording()
    if not _hold_active and state["recording"]:
        stop_and_transcribe()

def handle_trigger_press():
    global _hold_active
    if not state["enabled"]: return
    if _hold_active: return  # ignore key-repeat events in all modes
    _hold_active = True
    if config["mode"] == "toggle":
        if not state["recording"]:
            threading.Thread(target=start_recording, daemon=True).start()
        else:
            threading.Thread(target=stop_and_transcribe, daemon=True).start()
    elif config["mode"] == "hold":
        if not state["recording"]:
            threading.Thread(target=_hold_record, daemon=True).start()

def handle_trigger_release():
    global _hold_active
    _hold_active = False
    if not state["enabled"]: return
    if config["mode"] == "hold" and state["recording"]:
        threading.Thread(target=stop_and_transcribe, daemon=True).start()

def handle_ui_shortcut():
    """Open the settings window."""
    settings_py = os.path.join(_DATA_DIR, "settings_window.py")
    if not os.path.exists(settings_py):
        settings_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_window.py")
    subprocess.Popen(["arch", "-arm64", sys.executable, settings_py])

# ── KEYBOARD LISTENER ─────────────────────────────────────────────────────────

# Keys that must never be used as a UI shortcut — they'd break normal typing/system.
_BLOCKED_UI_KEYS = {
    # Letters (a-z)
    *[kb.KeyCode.from_char(c) for c in "abcdefghijklmnopqrstuvwxyz"],
    # Digits via KeyCode
    *[kb.KeyCode.from_char(c) for c in "0123456789"],
    # Critical system / editing keys
    kb.Key.space, kb.Key.enter, kb.Key.backspace, kb.Key.delete,
    kb.Key.esc, kb.Key.tab,
    kb.Key.up, kb.Key.down, kb.Key.left, kb.Key.right,
    kb.Key.home, kb.Key.end, kb.Key.page_up, kb.Key.page_down,
    kb.Key.cmd, kb.Key.cmd_r,
    kb.Key.ctrl, kb.Key.ctrl_r,
    kb.Key.shift, kb.Key.shift_r,
    kb.Key.alt, kb.Key.alt_r,
    kb.Key.caps_lock,
    # Function keys F1–F12 are system-assigned on most Macs
    kb.Key.f1, kb.Key.f2, kb.Key.f3, kb.Key.f4, kb.Key.f5, kb.Key.f6,
    kb.Key.f7, kb.Key.f8, kb.Key.f9, kb.Key.f10, kb.Key.f11, kb.Key.f12,
}

# Modifier keys tracked for 3-modifier combo detection
_MODIFIER_KEYS = {
    kb.Key.cmd, kb.Key.cmd_r,
    kb.Key.shift, kb.Key.shift_r,
    kb.Key.alt, kb.Key.alt_r,
    kb.Key.ctrl, kb.Key.ctrl_r,
}

_COMBO_MODIFIERS = {kb.Key.cmd, kb.Key.cmd_r, kb.Key.shift, kb.Key.shift_r, kb.Key.alt, kb.Key.alt_r}

# Currently held modifier keys
_held_modifiers: set = set()

# Live keyboard capture state (only used while capturing_type == "keyboard")
_kb_cap_mods: set = set()      # modifier keys held during capture
_kb_cap_trigger = None         # last non-modifier key pressed during capture
_kb_cap_trigger_name: str = "" # its name string

# Possible 3-modifier combos the user can choose from
COMBO_OPTIONS = {
    "cmd+shift+alt":  ("⌘⇧⌥",  {kb.Key.cmd, kb.Key.shift, kb.Key.alt}),
    "cmd+shift+alt_r":("⌘⇧⌥›", {kb.Key.cmd, kb.Key.shift, kb.Key.alt_r}),
    "cmd+ctrl+alt":   ("⌘⌃⌥",  {kb.Key.cmd, kb.Key.ctrl,  kb.Key.alt}),
    "cmd+shift+ctrl": ("⌘⇧⌃",  {kb.Key.cmd, kb.Key.shift, kb.Key.ctrl}),
}

# ── KEYBOARD HOTKEY LABELS ────────────────────────────────────────────────────
_MOD_LABELS = {
    "cmd": "⌘", "cmd_r": "⌘›",
    "ctrl": "⌃", "ctrl_r": "⌃›",
    "alt": "⌥", "alt_r": "⌥›",
    "shift": "⇧", "shift_r": "⇧›",
}
_KEY_LABELS = {
    **{f"f{i}": f"F{i}" for i in range(1, 21)},
    "space": "Space", "enter": "↩", "backspace": "⌫", "delete": "⌦",
    "tab": "⇥", "esc": "Esc", "home": "↖", "end": "↘",
    "page_up": "⇞", "page_down": "⇟",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
}

def _kb_key_name(key) -> str:
    """Return the storage name for a pynput key (used in hotkey strings)."""
    if isinstance(key, kb.Key):
        return key.name
    if isinstance(key, kb.KeyCode) and key.char:
        return key.char.lower()
    return ""

def _kb_key_label(key_name: str) -> str:
    """Human-readable label for a key name."""
    if key_name in _MOD_LABELS:
        return _MOD_LABELS[key_name]
    if key_name in _KEY_LABELS:
        return _KEY_LABELS[key_name]
    if len(key_name) == 1:
        return key_name.upper()
    return key_name.replace("_", " ").title()

def _kb_hotkey_label(hotkey_str: str) -> str:
    """Build a display label from a hotkey string like 'ctrl+alt+d' → '⌃⌥D'."""
    parts = hotkey_str.split("+")
    return "".join(_kb_key_label(p) for p in parts)

# ── KEYBOARD HOTKEY VALIDATION ────────────────────────────────────────────────

# Bare keys that are always blocked as dictation hotkeys
_KB_BLOCKED_BARE = {
    # Letters
    *[c for c in "abcdefghijklmnopqrstuvwxyz"],
    # Digits
    *[c for c in "0123456789"],
    # Essential editing/nav keys
    "space", "enter", "backspace", "delete", "tab", "esc",
    "up", "down", "left", "right",
    "home", "end", "page_up", "page_down",
    # Left-side modifiers (too intrusive as solo hotkeys)
    "cmd", "shift", "alt", "ctrl",
    # Right shift (commonly used as modifier)
    "shift_r",
    # Caps lock
    "caps_lock",
}

# Combos that are always blocked — (frozenset_of_mod_names, trigger_name)
_KB_BLOCKED_COMBOS: set = {
    # Essential macOS / universal app shortcuts
    (frozenset(["cmd"]), c) for c in list("cCvVxXzZaAqQwWsSnNtT")
} | {
    (frozenset(["cmd"]), "space"),
    (frozenset(["cmd"]), "tab"),
    (frozenset(["cmd"]), "delete"),
    (frozenset(["cmd"]), "backspace"),
    (frozenset(["ctrl"]), "space"),    # input source switch
    (frozenset(["ctrl", "cmd"]), "space"),  # character viewer
    (frozenset(["cmd", "shift"]), "3"),
    (frozenset(["cmd", "shift"]), "4"),
    (frozenset(["cmd", "shift"]), "5"),
    (frozenset(["cmd", "shift"]), "z"),
}

# Combos or keys that warn but are allowed
_KB_WARNED: list = [
    # (set_of_mod_names_or_None, trigger_name_or_None, message)
    # F1-F12 alone
    *[(set(), f"f{i}", f"F{i} is used by macOS for system functions (brightness, volume, Mission Control, etc.).")
      for i in range(1, 13)],
    # Right Option alone — used for special chars in many apps
    (set(), "alt_r", "⌥› (Right Option) is used for typing accented and special characters in many apps. It may interfere with text entry."),
    # Common ⌘ combos not in blocked list
    *[({"cmd"}, c, f"⌘{c.upper()} is a common application shortcut and may conflict.")
      for c in list("defghijklmopruybBDEFGHIJKLMOPRUY")],
    # ⌥+letter — special characters
    *[( {"alt"}, c, f"⌥{c.upper()} types a special character in most apps and may conflict.")
      for c in list("abcdefghijklmnopqrstuvwxyz")],
]

def _validate_kb_hotkey(mod_names: set, trigger_name: str):
    """
    Returns ("block", msg) | ("warn", msg) | ("ok", "").
    mod_names: set of modifier name strings (e.g. {"ctrl", "alt"})
    trigger_name: name of the trigger key (e.g. "d", "f5", "alt_r")
    """
    # Hard block: bare blocked keys
    if not mod_names and trigger_name in _KB_BLOCKED_BARE:
        return ("block", f"'{_kb_key_label(trigger_name)}' cannot be used alone as a dictation hotkey — it would break normal typing.")

    # Hard block: specific combos
    combo_key = (frozenset(mod_names), trigger_name.lower())
    # Normalize case for single-char trigger
    if len(trigger_name) == 1:
        combo_key = (frozenset(mod_names), trigger_name.lower())
    for blocked in _KB_BLOCKED_COMBOS:
        if blocked == combo_key:
            label = "".join(_MOD_LABELS.get(m, m) for m in sorted(mod_names)) + _kb_key_label(trigger_name)
            return ("block", f"{label} is a reserved system shortcut and cannot be used.")

    # Warnings
    for warn_mods, warn_trigger, warn_msg in _KB_WARNED:
        if mod_names == set(warn_mods) and trigger_name == warn_trigger:
            return ("warn", warn_msg)

    return ("ok", "")

# ── KEYBOARD HOTKEY PARSING ───────────────────────────────────────────────────
_MOD_NAME_TO_KEYS = {
    "cmd":    (kb.Key.cmd, kb.Key.cmd_r),
    "ctrl":   (kb.Key.ctrl, kb.Key.ctrl_r),
    "alt":    (kb.Key.alt, kb.Key.alt_r),
    "shift":  (kb.Key.shift, kb.Key.shift_r),
    "cmd_r":  (kb.Key.cmd_r,),
    "alt_r":  (kb.Key.alt_r,),
    "ctrl_r": (kb.Key.ctrl_r,),
    "shift_r":(kb.Key.shift_r,),
}

def _parse_kb_hotkey(hotkey_str: str):
    """Parse 'ctrl+alt+d' → (mod_names_set, trigger_name, trigger_pynput_key)."""
    parts = hotkey_str.split("+")
    mod_names = set(parts[:-1])
    trigger_name = parts[-1]
    try:
        trigger_key = getattr(kb.Key, trigger_name)
    except AttributeError:
        trigger_key = kb.KeyCode.from_char(trigger_name) if len(trigger_name) == 1 else None
    return mod_names, trigger_name, trigger_key

def _mods_satisfied(req_names: set) -> bool:
    """True if all required modifier names are satisfied by currently held keys."""
    for name in req_names:
        keys = _MOD_NAME_TO_KEYS.get(name, ())
        if not any(k in _held_modifiers for k in keys):
            return False
    return True

def _kb_preview_label() -> str:
    """Build a live preview label from _kb_cap_mods + _kb_cap_trigger."""
    parts = []
    # Order: ctrl, cmd, alt, shift, then trigger
    for name in ("ctrl", "ctrl_r", "cmd", "cmd_r", "alt", "alt_r", "shift", "shift_r"):
        if any(k in _kb_cap_mods for k in _MOD_NAME_TO_KEYS.get(name, ())):
            if name not in parts:
                parts.append(name)
    label = "".join(_MOD_LABELS.get(p, p) for p in parts)
    if _kb_cap_trigger_name:
        label += _kb_key_label(_kb_cap_trigger_name)
    return label or "…"

def _combo_is_active(combo_key: str) -> bool:
    """Return True when exactly the 3 mods for this combo are all held."""
    _, required = COMBO_OPTIONS.get(combo_key, (None, set()))
    if not required:
        return False
    # Normalise: treat cmd/cmd_r etc. as interchangeable within the required set
    # by checking that for each required key, either it or its pair is held
    _pair = {kb.Key.cmd: kb.Key.cmd_r, kb.Key.cmd_r: kb.Key.cmd,
             kb.Key.shift: kb.Key.shift_r, kb.Key.shift_r: kb.Key.shift,
             kb.Key.alt: kb.Key.alt_r, kb.Key.alt_r: kb.Key.alt,
             kb.Key.ctrl: kb.Key.ctrl_r, kb.Key.ctrl_r: kb.Key.ctrl}
    for mod in required:
        if mod not in _held_modifiers and _pair.get(mod) not in _held_modifiers:
            return False
    return True

def get_ui_hotkey():
    ui = config.get("ui_shortcut")
    if not ui: return None
    try:
        return getattr(kb.Key, ui)
    except AttributeError:
        if len(ui) == 1:
            return kb.KeyCode.from_char(ui)
        return None

def _key_label(key):
    labels = {
        kb.Key.f13: "F13", kb.Key.f14: "F14", kb.Key.f15: "F15",
        kb.Key.f16: "F16", kb.Key.f17: "F17", kb.Key.f18: "F18",
        kb.Key.f19: "F19", kb.Key.f20: "F20",
        kb.Key.caps_lock: "Caps Lock",
    }
    return labels.get(key, key.name.replace("_", " ").title())

def _is_blocked_ui_key(key) -> bool:
    """Return True if this key must not be used as a UI shortcut."""
    if key in _BLOCKED_UI_KEYS:
        return True
    # Also block digit keycodes that may come as KeyCode rather than Key
    if isinstance(key, kb.KeyCode) and key.char and key.char in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return True
    return False

_combo_triggered = False   # prevent repeated triggers while held

def on_kb_press(key):
    global _combo_triggered, _kb_cap_mods, _kb_cap_trigger, _kb_cap_trigger_name

    # Always track held modifiers
    if key in _COMBO_MODIFIERS:
        _held_modifiers.add(key)

    # ── Keyboard capture mode ──────────────────────────────────────────────
    if state["capturing"] and state["capturing_type"] == "keyboard":
        key_name = _kb_key_name(key)
        is_mod = key in _MODIFIER_KEYS
        if is_mod:
            _kb_cap_mods.add(key)
            state["kb_preview"] = _kb_preview_label()
            state["capture_error"] = None
        else:
            if not key_name:
                return
            _kb_cap_trigger = key
            _kb_cap_trigger_name = key_name
            state["kb_preview"] = _kb_preview_label()
            # Validate immediately — reject blocked keys inline
            mod_names = {_kb_key_name(m) for m in _kb_cap_mods}
            status, msg = _validate_kb_hotkey(mod_names, key_name)
            if status == "block":
                state["capture_error"]   = msg
                state["capture_warning"] = None
                _kb_cap_trigger = None
                _kb_cap_trigger_name = ""
                state["kb_preview"] = _kb_preview_label()
            elif status == "warn":
                state["capture_warning"] = msg
                state["capture_error"]   = None
            else:
                state["capture_warning"] = None
                state["capture_error"]   = None
        return

    # ── Combo capture mode ────────────────────────────────────────────────
    if state["capturing"] and state["capturing_type"] == "combo":
        for combo_key, (label, _) in COMBO_OPTIONS.items():
            if _combo_is_active(combo_key):
                config["hotkey"] = combo_key
                config["hotkey_label"] = label
                config["hotkey_type"] = "combo"
                save_config(config); state["capturing"] = False
                print(f"✅ Combo set: {label}")
        return

    # ── UI shortcut capture ───────────────────────────────────────────────
    if state["capturing_ui"]:
        if key in _MODIFIER_KEYS:
            return
        if _is_blocked_ui_key(key):
            state["capturing_ui_error"] = "That key is reserved. Try F13–F20 or a symbol key (` - = [ ] \\ ; ' , . /)."
            return
        state.pop("capturing_ui_error", None)
        key_name  = key.name if isinstance(key, kb.Key) else (key.char or str(key))
        key_label = _key_label(key) if isinstance(key, kb.Key) else (key.char.upper() if key.char else str(key))
        config["ui_shortcut"] = key_name; config["ui_shortcut_label"] = key_label
        save_config(config); state["capturing_ui"] = False
        print(f"✅ UI shortcut set: {key_label}"); return

    # ── UI shortcut trigger ───────────────────────────────────────────────
    ui_key = get_ui_hotkey()
    if ui_key and key == ui_key:
        handle_ui_shortcut()
        return

    # ── Keyboard hotkey trigger ───────────────────────────────────────────
    if config.get("hotkey_type") == "keyboard":
        hk = config.get("hotkey", "")
        if hk:
            req_mods, trig_name, trig_key = _parse_kb_hotkey(hk)
            if trig_key is not None and key == trig_key and _mods_satisfied(req_mods):
                handle_trigger_press()
        return

    # ── 3-modifier combo trigger ──────────────────────────────────────────
    if config.get("hotkey_type") == "combo":
        if not _combo_triggered and _combo_is_active(config.get("hotkey", "cmd+shift+alt")):
            _combo_triggered = True
            handle_trigger_press()

def on_kb_release(key):
    global _combo_triggered, _kb_cap_mods, _kb_cap_trigger, _kb_cap_trigger_name

    # ── Keyboard capture: finalize on release ─────────────────────────────
    if state["capturing"] and state["capturing_type"] == "keyboard":
        key_name = _kb_key_name(key)
        is_mod = key in _MODIFIER_KEYS

        if not is_mod and _kb_cap_trigger is not None and key == _kb_cap_trigger:
            # User released the trigger key → finalize capture
            mod_names = {_kb_key_name(m) for m in _kb_cap_mods}
            status, msg = _validate_kb_hotkey(mod_names, key_name)
            if status == "block":
                state["capture_error"] = msg
                state["capture_warning"] = None
                _kb_cap_trigger = None; _kb_cap_trigger_name = ""
            else:
                # Build hotkey string
                parts = sorted(mod_names, key=lambda n: ("cmd" in n, "ctrl" in n, "alt" in n, "shift" in n)) + [key_name]
                hk_str = "+".join(parts)
                hk_label = _kb_hotkey_label(hk_str)
                config["hotkey"] = hk_str
                config["hotkey_label"] = hk_label
                config["hotkey_type"] = "keyboard"
                save_config(config)
                state["capturing"] = False
                state["kb_preview"] = ""
                state["capture_warning"] = msg if status == "warn" else None
                state["capture_error"] = None
                _kb_cap_mods = set(); _kb_cap_trigger = None; _kb_cap_trigger_name = ""
                print(f"✅ Keyboard hotkey set: {hk_label}")
            return

        if is_mod:
            _kb_cap_mods.discard(key)
            # If only modifier keys held (no trigger) and all released → capture single modifier
            if not _kb_cap_trigger and not _kb_cap_mods:
                if key_name in ("alt_r", "cmd_r", "ctrl_r") or key_name.startswith("f"):
                    status, msg = _validate_kb_hotkey(set(), key_name)
                    if status == "block":
                        state["capture_error"] = msg
                        state["capture_warning"] = None
                    else:
                        hk_str = key_name
                        hk_label = _kb_hotkey_label(hk_str)
                        config["hotkey"] = hk_str
                        config["hotkey_label"] = hk_label
                        config["hotkey_type"] = "keyboard"
                        save_config(config)
                        state["capturing"] = False
                        state["kb_preview"] = ""
                        state["capture_warning"] = msg if status == "warn" else None
                        state["capture_error"] = None
                        _kb_cap_trigger_name = ""
                        print(f"✅ Keyboard hotkey set: {hk_label}")
            state["kb_preview"] = _kb_preview_label()
        return

    # Update held modifiers
    if key in _COMBO_MODIFIERS:
        _held_modifiers.discard(key)

    # ── Keyboard hotkey release ───────────────────────────────────────────
    if config.get("hotkey_type") == "keyboard":
        hk = config.get("hotkey", "")
        if hk:
            _, trig_name, trig_key = _parse_kb_hotkey(hk)
            if trig_key is not None and key == trig_key:
                handle_trigger_release()
        return

    # ── Combo trigger release ─────────────────────────────────────────────
    if config.get("hotkey_type") == "combo":
        if _combo_triggered and key in _COMBO_MODIFIERS:
            _combo_triggered = False
            handle_trigger_release()

# ── MOUSE LISTENER ────────────────────────────────────────────────────────────

MOUSE_BUTTON_LABELS = {
    "Button.left": "Left Click", "Button.right": "Right Click",
    "Button.middle": "Middle Click", "Button.x1": "Mouse Button 4", "Button.x2": "Mouse Button 5",
}

def on_ms_click(x, y, button, pressed):
    btn_str = str(button)
    if state["capturing"] and state.get("capturing_type") == "mouse" and pressed:
        config["hotkey"] = btn_str
        config["hotkey_label"] = MOUSE_BUTTON_LABELS.get(btn_str, btn_str)
        config["hotkey_type"] = "mouse"
        save_config(config); state["capturing"] = False
        print(f"✅ Hotkey set: {config['hotkey_label']}"); return
    if state["capturing_ui"] and pressed:
        config["ui_shortcut"] = btn_str
        config["ui_shortcut_label"] = MOUSE_BUTTON_LABELS.get(btn_str, btn_str)
        save_config(config); state["capturing_ui"] = False
        print(f"✅ UI shortcut set: {config['ui_shortcut_label']}"); return
    if config.get("hotkey_type") == "mouse" and btn_str == config.get("hotkey"):
        if pressed: handle_trigger_press()
        else:       handle_trigger_release()

def _accessibility_granted() -> bool:
    """Return True if this process has Accessibility permission (no dialog)."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": False}))
    except Exception:
        return True  # Unknown — assume OK to avoid false negatives

def _mic_granted() -> bool:
    """Return True if microphone permission is granted."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        return status == 3  # 3 = authorized
    except Exception:
        return True  # Unknown — assume OK

def start_listener():
    global _kb_listener, _ms_listener
    for l in [_kb_listener, _ms_listener]:
        if l:
            try: l.stop()
            except: pass
    _kb_listener = kb.Listener(on_press=on_kb_press, on_release=on_kb_release)
    _kb_listener.daemon = True; _kb_listener.start()
    _ms_listener = ms.Listener(on_click=on_ms_click)
    _ms_listener.daemon = True; _ms_listener.start()

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({
        "enabled": state["enabled"], "recording": state["recording"],
        "transcribing": state["transcribing"], "capturing": state["capturing"],
        "capturing_ui": state["capturing_ui"],
        "capturing_ui_error": state.pop("capturing_ui_error", None),
        "capturing_type":  state.get("capturing_type", "combo"),
        "kb_preview":      state.get("kb_preview", ""),
        "capture_warning": state.pop("capture_warning", None),
        "capture_error":   state.pop("capture_error", None),
        "mic_testing": state["mic_testing"], "mic_level": state["mic_level"],
        "overlay_text": state["overlay_text"],
        "config": config, "history": state["history"], "stats": load_stats(),
        "permissions": {"accessibility": _accessibility_granted(), "mic": _mic_granted()},
    })

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    state["enabled"] = not state["enabled"]
    if not state["enabled"] and state["recording"]:
        threading.Thread(target=stop_and_transcribe, daemon=True).start()
    return jsonify({"enabled": state["enabled"]})

@app.route("/api/config", methods=["POST"])
def api_config():
    config.update(request.json); save_config(config)
    return jsonify(config)

@app.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    global _kb_cap_mods, _kb_cap_trigger, _kb_cap_trigger_name
    body = request.get_json(silent=True) or {}
    ctype = body.get("type", "combo")
    state["capturing"] = True
    state["capturing_type"] = ctype
    state["kb_preview"] = ""
    state["capture_warning"] = None
    state["capture_error"] = None
    if ctype == "keyboard":
        _kb_cap_mods = set()
        _kb_cap_trigger = None
        _kb_cap_trigger_name = ""
    return jsonify({"capturing": True, "capturing_type": ctype})

@app.route("/api/capture/cancel", methods=["POST"])
def api_capture_cancel():
    global _kb_cap_mods, _kb_cap_trigger, _kb_cap_trigger_name
    state["capturing"] = False
    state["kb_preview"] = ""
    state["capture_warning"] = None
    state["capture_error"] = None
    _kb_cap_mods = set(); _kb_cap_trigger = None; _kb_cap_trigger_name = ""
    return jsonify({"capturing": False})

@app.route("/api/capture_ui/start",  methods=["POST"])
def api_capture_ui_start():  state["capturing_ui"] = True;  return jsonify({"capturing_ui": True})

@app.route("/api/capture_ui/cancel", methods=["POST"])
def api_capture_ui_cancel(): state["capturing_ui"] = False; return jsonify({"capturing_ui": False})

@app.route("/api/combo_options", methods=["GET"])
def api_combo_options():
    return jsonify([{"key": k, "label": v[0]} for k, v in COMBO_OPTIONS.items()])

@app.route("/api/combo/status", methods=["GET"])
def api_combo_status():
    """Return which combo modifier keys are currently held."""
    held = []
    if any(k in _held_modifiers for k in (kb.Key.cmd, kb.Key.cmd_r)):
        held.append("cmd")
    if any(k in _held_modifiers for k in (kb.Key.shift, kb.Key.shift_r)):
        held.append("shift")
    if any(k in _held_modifiers for k in (kb.Key.alt, kb.Key.alt_r)):
        held.append("alt")
    if any(k in _held_modifiers for k in (kb.Key.ctrl, kb.Key.ctrl_r)):
        held.append("ctrl")
    active = _combo_is_active(config.get("hotkey", "cmd+shift+alt"))
    return jsonify({"held": held, "active": active})

@app.route("/api/sounds", methods=["GET"])
def api_sounds():
    return jsonify(["None"] + SYSTEM_SOUNDS)

@app.route("/api/stats/weekly", methods=["GET"])
def api_stats_weekly():
    return jsonify(get_weekly_stats())

@app.route("/api/mic/start", methods=["POST"])
def api_mic_start():
    threading.Thread(target=start_mic_test, daemon=True).start()
    return jsonify({"mic_testing": True})

@app.route("/api/mic/stop", methods=["POST"])
def api_mic_stop():
    stop_mic_test(); return jsonify({"mic_testing": False})

@app.route("/api/mic/reset", methods=["POST"])
def api_mic_reset():
    _close_stream()
    return jsonify({"ok": True})

@app.route("/api/mic/devices", methods=["GET"])
def api_mic_devices():
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        result = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                result.append({
                    "index": i,
                    "name": d["name"],
                    "default": (i == default_in),
                })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/vocab", methods=["GET"])
def api_vocab_get(): return jsonify(config.get("vocabulary", []))

@app.route("/api/vocab", methods=["POST"])
def api_vocab_set():
    config["vocabulary"] = request.json; save_config(config)
    return jsonify(config["vocabulary"])

@app.route("/api/app_tones", methods=["GET"])
def api_app_tones_get(): return jsonify(config.get("app_tones", {}))

@app.route("/api/app_tones", methods=["POST"])
def api_app_tones_set():
    config["app_tones"] = request.json; save_config(config)
    return jsonify(config["app_tones"])

@app.route("/api/history/clear", methods=["POST"])
def api_clear_history():
    state["history"] = []
    save_history([])
    return jsonify({"ok": True})

@app.route("/api/history/repaste/<int:idx>", methods=["POST"])
def api_history_repaste(idx):
    try:
        entry = state["history"][idx]
        text  = entry.get("cleaned") or entry.get("raw", "")
        if not text:
            return jsonify({"error": "empty"}), 400
        output_text(text)
        return jsonify({"ok": True})
    except IndexError:
        return jsonify({"error": "not found"}), 404

@app.route("/api/history/export")
def api_history_export():
    fmt = request.args.get("fmt", "txt")
    h = state["history"]
    if fmt == "csv":
        import csv, io
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["timestamp", "app", "language", "cleaned", "raw"])
        for e in h:
            w.writerow([e.get("ts",""), e.get("app",""), e.get("lang",""),
                        e.get("cleaned",""), e.get("raw","")])
        return out.getvalue(), 200, {
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename=dictate_history.csv"
        }
    else:
        lines = []
        for e in h:
            lines.append(f"[{e.get('ts','')}] {e.get('app','') or ''}")
            lines.append(e.get("cleaned",""))
            lines.append("")
        return "\n".join(lines), 200, {
            "Content-Type": "text/plain",
            "Content-Disposition": "attachment; filename=dictate_history.txt"
        }

@app.route("/api/settings/export")
def api_settings_export():
    return json.dumps(config, indent=2), 200, {
        "Content-Type": "application/json",
        "Content-Disposition": "attachment; filename=dictate_settings.json"
    }

@app.route("/api/settings/import", methods=["POST"])
def api_settings_import():
    try:
        imported = request.get_json(force=True)
        if not isinstance(imported, dict):
            return jsonify({"error": "Invalid settings file"}), 400
        # Merge imported over defaults — only accept known keys
        allowed = set(DEFAULT_CONFIG.keys())
        for k, v in imported.items():
            if k in allowed:
                config[k] = v
        save_config(config)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/languages", methods=["GET"])
def api_languages():
    return jsonify(LANGUAGES)

@app.route("/api/version")
def api_version():
    latest = None
    try:
        resp = urllib.request.urlopen(f"{GITHUB_RAW}/version.txt", timeout=5)
        latest = resp.read().decode().strip()
    except Exception:
        pass
    return jsonify({"current": APP_VERSION, "latest": latest,
                    "update_available": bool(latest and latest != APP_VERSION)})

@app.route("/api/open_settings", methods=["POST"])
def api_open_settings():
    """Signal app.py to open the settings window."""
    settings_py = os.path.join(_DATA_DIR, "settings_window.py")
    if not os.path.exists(settings_py):
        settings_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_window.py")
    venv_py = os.path.join(_DATA_DIR, "venv", "bin", "python3")
    if not os.path.exists(venv_py):
        venv_py = sys.executable
    subprocess.Popen(["arch", "-arm64", venv_py, settings_py])
    return jsonify({"ok": True})

@app.route("/api/onboarding/complete", methods=["POST"])
def api_onboarding_complete():
    config["onboarding_done"] = True; save_config(config)
    return jsonify({"ok": True})

@app.route("/api/launch_at_login", methods=["GET", "POST"])
def api_launch_at_login():
    if request.method == "GET":
        return jsonify({"enabled": os.path.exists(LAUNCH_AGENT_PLIST)})
    enabled = (request.json or {}).get("enabled", False)
    if enabled:
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PLIST), exist_ok=True)
        with open(LAUNCH_AGENT_PLIST, "w") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>com.dictate.app</string>\n'
                '  <key>ProgramArguments</key>\n'
                '  <array><string>/usr/bin/open</string>'
                '<string>-a</string><string>Dictate</string></array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n'
            )
        subprocess.run(["launchctl", "load", LAUNCH_AGENT_PLIST], capture_output=True)
    else:
        try:
            subprocess.run(["launchctl", "unload", LAUNCH_AGENT_PLIST], capture_output=True)
            os.unlink(LAUNCH_AGENT_PLIST)
        except Exception:
            pass
    config["launch_at_login"] = enabled
    save_config(config)
    return jsonify({"enabled": os.path.exists(LAUNCH_AGENT_PLIST)})

@app.route("/api/errors")
def api_errors():
    try:
        with open(ERROR_LOG) as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-50:])})  # last 50 entries
    except Exception:
        return jsonify({"log": ""})

@app.route("/api/errors/clear", methods=["POST"])
def api_errors_clear():
    try: open(ERROR_LOG, "w").close()
    except Exception: pass
    return jsonify({"ok": True})

@app.route("/popover")
def popover():
    return POPOVER_HTML

@app.route("/")
def index(): return HTML

# ── POPOVER HTML ─────────────────────────────────────────────────────────────

POPOVER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  :root{--bg:transparent;--surface:#1c1c1e;--border:rgba(255,255,255,.10);--text:#f0f0f0;--dim:#888;--amber:#f59e0b}
  @media(prefers-color-scheme:light){:root{--surface:#f5f5f7;--border:rgba(0,0,0,.10);--text:#1a1a1a;--dim:#666}}
  body{font-family:-apple-system,sans-serif;font-size:13px;color:var(--text);background:transparent;padding:12px;-webkit-font-smoothing:antialiased;user-select:none}
  .row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
  .row:last-child{margin-bottom:0}
  .label{color:var(--dim);font-size:11px;letter-spacing:.04em;flex:1}
  .val{font-weight:500;color:var(--text)}
  .toggle-wrap{display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--surface);border-radius:10px;margin-bottom:10px;border:1px solid var(--border)}
  .toggle-label{flex:1;font-size:13px;font-weight:500}
  .toggle{position:relative;width:36px;height:22px;flex-shrink:0}
  .toggle input{opacity:0;width:0;height:0}
  .slider{position:absolute;inset:0;border-radius:11px;background:#555;transition:.2s;cursor:pointer}
  .slider:before{content:"";position:absolute;width:18px;height:18px;left:2px;bottom:2px;background:#fff;border-radius:50%;transition:.2s}
  input:checked+.slider{background:var(--amber)}
  input:checked+.slider:before{transform:translateX(14px)}
  .last-text{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--dim);min-height:36px;display:flex;align-items:center;justify-content:space-between;gap:8px}
  .last-text .snippet{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
  .last-text .snippet.empty{color:var(--dim);font-style:italic}
  .copy-btn{background:none;border:none;color:var(--dim);cursor:pointer;font-size:11px;padding:2px 6px;border-radius:5px;flex-shrink:0}
  .copy-btn:hover{background:rgba(128,128,128,.15);color:var(--text)}
  .stats{display:flex;gap:6px;margin-bottom:10px}
  .stat{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 8px;text-align:center}
  .stat .n{font-size:16px;font-weight:600;color:var(--amber)}
  .stat .s{font-size:10px;color:var(--dim);margin-top:1px}
  .open-btn{width:100%;padding:9px;background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.25);border-radius:10px;color:var(--amber);font-size:13px;font-weight:500;cursor:pointer;transition:all .15s}
  .open-btn:hover{background:rgba(245,158,11,.2)}
  .recording-dot{width:7px;height:7px;border-radius:50%;background:#ef4444;display:inline-block;animation:blink 1s ease-in-out infinite;margin-right:5px}
  @keyframes blink{0%,100%{opacity:.3}50%{opacity:1}}
</style>
</head>
<body>
<div class="toggle-wrap">
  <span id="statusDot"></span>
  <span class="toggle-label" id="toggleLabel">Dictation</span>
  <label class="toggle">
    <input type="checkbox" id="enableToggle" onchange="toggleEnabled()">
    <span class="slider"></span>
  </label>
</div>
<div class="last-text">
  <span class="snippet" id="lastSnippet"><span class="empty">No transcriptions yet</span></span>
  <button class="copy-btn" onclick="copyLast()" title="Copy">⌘C</button>
</div>
<div class="stats">
  <div class="stat"><div class="n" id="popWords">0</div><div class="s">words today</div></div>
  <div class="stat"><div class="n" id="popSessions">0</div><div class="s">sessions</div></div>
</div>
<button class="open-btn" onclick="openSettings()">Open Dictate Settings →</button>
<script>
let _lastText = '';
async function refresh() {
  try {
    const d = await (await fetch('http://127.0.0.1:5001/api/status')).json();
    const en = d.enabled;
    document.getElementById('enableToggle').checked = en;
    const lbl = document.getElementById('toggleLabel');
    const dot = document.getElementById('statusDot');
    if (d.recording) {
      dot.innerHTML = '<span class="recording-dot"></span>';
      lbl.textContent = 'Recording…';
    } else {
      dot.innerHTML = '';
      lbl.textContent = en ? 'Enabled' : 'Disabled';
    }
    const h = d.history;
    const snip = document.getElementById('lastSnippet');
    if (h && h.length) {
      _lastText = h[0].cleaned || h[0].text || '';
      const short = _lastText.length > 60 ? _lastText.slice(0, 60) + '…' : _lastText;
      snip.innerHTML = escHtml(short);
    }
    if (d.stats) {
      document.getElementById('popWords').textContent = d.stats.words_today || 0;
      document.getElementById('popSessions').textContent = d.stats.sessions_today || 0;
    }
  } catch(e) {}
}
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
async function toggleEnabled() {
  const en = document.getElementById('enableToggle').checked;
  await fetch('http://127.0.0.1:5001/api/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:en})});
  refresh();
}
function copyLast() {
  if (_lastText) navigator.clipboard.writeText(_lastText).catch(()=>{});
}
async function openSettings() {
  await fetch('http://127.0.0.1:5001/api/open_settings', {method:'POST'});
  window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.closePopover && window.webkit.messageHandlers.closePopover.postMessage('close');
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

# ── HTML UI ───────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dictate</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

  /* ── Theme ── */
  :root{
    --bg:#080808;--surface:#111111;--border:#222222;--muted:#333333;
    --text:#e8e8e8;--dim:#666666;--amber:#f59e0b;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;
    --overlay-bg:rgba(10,10,10,0.85);
  }
  [data-theme="light"]{
    --bg:#f4f4f4;--surface:#ffffff;--border:#e0e0e0;--muted:#cccccc;
    --text:#111111;--dim:#888888;--overlay-bg:rgba(240,240,240,0.85);
  }

  html,body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;font-size:13px;min-height:100vh;line-height:1.5;transition:background .2s,color .2s}
  .app{max-width:100%;padding:0}

  /* ── Header ── */
  .header{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:0;padding-bottom:0;border-bottom:none}
  .wordmark{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px}
  .wordmark span{color:var(--amber)}
  .header-right{display:flex;align-items:center;gap:12px}
  .version-badge{font-size:11px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
  .theme-toggle{background:none;border:1px solid var(--border);border-radius:20px;padding:4px 10px;cursor:pointer;font-size:12px;color:var(--dim);font-family:'JetBrains Mono',monospace;transition:all .15s}
  .theme-toggle:hover{border-color:var(--amber);color:var(--amber)}
  /* ── Header waveform ── */
  .header-wave{display:flex;align-items:center;gap:3px;height:22px;opacity:0;transition:opacity .25s}
  .header-wave.active{opacity:1}
  .hw-bar{width:3px;border-radius:2px;background:var(--amber);height:4px;transform-origin:center}
  .header-wave.active .hw-bar:nth-child(1){animation:hw 0.9s ease-in-out -0.4s infinite}
  .header-wave.active .hw-bar:nth-child(2){animation:hw 0.9s ease-in-out -0.2s infinite}
  .header-wave.active .hw-bar:nth-child(3){animation:hw 0.9s ease-in-out  0.0s infinite}
  .header-wave.active .hw-bar:nth-child(4){animation:hw 0.9s ease-in-out -0.3s infinite}
  .header-wave.active .hw-bar:nth-child(5){animation:hw 0.9s ease-in-out -0.1s infinite}
  @keyframes hw{0%,100%{height:4px}50%{height:18px}}

  /* ── Power ── */
  .power-section{display:flex;align-items:center;gap:32px;margin-bottom:40px;padding:28px 32px;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.15)}
  .power-btn{width:72px;height:72px;border-radius:50%;border:2px solid var(--muted);background:transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0;position:relative}
  .power-btn svg{transition:all .2s}
  .power-btn:hover{border-color:var(--amber)}.power-btn:hover svg{stroke:var(--amber)}
  .power-btn.on{border-color:var(--green);box-shadow:0 0 0 4px rgba(34,197,94,.12),0 0 24px rgba(34,197,94,.2)}
  .power-btn.on svg{stroke:var(--green)}
  .power-btn.on::before{content:'';position:absolute;inset:-6px;border-radius:50%;border:1px solid rgba(34,197,94,.2);animation:ring-pulse 2s ease infinite}
  .power-info{flex:1}
  .power-status{font-family:'Syne',sans-serif;font-size:20px;font-weight:700;margin-bottom:4px}
  .power-status.on{color:var(--green)}.power-status.off{color:var(--dim)}
  .power-hint{color:var(--dim);font-size:12px}
  .indicator{display:flex;align-items:center;gap:10px;padding:8px 14px;border:1px solid var(--border);border-radius:3px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);transition:all .2s}
  .indicator-dot{width:7px;height:7px;border-radius:50%;background:var(--muted);transition:all .2s}
  .indicator.recording{border-color:var(--red);color:var(--red)}
  .indicator.recording .indicator-dot{background:var(--red);animation:dot-pulse .8s ease infinite}
  .indicator.transcribing{border-color:var(--amber);color:var(--amber)}
  .indicator.transcribing .indicator-dot{background:var(--amber);animation:dot-pulse 1s ease infinite}

  /* ── Stats ── */
  .stats-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
  .stat-value{font-family:'Syne',sans-serif;font-size:22px;font-weight:700;color:var(--amber);margin-bottom:2px}
  .stat-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
  .chart-bar{flex:1;background:rgba(245,158,11,.18);border-radius:3px 3px 0 0;min-height:2px;transition:height .3s ease;cursor:default;position:relative}
  .chart-bar.today{background:rgba(245,158,11,.55)}
  .chart-bar:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 4px);left:50%;transform:translateX(-50%);background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:2px 7px;font-size:10px;color:var(--text);white-space:nowrap;pointer-events:none;z-index:10}
  .chart-label{flex:1;text-align:center;font-size:9px;color:var(--dim);letter-spacing:.03em}

  /* ── Tabs ── */
  .app-shell{display:flex;flex-direction:column;min-height:100vh}
  .app-header{display:flex;align-items:flex-end;justify-content:space-between;padding:20px 24px 16px;border-bottom:1px solid var(--border);flex-shrink:0}
  .app-body{display:flex;flex:1;overflow:hidden;min-height:0}
  .sidebar{width:160px;flex-shrink:0;border-right:1px solid var(--border);padding:12px 8px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
  .nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;border:none;background:transparent;color:var(--dim);cursor:pointer;font-family:-apple-system,sans-serif;font-size:13px;text-align:left;width:100%;transition:all .15s}
  .nav-item:hover{background:var(--surface);color:var(--text)}
  .nav-item.active{background:rgba(245,158,11,.1);color:var(--amber)}
  .nav-item svg{flex-shrink:0;opacity:.7}
  .nav-item.active svg{opacity:1}
  .content{flex:1;overflow-y:auto;padding:24px}
  .tab-panel{display:none}.tab-panel.active{display:block}

  /* ── Fields ── */
  .section-label{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--dim);margin-bottom:12px;padding-left:2px}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .field{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;transition:border-color .15s,box-shadow .15s;box-shadow:0 1px 3px rgba(0,0,0,.12)}
  .field:hover{border-color:var(--muted);box-shadow:0 2px 8px rgba(0,0,0,.18)}
  .field-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
  select{width:100%;background:transparent;border:none;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;cursor:pointer;appearance:none;-webkit-appearance:none}
  select option{background:var(--surface)}

  /* ── Toggles ── */
  .toggle-row{display:flex;align-items:center;justify-content:space-between}
  .toggle-switch{position:relative;width:36px;height:20px;flex-shrink:0}
  .toggle-switch input{opacity:0;width:0;height:0}
  .toggle-slider{position:absolute;inset:0;background:var(--muted);border-radius:20px;cursor:pointer;transition:.2s}
  .toggle-slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:var(--dim);border-radius:50%;transition:.2s}
  .toggle-switch input:checked+.toggle-slider{background:rgba(245,158,11,.3)}
  .toggle-switch input:checked+.toggle-slider::before{transform:translateX(16px);background:var(--amber)}
  .toggle-label{font-size:13px;color:var(--text)}.toggle-label.off{color:var(--dim)}

  /* ── Hotkey ── */
  .hotkey-field{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;transition:border-color .15s}
  .hotkey-field.capturing{border-color:var(--amber);animation:capture-pulse 1s ease infinite}
  .htab{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--dim);padding:4px 10px;font-size:11px;font-weight:500;cursor:pointer;transition:all .15s}
  .htab.active{background:var(--amber);border-color:var(--amber);color:#1a1006;font-weight:600}
  .htab:hover:not(.active){border-color:var(--amber);color:var(--amber)}
  .hotkey-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .hotkey-value{color:var(--text);font-size:13px;flex:1}
  .hotkey-value.capturing{color:var(--amber)}
  .capture-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 10px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;white-space:nowrap}
  .capture-btn:hover{color:var(--text);background:#444}.capture-btn.active{background:rgba(245,158,11,.15);color:var(--amber)}

  /* ── Tone ── */
  .tone-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
  .tone-btn{padding:10px 0;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--dim);font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;transition:all .15s;text-align:center}
  .tone-btn:hover{border-color:var(--muted);color:var(--text)}.tone-btn.active{border-color:var(--amber);color:var(--amber);background:rgba(245,158,11,.06)}
  .tone-btn.disabled{opacity:.3;pointer-events:none}

  /* ── Mic ── */
  .mic-section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:12px}
  .mic-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  .mic-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:var(--muted);border:none;border-radius:3px;padding:5px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;color:var(--dim)}
  .mic-btn:hover{color:var(--text);background:#444}.mic-btn.testing{background:rgba(239,68,68,.15);color:var(--red)}
  .meter-track{height:8px;background:var(--muted);border-radius:4px;overflow:hidden}
  .meter-fill{height:100%;width:0%;border-radius:4px;background:linear-gradient(90deg,var(--green) 0%,var(--amber) 70%,var(--red) 100%);transition:width .05s ease}
  .meter-hint{font-size:10px;color:var(--dim);margin-top:8px;letter-spacing:.05em}

  /* ── Vocab ── */
  .vocab-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
  .vocab-row{display:flex;align-items:center;gap:8px}
  .vocab-input{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:8px 10px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none}
  .vocab-arrow{color:var(--dim);font-size:14px;flex-shrink:0}
  .vocab-del{background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;padding:0 4px;line-height:1;transition:color .15s;flex-shrink:0}
  .vocab-del:hover{color:var(--red)}
  .add-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:8px 14px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;width:100%}
  .add-btn:hover{color:var(--text);background:#444}

  /* ── App tones ── */
  .app-tone-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
  .app-tone-row{display:grid;grid-template-columns:1fr auto 120px auto;align-items:center;gap:8px}
  .app-tone-input,.app-tone-select{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:8px 10px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none}
  .app-tone-select{appearance:none;-webkit-appearance:none;cursor:pointer}
  .app-hint{font-size:11px;color:var(--dim);margin-bottom:12px;line-height:1.6}

  /* ── Save ── */
  .save-btn{width:100%;padding:12px;background:transparent;border:1px solid var(--muted);border-radius:4px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:8px}
  .save-btn:hover{border-color:var(--amber);color:var(--amber)}.save-btn.saved{border-color:var(--green);color:var(--green)}

  .divider{height:1px;background:var(--border);margin:32px 0}

  /* ── History ── */
  .history-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
  .history-search{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:12px;outline:none;margin-bottom:12px;font-family:inherit;transition:border-color .15s}
  .history-search:focus{border-color:var(--amber)}
  .history-search::placeholder{color:var(--dim)}
  .history-copy-btn{font-size:10px;letter-spacing:.05em;text-transform:uppercase;background:transparent;border:1px solid var(--border);border-radius:5px;padding:3px 8px;cursor:pointer;color:var(--dim);font-family:-apple-system,sans-serif;transition:all .15s;flex-shrink:0}
  .history-copy-btn:hover{border-color:var(--amber);color:var(--amber)}
  .history-copy-btn.copied{border-color:var(--green);color:var(--green)}
  .clear-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:none;border:none;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:color .15s}
  .clear-btn:hover{color:var(--red)}
  .history-list{display:flex;flex-direction:column;gap:8px}
  .history-item{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;animation:slide-in .2s ease}
  .history-meta{font-size:10px;color:var(--dim);margin-bottom:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .badge{font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:2px 6px;border-radius:2px}
  .badge.raw-badge{background:rgba(102,102,102,.2);color:var(--dim)}
  .badge.clean-badge{background:rgba(245,158,11,.1);color:var(--amber)}
  .badge.app-badge{background:rgba(59,130,246,.1);color:var(--blue)}
  .badge.lang-badge{background:rgba(34,197,94,.1);color:var(--green)}
  .history-text{color:var(--text);line-height:1.6}
  .history-raw-text{font-size:11px;color:var(--dim);margin-top:4px}
  .empty-state{text-align:center;color:var(--dim);padding:40px 0;font-size:12px}

  /* ── Overlay preview ── */
  .overlay-preview{
    background:var(--overlay-bg);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
    border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:14px 20px;
    font-size:15px;color:var(--text);text-align:center;margin-bottom:8px;
    min-height:48px;display:flex;align-items:center;justify-content:center;
    transition:all .2s;
  }
  .overlay-preview.recording{border-color:rgba(245,158,11,.4);box-shadow:0 0 20px rgba(245,158,11,.1)}

  /* ── Onboarding modal ── */
  .modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:1000;display:flex;align-items:center;justify-content:center}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:40px;max-width:480px;width:90%;position:relative}
  .modal-step{display:none}.modal-step.active{display:block}
  .modal-title{font-family:'Syne',sans-serif;font-size:24px;font-weight:800;margin-bottom:8px}
  .modal-subtitle{color:var(--dim);font-size:13px;margin-bottom:32px;line-height:1.6}
  .modal-icon{font-size:48px;margin-bottom:20px;text-align:center;display:flex;align-items:center;justify-content:center}
  .step-indicators{display:flex;gap:6px;justify-content:center;margin-bottom:28px}
  .step-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);transition:all .2s}
  .step-dot.active{background:var(--amber);transform:scale(1.2)}
  .step-dot.done{background:var(--green)}
  .modal-btn{width:100%;padding:12px;background:var(--amber);border:none;border-radius:4px;color:#000;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:8px}
  .modal-btn:hover{background:#e08e00}
  .modal-btn-skip{width:100%;padding:10px;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--dim);font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:8px}
  .modal-btn-skip:hover{border-color:var(--muted);color:var(--text)}

  /* ── Version footer ── */
  .version-footer{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
  .update-banner{display:none;background:rgba(245,158,11,.08);border:1px solid var(--amber);border-radius:4px;padding:12px 16px;margin-bottom:24px;align-items:center;justify-content:space-between;gap:16px}

  @keyframes ring-pulse{0%,100%{opacity:.6;transform:scale(1)}50%{opacity:0;transform:scale(1.15)}}
  @keyframes dot-pulse{0%,100%{opacity:1}50%{opacity:.3}}
  @keyframes slide-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
  @keyframes capture-pulse{0%,100%{border-color:var(--amber)}50%{border-color:rgba(245,158,11,.3)}}
</style>
</head>
<body>

<!-- Onboarding Modal -->
<div class="modal-backdrop" id="onboardingModal" style="display:none">
  <div class="modal">
    <div class="step-indicators">
      <div class="step-dot active" id="dot0"></div>
      <div class="step-dot" id="dot1"></div>
      <div class="step-dot" id="dot2"></div>
    </div>

    <!-- Step 1: Welcome -->
    <div class="modal-step active" id="step0">
      <div class="modal-icon">
        <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">
          <rect x="28" y="8" width="24" height="36" rx="12" fill="#f59e0b"/>
          <path d="M18 38 Q18 60 40 60 Q62 60 62 38" stroke="#f59e0b" stroke-width="3" fill="none" stroke-linecap="round"/>
          <line x1="40" y1="60" x2="40" y2="70" stroke="#f59e0b" stroke-width="3" stroke-linecap="round"/>
          <line x1="28" y1="70" x2="52" y2="70" stroke="#f59e0b" stroke-width="3" stroke-linecap="round"/>
          <path d="M14 26 Q9 37 14 48" stroke="#f59e0b" stroke-width="2.5" fill="none" stroke-linecap="round" opacity="0.7"><animate attributeName="opacity" values="0.7;0.2;0.7" dur="1.4s" repeatCount="indefinite"/></path>
          <path d="M6 20 Q-1 37 6 54" stroke="#f59e0b" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.4"><animate attributeName="opacity" values="0.4;0.1;0.4" dur="1.4s" begin="0.25s" repeatCount="indefinite"/></path>
          <path d="M66 26 Q71 37 66 48" stroke="#f59e0b" stroke-width="2.5" fill="none" stroke-linecap="round" opacity="0.7"><animate attributeName="opacity" values="0.7;0.2;0.7" dur="1.4s" repeatCount="indefinite"/></path>
          <path d="M74 20 Q81 37 74 54" stroke="#f59e0b" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.4"><animate attributeName="opacity" values="0.4;0.1;0.4" dur="1.4s" begin="0.25s" repeatCount="indefinite"/></path>
        </svg>
      </div>
      <div class="modal-title">Welcome to Dictate</div>
      <div class="modal-subtitle">System-wide AI dictation for your Mac. Let's get you set up in 3 quick steps.</div>
      <button class="modal-btn" onclick="nextStep()">Get Started →</button>
      <button class="modal-btn-skip" onclick="skipOnboarding()">Skip setup</button>
    </div>

    <!-- Step 2: Hotkey -->
    <div class="modal-step" id="step1">
      <div class="modal-icon">
        <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">
          <rect x="6" y="22" width="68" height="38" rx="7" fill="none" stroke="#f59e0b" stroke-width="2.5"/>
          <rect x="14" y="30" width="11" height="9" rx="2.5" fill="#f59e0b" opacity="0.9"><animate attributeName="opacity" values="0.9;0.3;0.9" dur="0.9s" repeatCount="indefinite"/></rect>
          <rect x="29" y="30" width="11" height="9" rx="2.5" fill="#f59e0b" opacity="0.45"/>
          <rect x="44" y="30" width="11" height="9" rx="2.5" fill="#f59e0b" opacity="0.45"/>
          <rect x="59" y="30" width="11" height="9" rx="2.5" fill="#f59e0b" opacity="0.45"/>
          <rect x="14" y="43" width="8" height="9" rx="2.5" fill="#f59e0b" opacity="0.35"/>
          <rect x="26" y="43" width="28" height="9" rx="2.5" fill="#f59e0b" opacity="0.35"/>
          <rect x="58" y="43" width="12" height="9" rx="2.5" fill="#f59e0b" opacity="0.35"/>
        </svg>
      </div>
      <div class="modal-title">Set Your Shortcut</div>
      <div class="modal-subtitle">Choose a 3-modifier combo to trigger dictation. These combos are safe — they won't conflict with normal typing or system shortcuts.</div>
      <div class="hotkey-field" id="onboardHotkeyField" style="margin-bottom:16px">
        <div class="field-label">Dictation Combo</div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="onboardHotkeyValue">⌘⇧⌥</span>
          <select id="onboardComboSelect" onchange="saveOnboardCombo()" style="background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px;cursor:pointer"></select>
        </div>
      </div>
      <button class="modal-btn" onclick="nextStep()">Next →</button>
    </div>

    <!-- Step 3: Permissions + mic test -->
    <div class="modal-step" id="step2">
      <div class="modal-icon">
        <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">
          <path d="M40 6 L66 17 L66 40 Q66 60 40 74 Q14 60 14 40 L14 17 Z" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linejoin="round"/>
          <path d="M26 40 L35 50 L54 30" stroke="#f59e0b" stroke-width="3.5" fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="40" stroke-dashoffset="40"><animate attributeName="stroke-dashoffset" from="40" to="0" dur="0.7s" begin="0.3s" fill="freeze"/></path>
        </svg>
      </div>
      <div class="modal-title">Grant Permissions</div>
      <div class="modal-subtitle">Dictate needs two permissions to work. Both can be found in <strong>System Settings → Privacy & Security</strong>.</div>
      <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px">
        <div class="field" style="display:flex;align-items:center;gap:12px">
          <span style="font-size:20px">🎙️</span>
          <div><div style="font-size:12px;font-weight:600;color:var(--text)">Microphone</div><div style="font-size:11px;color:var(--dim)">For voice capture</div></div>
        </div>
        <div class="field" style="display:flex;align-items:center;gap:12px">
          <span style="font-size:20px">♿</span>
          <div><div style="font-size:12px;font-weight:600;color:var(--text)">Accessibility</div><div style="font-size:11px;color:var(--dim)">For hotkey + text injection → enable Python & Dictate</div></div>
        </div>
      </div>
      <div class="mic-section" style="margin-bottom:16px">
        <div class="mic-header">
          <span class="field-label" style="margin:0">Quick Mic Test</span>
          <button class="mic-btn" id="onboardMicBtn" onclick="toggleOnboardMic()">Test Mic</button>
        </div>
        <div class="meter-track"><div class="meter-fill" id="onboardMeterFill"></div></div>
        <div class="meter-hint" id="onboardMeterHint">Click Test Mic and speak</div>
      </div>
      <button class="modal-btn" onclick="finishOnboarding()">Done — Start Dictating 🎉</button>
    </div>
  </div>
</div>

<div class="app app-shell">
  <div class="app-header header">
    <div class="wordmark">dict<span>.</span>ate</div>
    <div id="headerWave" class="header-wave">
      <div class="hw-bar"></div><div class="hw-bar"></div><div class="hw-bar"></div>
      <div class="hw-bar"></div><div class="hw-bar"></div>
    </div>
    <div class="header-right">
      <span class="version-badge" id="versionBadge">loading...</span>
      <button class="theme-toggle" onclick="cycleTheme()" id="themeBtn">🌙</button>
    </div>
  </div><!-- end header -->

  <div class="app-body">
  <aside class="sidebar">
    <button class="nav-item active" data-tab="home" onclick="showTab('home')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
      Home
    </button>
    <button class="nav-item" data-tab="history" onclick="showTab('history')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      History
    </button>
    <button class="nav-item" data-tab="general" onclick="showTab('general')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/><path d="M19.07 4.93L4.93 19.07"/></svg>
      General
    </button>
    <button class="nav-item" data-tab="language" onclick="showTab('language')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
      Language
    </button>
    <button class="nav-item" data-tab="overlay" onclick="showTab('overlay')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
      Overlay
    </button>
    <button class="nav-item" data-tab="vocab" onclick="showTab('vocab')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
      Vocabulary
    </button>
    <button class="nav-item" data-tab="apptones" onclick="showTab('apptones')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
      App Tones
    </button>
  </aside>
  <main class="content">

  <!-- Home panel: power + stats -->
  <div class="tab-panel active" id="tab-home">

  <!-- Permissions banner — shown only when a permission is missing -->
  <div id="permBanner" style="display:none;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:12px">
    <div style="font-weight:600;color:var(--amber);margin-bottom:6px">⚠ Permissions needed</div>
    <div id="permItems" style="display:flex;flex-direction:column;gap:5px"></div>
  </div>

  <!-- Power -->
  <div class="power-section">
    <button class="power-btn" id="powerBtn" onclick="togglePower()">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#666" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>
      </svg>
    </button>
    <div class="power-info">
      <div class="power-status off" id="powerStatus">Disabled</div>
      <div class="power-hint" id="powerHint">Click to enable dictation</div>
    </div>
    <div class="indicator" id="indicator">
      <div class="indicator-dot"></div>
      <span id="indicatorText">Idle</span>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-bar">
    <div class="stat-card"><div class="stat-value" id="statWordsToday">0</div><div class="stat-label">Words Today</div></div>
    <div class="stat-card"><div class="stat-value" id="statSessionsToday">0</div><div class="stat-label">Sessions Today</div></div>
    <div class="stat-card"><div class="stat-value" id="statWordsTotal">0</div><div class="stat-label">Words Total</div></div>
    <div class="stat-card"><div class="stat-value" id="statSessionsTotal">0</div><div class="stat-label">Sessions Total</div></div>
  </div>

  <!-- Weekly chart -->
  <div class="card" style="margin-top:16px">
    <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px">
      <span style="font-size:12px;font-weight:600;color:var(--text)">Last 7 Days</span>
      <span style="font-size:11px;color:var(--dim)" id="chartPeak"></span>
    </div>
    <div id="weeklyChart" style="display:flex;align-items:flex-end;gap:6px;height:72px"></div>
    <div id="weeklyLabels" style="display:flex;gap:6px;margin-top:6px"></div>
  </div>

  </div><!-- end tab-home -->

  <div class="tab-panel" id="tab-general">
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Trigger Mode</div>
        <select id="mode" onchange="autoSave()">
          <option value="toggle">Toggle (press once)</option>
          <option value="hold">Hold (hold to record)</option>
        </select>
      </div>
      <div class="hotkey-field" id="hotkeyField">
        <div class="field-label">Dictation Shortcut</div>
        <div style="display:flex;gap:4px;margin-bottom:12px">
          <button class="htab active" id="htab_combo" onclick="switchHotkeyType('combo')">3-Key Combo</button>
          <button class="htab" id="htab_keyboard" onclick="switchHotkeyType('keyboard')">Keyboard</button>
          <button class="htab" id="htab_mouse" onclick="switchHotkeyType('mouse')">Mouse Button</button>
        </div>
        <!-- Combo panel -->
        <div id="htPanel_combo">
          <div class="hotkey-row">
            <span class="hotkey-value" id="hotkeyValue">⌘⇧⌥</span>
            <select id="comboSelect" onchange="saveCombo()" style="background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px;cursor:pointer"></select>
          </div>
          <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
            <span style="font-size:11px;color:var(--dim)">Hold your combo to test:</span>
            <span id="comboTester" style="font-size:12px;color:var(--dim);font-family:'JetBrains Mono',monospace;padding:3px 8px;border-radius:5px;background:var(--surface);border:1px solid var(--border);transition:all .15s">—</span>
          </div>
        </div>
        <!-- Keyboard panel -->
        <div id="htPanel_keyboard" style="display:none">
          <div class="hotkey-row">
            <span class="hotkey-value" id="hotkeyKbValue">Not set</span>
            <button class="capture-btn" id="captureKbBtn" onclick="toggleCaptureKb()">Assign</button>
          </div>
          <div id="hotkeyKbHint" style="font-size:11px;color:var(--dim);margin-top:6px">Press any key combo — modifiers + key, F1–F20, or a right-side modifier alone (⌥›, ⌘›, ⌃›).</div>
          <div id="hotkeyKbWarning" style="font-size:11px;color:#f59e0b;margin-top:6px;display:none"></div>
          <div id="hotkeyKbError" style="font-size:11px;color:#ef4444;margin-top:6px;display:none"></div>
        </div>
        <!-- Mouse panel -->
        <div id="htPanel_mouse" style="display:none">
          <div class="hotkey-row">
            <span class="hotkey-value" id="hotkeyMouseValue">Not set</span>
            <button class="capture-btn" id="captureMsBtn" onclick="toggleCaptureMs()">Assign</button>
          </div>
          <div style="font-size:11px;color:var(--dim);margin-top:6px">Click any mouse button (left, right, middle, or extra buttons).</div>
        </div>
      </div>
    </div>
    <div class="settings-grid">
      <div class="hotkey-field" id="uiShortcutField">
        <div class="field-label">Open UI Shortcut <span style="font-size:10px;color:var(--dim);font-weight:400">(F13–F20 or symbol key)</span></div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="uiShortcutValue">Not set</span>
          <button class="capture-btn" id="captureUiBtn" onclick="toggleCaptureUi()">Assign</button>
        </div>
        <div id="uiShortcutError" style="font-size:11px;color:#ef4444;margin-top:6px;display:none"></div>
      </div>
      <div class="field">
        <div class="field-label">Whisper Model</div>
        <select id="whisper_model" onchange="autoSave()">
          <option value="mlx-community/whisper-tiny-mlx">tiny (fastest)</option>
          <option value="mlx-community/whisper-base-mlx">base (fast)</option>
          <option value="mlx-community/whisper-small-mlx">small (recommended)</option>
          <option value="mlx-community/whisper-medium-mlx">medium (accurate)</option>
          <option value="mlx-community/whisper-large-v3-mlx">large-v3 (best)</option>
        </select>
      </div>
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Ollama Model</div>
        <select id="ollama_model" onchange="autoSave()">
          <option value="qwen2.5:0.5b">qwen2.5:0.5b (fastest)</option>
          <option value="qwen2.5:1.5b">qwen2.5:1.5b (fast)</option>
          <option value="llama3.2:1b">llama3.2:1b (fast)</option>
          <option value="llama3.2">llama3.2 (recommended)</option>
          <option value="mistral">mistral</option>
          <option value="llama3.1">llama3.1</option>
        </select>
      </div>
      <div class="field">
        <div class="field-label">AI Cleanup</div>
        <div class="toggle-row">
          <span class="toggle-label" id="cleanupLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="cleanupToggle" onchange="toggleCleanup()"><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Output Mode</div>
        <div class="toggle-row">
          <span class="toggle-label" id="clipboardLabel">Auto-paste</span>
          <label class="toggle-switch"><input type="checkbox" id="clipboardToggle" onchange="toggleClipboard()"><span class="toggle-slider"></span></label>
        </div>
      </div>
      <div class="field">
        <div class="field-label">Sound Feedback</div>
        <div class="toggle-row">
          <span class="toggle-label" id="soundLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="soundToggle" onchange="toggleSound()"><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>
    <div class="settings-grid" id="soundPickerGrid">
      <div class="field">
        <div class="field-label">Start sound</div>
        <select id="soundStart" onchange="autoSave()"></select>
      </div>
      <div class="field">
        <div class="field-label">Stop sound</div>
        <select id="soundStop" onchange="autoSave()"></select>
      </div>
      <div class="field">
        <div class="field-label">Done sound</div>
        <select id="soundDone" onchange="autoSave()"></select>
      </div>
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Pause Detection</div>
        <div class="toggle-row">
          <span class="toggle-label" id="pauseLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="pauseToggle" onchange="togglePause()"><span class="toggle-slider"></span></label>
        </div>
      </div>
      <div class="field" id="pauseSecondsField">
        <div class="field-label">Silence Threshold</div>
        <input type="range" id="pauseSeconds" min="1" max="5" step="0.5" value="2"
               oninput="updatePauseLabel()" onchange="autoSave()"
               style="width:100%;accent-color:var(--amber);margin-top:4px">
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-top:4px">
          <span>1s</span><span id="pauseSecondsVal">2s</span><span>5s</span>
        </div>
      </div>
    </div>

    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Launch at Login</div>
        <div class="toggle-row">
          <span class="toggle-label" id="launchAtLoginLabel">Off</span>
          <label class="toggle-switch"><input type="checkbox" id="launchAtLoginToggle" onchange="toggleLaunchAtLogin()"><span class="toggle-slider"></span></label>
        </div>
      </div>
      <div class="field">
        <div class="field-label">Remove Filler Words</div>
        <div class="toggle-row">
          <span class="toggle-label" id="fillersLabel">Off</span>
          <label class="toggle-switch"><input type="checkbox" id="fillersToggle" onchange="toggleFillers()"><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>

    <div class="section-label" style="margin-top:8px;">Cleanup Tone</div>
    <div class="tone-grid">
      <button class="tone-btn" data-tone="neutral"      onclick="setTone('neutral')">Neutral</button>
      <button class="tone-btn" data-tone="professional" onclick="setTone('professional')">Professional</button>
      <button class="tone-btn" data-tone="casual"       onclick="setTone('casual')">Casual</button>
      <button class="tone-btn" data-tone="concise"      onclick="setTone('concise')">Concise</button>
    </div>

    <div class="section-label">Microphone</div>
    <div class="settings-grid" style="margin-bottom:16px">
      <div class="field">
        <div class="field-label">Input Device</div>
        <select id="micDeviceSelect" onchange="saveMicDevice()">
          <option value="">System Default</option>
        </select>
      </div>
    </div>
    <div class="mic-section">
      <div class="mic-header">
        <span class="field-label" style="margin:0">Input Level</span>
        <button class="mic-btn" id="micBtn" onclick="toggleMicTest()">Start Test</button>
      </div>
      <div class="meter-track"><div class="meter-fill" id="meterFill"></div></div>
      <div class="meter-hint" id="meterHint">Click "Start Test" and speak to check your mic level</div>
    </div>

    <button class="save-btn" id="generalSaveBtn" onclick="saveAndConfirm()">Save Settings</button>
      <div style="display:flex;gap:8px;margin-top:8px">
        <a href="/api/settings/export" download="dictate_settings.json" class="capture-btn" style="text-decoration:none">Export Settings</a>
        <label class="capture-btn" style="cursor:pointer">Import Settings<input type="file" accept=".json" style="display:none" onchange="importSettings(this)"></label>
      </div>
    <!-- Error log -->
    <div class="field" style="margin-top:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div class="field-label" style="margin-bottom:0">Error Log</div>
        <button class="capture-btn" onclick="clearErrors()" style="font-size:10px">Clear</button>
      </div>
      <pre id="errorLog" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px;font-size:10px;color:var(--dim);max-height:120px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;font-family:'JetBrains Mono',monospace;margin:0">No errors logged.</pre>
    </div>
  </div>

  <!-- Language tab -->
  <div class="tab-panel" id="tab-language">
    <div class="app-hint" style="font-size:11px;color:var(--dim);margin-bottom:16px;line-height:1.6">
      Whisper automatically detects what language you're speaking. Set a paste language to translate your speech before pasting.
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Transcription Language</div>
        <select id="transcribe_language" onchange="autoSave()">
          <option value="">Auto-detect (recommended)</option>
        </select>
      </div>
      <div class="field">
        <div class="field-label">Paste Language</div>
        <select id="paste_language" onchange="autoSave()">
        </select>
      </div>
    </div>
    <div class="field" style="margin-bottom:12px">
      <div class="field-label">Translation Example</div>
      <div style="font-size:12px;color:var(--dim);line-height:1.8">
        Speak in <strong style="color:var(--amber)" id="exSrc">any language</strong> →
        paste in <strong style="color:var(--green)" id="exDst">English</strong>
      </div>
    </div>
    <button class="save-btn" id="langSaveBtn" onclick="saveLangAndConfirm()">Save Language Settings</button>
  </div>

  <!-- Overlay tab -->
  <div class="tab-panel" id="tab-overlay">
    <div class="app-hint" style="font-size:11px;color:var(--dim);margin-bottom:16px;line-height:1.6">
      A floating glassmorphism bubble shows your transcription in real-time while recording. Drag it anywhere on screen.
    </div>
    <div class="field" style="margin-bottom:12px">
      <div class="field-label">Live Preview</div>
      <div class="overlay-preview" id="overlayPreview">Start recording to see text here…</div>
    </div>
    <div class="field" style="margin-bottom:12px">
      <div class="field-label">Real-time Overlay</div>
      <div class="toggle-row">
        <span class="toggle-label" id="overlayLabel">On</span>
        <label class="toggle-switch"><input type="checkbox" id="overlayToggle" onchange="toggleOverlay()"><span class="toggle-slider"></span></label>
      </div>
    </div>
    <button class="save-btn" id="overlaySaveBtn" onclick="saveOverlayAndConfirm()">Save Overlay Settings</button>
  </div>

  <!-- Vocabulary tab -->
  <div class="tab-panel" id="tab-vocab">
    <div class="app-hint">Words Whisper consistently mishears — corrected before AI cleanup.</div>
    <div class="vocab-list" id="vocabList"></div>
    <button class="add-btn" onclick="addVocabRow()">+ Add Entry</button>
    <button class="save-btn" id="vocabSaveBtn" onclick="saveVocab()">Save Vocabulary</button>
  </div>

  <!-- App Tones tab -->
  <div class="tab-panel" id="tab-apptones">
    <div class="app-hint">Different cleanup tone per app. App name must match exactly (e.g. "Slack", "Mail", "Notes", "Arc").</div>
    <div class="app-tone-list" id="appToneList"></div>
    <button class="add-btn" onclick="addAppToneRow()">+ Add App</button>
    <button class="save-btn" id="appToneSaveBtn" onclick="saveAppTones()">Save App Tones</button>
  </div>

  <div class="tab-panel" id="tab-history">
  <div class="history-header">
    <div class="section-label" style="margin:0">Recent Transcriptions</div>
    <button class="clear-btn" onclick="clearHistory()">Clear</button>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
    <input class="history-search" id="historySearch" placeholder="Search transcriptions…" oninput="filterHistory()" style="flex:1;margin-bottom:0" />
    <a href="/api/history/export?fmt=txt" download="dictate_history.txt" class="capture-btn" style="text-decoration:none;white-space:nowrap">Export TXT</a>
    <a href="/api/history/export?fmt=csv" download="dictate_history.csv" class="capture-btn" style="text-decoration:none;white-space:nowrap">Export CSV</a>
  </div>
  <div class="history-list" id="historyList">
    <div class="empty-state">No transcriptions yet</div>
  </div>
  </div><!-- end tab-history -->
  </main><!-- end content -->
  </div><!-- end app-body -->
<div class="version-footer">
    <div style="font-size:11px;color:var(--dim)">dict<span style="color:var(--amber)">.</span>ate &nbsp;·&nbsp; <span id="versionFooter">loading...</span></div>
    <button onclick="checkForUpdates()" id="updateBtn" style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;">Check for Updates</button>
  </div>
  <div id="updateBanner" class="update-banner">
    <span id="updateMsg" style="font-size:12px;"></span>
    <a id="updateLink" href="https://github.com/mcolfax/dictate/releases" target="_blank" style="color:var(--amber);text-decoration:none;font-size:10px;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;">View Release →</a>
  </div>
</div>

<script>
// ── Theme ──────────────────────────────────────────────────────────────────
let currentTheme = localStorage.getItem('theme') || 'system';

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
    document.getElementById('themeBtn').textContent = dark ? '🌙' : '☀️';
  } else {
    root.setAttribute('data-theme', theme);
    document.getElementById('themeBtn').textContent = theme === 'dark' ? '🌙' : '☀️';
  }
}

function cycleTheme() {
  const themes = ['system', 'dark', 'light'];
  const idx = themes.indexOf(currentTheme);
  currentTheme = themes[(idx + 1) % themes.length];
  localStorage.setItem('theme', currentTheme);
  applyTheme(currentTheme);
  autoSave();
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (currentTheme === 'system') applyTheme('system');
});

// ── Onboarding ─────────────────────────────────────────────────────────────
let onboardStep = 0;
let onboardMicTesting = false;

function showOnboarding() {
  document.getElementById('onboardingModal').style.display = 'flex';
}

function nextStep() {
  if (onboardStep < 2) {
    document.getElementById('step' + onboardStep).classList.remove('active');
    document.getElementById('dot' + onboardStep).classList.remove('active');
    document.getElementById('dot' + onboardStep).classList.add('done');
    onboardStep++;
    document.getElementById('step' + onboardStep).classList.add('active');
    document.getElementById('dot' + onboardStep).classList.add('active');
  }
}

async function saveOnboardCombo() {
  const key = document.getElementById('onboardComboSelect').value;
  const opt = Array.from(document.getElementById('onboardComboSelect').options).find(o => o.value === key);
  if (!opt) return;
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({hotkey: key, hotkey_label: opt.textContent.trim(), hotkey_type: 'combo'})});
  fetchStatus();
}

async function toggleOnboardMic() {
  onboardMicTesting = !onboardMicTesting;
  await fetch(onboardMicTesting ? '/api/mic/start' : '/api/mic/stop', {method:'POST'});
}

async function finishOnboarding() {
  if (onboardMicTesting) {
    await fetch('/api/mic/stop', {method:'POST'});
  }
  await fetch('/api/onboarding/complete', {method:'POST'});
  document.getElementById('onboardingModal').style.display = 'none';
}

async function skipOnboarding() {
  await fetch('/api/onboarding/complete', {method:'POST'});
  document.getElementById('onboardingModal').style.display = 'none';
}

// ── State ──────────────────────────────────────────────────────────────────
let currentConfig  = {};
let currentTone    = 'neutral';
let cleanupEnabled = true;
let clipboardOnly  = false;
let soundEnabled   = true;
let pauseEnabled   = true;
let overlayEnabled = true;
let isCapturingUi  = false;
let isCapturingKb  = false;
let isCapturingMs  = false;
let currentHotkeyType = 'combo';
let hotkeyTabManual = false;  // true when user has manually picked a tab; prevents poll-driven reset
let isMicTesting   = false;
let lastHistoryKey = '';
let languages      = {};

// Load languages
fetch('/api/languages').then(r=>r.json()).then(data => {
  languages = data;
  const srcSel = document.getElementById('transcribe_language');
  const dstSel = document.getElementById('paste_language');
  Object.entries(data).forEach(([name, code]) => {
    if (code !== null) {
      const o1 = document.createElement('option');
      o1.value = code; o1.textContent = name;
      srcSel.appendChild(o1);
    }
    const o2 = document.createElement('option');
    o2.value = code || 'auto'; o2.textContent = name;
    dstSel.appendChild(o2);
  });
});

async function fetchStatus() {
  try {
    const data = await (await fetch('/api/status')).json();
    applyStatus(data);
  } catch(e) {}
}

function applyStatus(data) {
  const { enabled, recording, transcribing, capturing_ui,
          capturing_type, kb_preview, capture_warning, capture_error,
          mic_testing, mic_level, overlay_text, config, history, stats,
          permissions } = data;

  // Permissions banner
  if (permissions) {
    const missing = [];
    if (!permissions.accessibility) missing.push({
      label: 'Accessibility',
      desc: 'Required for hotkey detection and text injection.',
      url: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
    });
    if (!permissions.mic) missing.push({
      label: 'Microphone',
      desc: 'Required for voice recording.',
      url: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone'
    });
    const banner = document.getElementById('permBanner');
    const items  = document.getElementById('permItems');
    if (missing.length > 0) {
      items.innerHTML = missing.map(p =>
        `<div style="display:flex;align-items:center;gap:8px">` +
        `<span style="color:var(--text);font-weight:500">${p.label}</span>` +
        `<span style="color:var(--dim);flex:1">${p.desc}</span>` +
        `<button onclick="open('${p.url}')" style="font-size:11px;padding:3px 8px;` +
        `border-radius:5px;border:1px solid var(--amber);background:transparent;` +
        `color:var(--amber);cursor:pointer">Open Settings</button></div>`
      ).join('');
      banner.style.display = '';
    } else {
      banner.style.display = 'none';
    }
  }

  // Power
  document.getElementById('powerBtn').className    = 'power-btn' + (enabled ? ' on' : '');
  document.getElementById('powerStatus').className = 'power-status ' + (enabled ? 'on' : 'off');
  document.getElementById('powerStatus').textContent = enabled ? 'Enabled' : 'Disabled';
  document.getElementById('powerHint').textContent   = enabled
    ? `Listening — ${config.hotkey_label} to ${config.mode === 'toggle' ? 'start/stop' : 'hold and record'}`
    : 'Click to enable dictation';

  // Header waveform — visible from every tab while recording
  const hw = document.getElementById('headerWave');
  if (hw) hw.className = 'header-wave' + (recording ? ' active' : '');

  // Indicator
  const ind = document.getElementById('indicator');
  ind.className = 'indicator' + (recording ? ' recording' : transcribing ? ' transcribing' : '');
  document.getElementById('indicatorText').textContent = recording ? 'Recording' : transcribing ? 'Processing' : 'Idle';

  // Stats
  if (stats) {
    document.getElementById('statWordsToday').textContent    = stats.words_today    || 0;
    document.getElementById('statSessionsToday').textContent = stats.sessions_today || 0;
    document.getElementById('statWordsTotal').textContent    = stats.words_total    || 0;
    document.getElementById('statSessionsTotal').textContent = stats.sessions_total || 0;
  }

  // Sync hotkey type tabs and panels
  const htype = config.hotkey_type || 'combo';
  if (!hotkeyTabManual) {
    // Initial load or config changed from a save — sync tabs to match saved config
    if (htype !== currentHotkeyType) switchHotkeyType(htype, true);
  } else if (htype === currentHotkeyType) {
    // Config was successfully saved with the type the user selected — unlock auto-sync
    hotkeyTabManual = false;
  }

  // Combo panel
  const cs = document.getElementById('comboSelect');
  if (cs && cs.options.length > 0 && htype === 'combo') {
    cs.value = config.hotkey || 'cmd+shift+alt';
  }
  document.getElementById('hotkeyValue').textContent = config.hotkey_label || '⌘⇧⌥';
  document.getElementById('onboardHotkeyValue').textContent = config.hotkey_label || '⌘⇧⌥';
  const ocs = document.getElementById('onboardComboSelect');
  if (ocs && ocs.options.length > 0) ocs.value = config.hotkey || 'cmd+shift+alt';

  // Keyboard panel
  isCapturingKb = data.capturing && capturing_type === 'keyboard';
  const kbField  = document.getElementById('htPanel_keyboard');
  const kbVal    = document.getElementById('hotkeyKbValue');
  const kbBtn    = document.getElementById('captureKbBtn');
  const kbWarn   = document.getElementById('hotkeyKbWarning');
  const kbErr    = document.getElementById('hotkeyKbError');
  if (isCapturingKb) {
    document.getElementById('hotkeyField').classList.add('capturing');
    kbBtn.textContent = 'Cancel'; kbBtn.classList.add('active');
    kbVal.classList.add('capturing');
    kbVal.textContent = kb_preview || '…';
  } else {
    document.getElementById('hotkeyField').classList.remove('capturing');
    kbBtn.textContent = 'Assign'; kbBtn.classList.remove('active');
    kbVal.classList.remove('capturing');
    kbVal.textContent = (htype === 'keyboard' && config.hotkey_label) ? config.hotkey_label : 'Not set';
  }
  if (capture_warning) {
    kbWarn.textContent = '⚠ ' + capture_warning; kbWarn.style.display = 'block';
  } else {
    kbWarn.style.display = 'none';
  }
  if (capture_error) {
    kbErr.textContent = '✕ ' + capture_error; kbErr.style.display = 'block';
  } else {
    kbErr.style.display = 'none';
  }

  // Mouse panel
  isCapturingMs = data.capturing && capturing_type === 'mouse';
  const msVal = document.getElementById('hotkeyMouseValue');
  const msBtn = document.getElementById('captureMsBtn');
  if (msVal) {
    if (isCapturingMs) {
      msBtn.textContent = 'Cancel'; msBtn.classList.add('active');
      msVal.classList.add('capturing'); msVal.textContent = 'Click a mouse button…';
    } else {
      msBtn.textContent = 'Assign'; msBtn.classList.remove('active');
      msVal.classList.remove('capturing');
      msVal.textContent = (htype === 'mouse' && config.hotkey_label) ? config.hotkey_label : 'Not set';
    }
  }

  // UI shortcut capture
  isCapturingUi = capturing_ui;
  const uiField = document.getElementById('uiShortcutField');
  const uiVal   = document.getElementById('uiShortcutValue');
  const uiCbtn  = document.getElementById('captureUiBtn');
  if (capturing_ui) {
    uiField.classList.add('capturing'); uiVal.classList.add('capturing');
    uiVal.textContent = 'Press a symbol or F-key…';
    uiCbtn.textContent = 'Cancel'; uiCbtn.classList.add('active');
  } else {
    uiField.classList.remove('capturing'); uiVal.classList.remove('capturing');
    uiVal.textContent = config.ui_shortcut_label || 'Not set';
    uiCbtn.textContent = 'Assign'; uiCbtn.classList.remove('active');
  }
  // UI shortcut error feedback
  const uiErr = document.getElementById('uiShortcutError');
  if (data.capturing_ui_error) {
    uiErr.textContent = data.capturing_ui_error; uiErr.style.display = 'block';
  } else if (!capturing_ui) {
    uiErr.style.display = 'none';
  }

  // Mic meter
  isMicTesting = mic_testing;
  const micBtn = document.getElementById('micBtn');
  const meter  = document.getElementById('meterFill');
  const hint   = document.getElementById('meterHint');
  if (mic_testing) {
    micBtn.textContent = 'Stop Test'; micBtn.classList.add('testing');
    meter.style.width = mic_level + '%';
    hint.textContent  = mic_level < 5 ? 'No signal' : mic_level < 20 ? 'Low — speak louder' : mic_level < 70 ? '✓ Good level' : 'Too loud';
    // Onboarding mic
    document.getElementById('onboardMicBtn').textContent = 'Stop Test';
    document.getElementById('onboardMeterFill').style.width = mic_level + '%';
    document.getElementById('onboardMeterHint').textContent = hint.textContent;
  } else {
    micBtn.textContent = 'Start Test'; micBtn.classList.remove('testing');
    meter.style.width = '0%';
    hint.textContent  = 'Click "Start Test" and speak to check your mic level';
    document.getElementById('onboardMicBtn').textContent = 'Test Mic';
    document.getElementById('onboardMeterFill').style.width = '0%';
  }

  // Overlay preview
  const preview = document.getElementById('overlayPreview');
  if (overlay_text) {
    preview.textContent = overlay_text;
    preview.classList.add('recording');
  } else if (recording) {
    preview.textContent = 'Listening…';
    preview.classList.add('recording');
  } else {
    preview.textContent = 'Start recording to see text here…';
    preview.classList.remove('recording');
  }

  // Config — only on first load
  if (Object.keys(currentConfig).length === 0 && config) {
    currentConfig = config;
    document.getElementById('mode').value          = config.mode          || 'toggle';
    document.getElementById('whisper_model').value  = config.whisper_model || 'mlx-community/whisper-small-mlx';
    document.getElementById('ollama_model').value   = config.ollama_model  || 'llama3.2';
    if (config.transcribe_language !== undefined)
      document.getElementById('transcribe_language').value = config.transcribe_language || '';
    if (config.paste_language)
      document.getElementById('paste_language').value = config.paste_language;
    cleanupEnabled = config.cleanup !== false;
    clipboardOnly  = config.clipboard_only === true;
    soundEnabled   = config.sound_feedback !== false;
    pauseEnabled   = config.pause_detection !== false;
    overlayEnabled = config.overlay_enabled !== false;
    const ps = config.pause_seconds || 2.0;
    document.getElementById('cleanupToggle').checked   = cleanupEnabled;
    document.getElementById('clipboardToggle').checked = clipboardOnly;
    document.getElementById('soundToggle').checked     = soundEnabled;
    document.getElementById('pauseToggle').checked     = pauseEnabled;
    document.getElementById('overlayToggle').checked   = overlayEnabled;
    document.getElementById('pauseSeconds').value      = ps;
    document.getElementById('pauseSecondsVal').textContent = ps + 's';
    updateCleanupUI(); updateClipboardUI(); updateSoundUI(); updatePauseUI(); updateOverlayUI();
    setTone(config.tone || 'neutral');
    updateLangExample();

    // Launch at login
    fetch('/api/launch_at_login').then(r=>r.json()).then(d => {
      document.getElementById('launchAtLoginToggle').checked = d.enabled;
      document.getElementById('launchAtLoginLabel').textContent = d.enabled ? 'On' : 'Off';
    }).catch(()=>{});

    // Filler removal
    const fillersOn = config.remove_fillers === true;
    document.getElementById('fillersToggle').checked = fillersOn;
    document.getElementById('fillersLabel').textContent = fillersOn ? 'On — stripping um/uh/er…' : 'Off';
    document.getElementById('fillersLabel').className = 'toggle-label' + (fillersOn ? '' : ' off');

    // Theme
    if (config.theme) { currentTheme = config.theme; applyTheme(currentTheme); }

    // Show onboarding if not done
    if (!config.onboarding_done) {
      setTimeout(() => showOnboarding(), 500);
    }

    // Load mic devices now that config is available
    loadMicDevices();
    loadSoundOptions(config);
    loadWeeklyChart();
  }

  // History
  const newKey = history ? history.map(h => h.ts + h.cleaned).join('|') : '';
  if (newKey !== lastHistoryKey) {
    lastHistoryKey = newKey;
    const list = document.getElementById('historyList');
    if (!history || history.length === 0) {
      list.innerHTML = '<div class="empty-state">No transcriptions yet</div>';
    } else {
      _historyData = history;
      list.innerHTML = history.map((h,i) => `
        <div class="history-item">
          <div class="history-meta">
            <span>${h.ts}</span>
            ${h.app ? `<span class="badge app-badge">${escHtml(h.app)}</span>` : ''}
            ${h.lang ? `<span class="badge lang-badge">${h.lang}</span>` : ''}
            <span class="badge ${h.cleanup_used ? 'clean-badge' : 'raw-badge'}">${h.cleanup_used ? 'AI cleaned' : 'raw'}</span>
            <button class="history-copy-btn" id="hcopy${i}" onclick="copyHistory(${i},this)">Copy</button>
            <button class="history-copy-btn" onclick="repaste(${i})" title="Re-inject into frontmost app">↩ Paste</button>
          </div>
          <div class="history-text">${escHtml(h.cleaned)}</div>
          ${h.cleanup_used && h.raw !== h.cleaned ? `<div class="history-raw-text">raw: ${escHtml(h.raw)}</div>` : ''}
        </div>`).join('');
    }
  }
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

let _historyData = [];
function copyHistory(idx, btn) {
  const text = _historyData[idx]?.cleaned;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1500);
  }).catch(() => {});
}
async function repaste(i) {
  await fetch(`/api/history/repaste/${i}`, {method:'POST'});
}
function filterHistory() {
  const q = document.getElementById('historySearch').value.trim();
  const ql = q.toLowerCase();
  document.querySelectorAll('.history-item').forEach((el, i) => {
    const entry = _historyData[i];
    if (!entry) return;
    const text = entry.cleaned || '';
    if (!q) {
      el.style.display = '';
      // Restore un-highlighted text
      const td = el.querySelector('.history-text');
      if (td) td.innerHTML = escHtml(text);
      return;
    }
    if (!text.toLowerCase().includes(ql)) {
      el.style.display = 'none';
      return;
    }
    el.style.display = '';
    // Highlight matches
    const td = el.querySelector('.history-text');
    if (td) {
      const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      td.innerHTML = escHtml(text).replace(re, m => `<mark style="background:rgba(245,158,11,.35);border-radius:2px;padding:0 1px">${m}</mark>`);
    }
  });
}

function showTab(name) {
  document.querySelectorAll('.nav-item').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.id === `tab-${name}`);
  });
  if (name === 'vocab')    loadVocab();
  if (name === 'apptones') loadAppTones();
  if (name === 'general')  loadErrors();
}

async function togglePower() { await fetch('/api/toggle', {method:'POST'}); fetchStatus(); }

async function switchHotkeyType(type, silent) {
  currentHotkeyType = type;
  ['combo','keyboard','mouse'].forEach(t => {
    const panel = document.getElementById('htPanel_' + t);
    const tab   = document.getElementById('htab_' + t);
    if (panel) panel.style.display = (t === type) ? '' : 'none';
    if (tab)   tab.classList.toggle('active', t === type);
  });
  if (!silent) {
    hotkeyTabManual = true;  // Lock tab — don't let poll reset it until a hotkey is saved
    // Cancel any active capture so keypresses aren't silently swallowed
    if (isCapturingKb || isCapturingMs) {
      await fetch('/api/capture/cancel', {method:'POST'});
      isCapturingKb = false; isCapturingMs = false;
    }
  }
}

async function toggleCaptureKb() {
  if (isCapturingKb) {
    await fetch('/api/capture/cancel', {method:'POST'});
  } else {
    await fetch('/api/capture/start', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({type: 'keyboard'})});
  }
  fetchStatus();
}

async function toggleCaptureMs() {
  if (isCapturingMs) {
    await fetch('/api/capture/cancel', {method:'POST'});
  } else {
    await fetch('/api/capture/start', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({type: 'mouse'})});
  }
  fetchStatus();
}

async function loadComboOptions() {
  try {
    const opts = await (await fetch('/api/combo_options')).json();
    [document.getElementById('comboSelect'), document.getElementById('onboardComboSelect')]
      .forEach(sel => {
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = opts.map(o => `<option value="${o.key}">${o.label}</option>`).join('');
        if (cur) sel.value = cur;
      });
  } catch(e) {}
}

async function saveCombo() {
  const key = document.getElementById('comboSelect').value;
  const opt = Array.from(document.getElementById('comboSelect').options).find(o => o.value === key);
  if (!opt) return;
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({hotkey: key, hotkey_label: opt.textContent.trim(), hotkey_type: 'combo'})});
  fetchStatus();
}

async function toggleCaptureUi() {
  await fetch(isCapturingUi ? '/api/capture_ui/cancel' : '/api/capture_ui/start', {method:'POST'});
  fetchStatus();
}
async function toggleMicTest() {
  await fetch(isMicTesting ? '/api/mic/stop' : '/api/mic/start', {method:'POST'});
  fetchStatus();
}

async function loadMicDevices() {
  const sel = document.getElementById('micDeviceSelect');
  try {
    const resp = await fetch('/api/mic/devices');
    const devices = await resp.json();
    if (!Array.isArray(devices) || devices.length === 0) return;
    const current = currentConfig.mic_device || '';
    sel.innerHTML = '<option value="">System Default</option>' +
      devices.map(d =>
        `<option value="${escHtml(d.name)}" ${d.name === current ? 'selected' : ''}>` +
        `${escHtml(d.name)}${d.default ? ' ✓' : ''}</option>`
      ).join('');
  } catch(e) { console.error('Could not load mic devices', e); }
}

async function loadSoundOptions(cfg) {
  try {
    const sounds = await (await fetch('/api/sounds')).json();
    const fields = [
      ['soundStart', cfg.sound_start || 'Tink'],
      ['soundStop',  cfg.sound_stop  || 'Pop'],
      ['soundDone',  cfg.sound_done  || 'Glass'],
    ];
    fields.forEach(([id, cur]) => {
      const sel = document.getElementById(id);
      sel.innerHTML = sounds.map(s =>
        `<option value="${s}" ${s === cur ? 'selected' : ''}>${s === 'None' ? '— None —' : s}</option>`
      ).join('');
    });
  } catch(e) {}
}

async function loadWeeklyChart() {
  try {
    const data = await (await fetch('/api/stats/weekly')).json();
    const max = Math.max(...data.map(d => d.words), 1);
    const today = new Date().toISOString().slice(0, 10);
    const chart = document.getElementById('weeklyChart');
    const labels = document.getElementById('weeklyLabels');
    const days = ['Su','Mo','Tu','We','Th','Fr','Sa'];
    chart.innerHTML = data.map(d => {
      const h = Math.max(2, Math.round((d.words / max) * 72));
      const day = days[new Date(d.date + 'T12:00:00').getDay()];
      const isToday = d.date === today;
      return `<div class="chart-bar${isToday?' today':''}" style="height:${h}px" data-tip="${d.words} words"></div>`;
    }).join('');
    labels.innerHTML = data.map(d => {
      const day = days[new Date(d.date + 'T12:00:00').getDay()];
      return `<span class="chart-label">${day}</span>`;
    }).join('');
    const peak = Math.max(...data.map(d => d.words));
    document.getElementById('chartPeak').textContent = peak > 0 ? `peak ${peak.toLocaleString()} words` : '';
  } catch(e) {}
}

async function saveMicDevice() {
  const val = document.getElementById('micDeviceSelect').value;
  config.mic_device = val || null;
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mic_device: config.mic_device})});
  // Reset stream so next recording uses new device
  await fetch('/api/mic/reset', {method:'POST'}).catch(() => {});
}

async function toggleLaunchAtLogin() {
  const enabled = document.getElementById('launchAtLoginToggle').checked;
  document.getElementById('launchAtLoginLabel').textContent = enabled ? 'On' : 'Off';
  await fetch('/api/launch_at_login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled})});
}
async function toggleFillers() {
  const enabled = document.getElementById('fillersToggle').checked;
  document.getElementById('fillersLabel').textContent = enabled ? 'On — stripping um/uh/er…' : 'Off';
  document.getElementById('fillersLabel').className = 'toggle-label' + (enabled ? '' : ' off');
  config.remove_fillers = enabled;
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({remove_fillers: enabled})});
}
function toggleCleanup() { cleanupEnabled = document.getElementById('cleanupToggle').checked; updateCleanupUI(); autoSave(); }
function toggleClipboard() { clipboardOnly = document.getElementById('clipboardToggle').checked; updateClipboardUI(); autoSave(); }
function toggleSound() { soundEnabled = document.getElementById('soundToggle').checked; updateSoundUI(); autoSave(); }
function togglePause() { pauseEnabled = document.getElementById('pauseToggle').checked; updatePauseUI(); autoSave(); }
function toggleOverlay() { overlayEnabled = document.getElementById('overlayToggle').checked; updateOverlayUI(); autoSave(); }

function updatePauseLabel() { const v = document.getElementById('pauseSeconds').value; document.getElementById('pauseSecondsVal').textContent = v + 's'; }
function updateCleanupUI() { const l = document.getElementById('cleanupLabel'); l.textContent = cleanupEnabled ? 'On' : 'Off — raw only'; l.className = 'toggle-label' + (cleanupEnabled ? '' : ' off'); document.querySelectorAll('.tone-btn').forEach(b => b.classList.toggle('disabled', !cleanupEnabled)); }
function updateClipboardUI() { const l = document.getElementById('clipboardLabel'); l.textContent = clipboardOnly ? 'Clipboard only' : 'Auto-paste'; l.className = 'toggle-label' + (clipboardOnly ? ' off' : ''); }
function updateSoundUI() {
  const l = document.getElementById('soundLabel');
  l.textContent = soundEnabled ? 'On' : 'Off';
  l.className = 'toggle-label' + (soundEnabled ? '' : ' off');
  document.getElementById('soundPickerGrid').style.opacity = soundEnabled ? '1' : '0.4';
  document.getElementById('soundPickerGrid').style.pointerEvents = soundEnabled ? '' : 'none';
}
function updatePauseUI() { const v = document.getElementById('pauseSeconds').value; const l = document.getElementById('pauseLabel'); l.textContent = pauseEnabled ? `On — ${v}s silence` : 'Off'; l.className = 'toggle-label' + (pauseEnabled ? '' : ' off'); document.getElementById('pauseSecondsField').style.opacity = pauseEnabled ? '1' : '0.4'; }
function updateOverlayUI() { const l = document.getElementById('overlayLabel'); l.textContent = overlayEnabled ? 'On' : 'Off'; l.className = 'toggle-label' + (overlayEnabled ? '' : ' off'); }

function updateLangExample() {
  const src = document.getElementById('transcribe_language').value;
  const dst = document.getElementById('paste_language').value;
  const srcName = src ? Object.entries(languages).find(([n,c]) => c === src)?.[0] || src : 'any language';
  const dstName = Object.entries(languages).find(([n,c]) => c === dst || (c === null && dst === 'auto'))?.[0] || dst;
  document.getElementById('exSrc').textContent = srcName;
  document.getElementById('exDst').textContent = dstName;
}

function setTone(tone) { currentTone = tone; document.querySelectorAll('.tone-btn').forEach(b => b.classList.toggle('active', b.dataset.tone === tone)); autoSave(); }

async function autoSave() {
  const cfg = {
    mode:                document.getElementById('mode').value,
    whisper_model:       document.getElementById('whisper_model').value,
    ollama_model:        document.getElementById('ollama_model').value,
    tone:                currentTone,
    cleanup:             cleanupEnabled,
    clipboard_only:      clipboardOnly,
    sound_feedback:      soundEnabled,
    sound_start:         document.getElementById('soundStart').value,
    sound_stop:          document.getElementById('soundStop').value,
    sound_done:          document.getElementById('soundDone').value,
    pause_detection:     pauseEnabled,
    pause_seconds:       parseFloat(document.getElementById('pauseSeconds').value),
    overlay_enabled:     overlayEnabled,
    transcribe_language: document.getElementById('transcribe_language').value || null,
    paste_language:      document.getElementById('paste_language').value,
    theme:               currentTheme,
  };
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
  Object.assign(currentConfig, cfg);
}

async function saveAndConfirm() { await autoSave(); confirmBtn('generalSaveBtn', 'Save Settings'); }
async function saveLangAndConfirm() { updateLangExample(); await autoSave(); confirmBtn('langSaveBtn', 'Save Language Settings'); }
async function saveOverlayAndConfirm() { await autoSave(); confirmBtn('overlaySaveBtn', 'Save Overlay Settings'); }

async function loadErrors() {
  try {
    const d = await (await fetch('/api/errors')).json();
    const el = document.getElementById('errorLog');
    el.textContent = d.log.trim() || 'No errors logged.';
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}
async function clearErrors() {
  await fetch('/api/errors/clear', {method:'POST'});
  document.getElementById('errorLog').textContent = 'No errors logged.';
}
async function importSettings(input) {
  const file = input.files[0];
  if (!file) return;
  const text = await file.text();
  try {
    const data = JSON.parse(text);
    const r = await fetch('/api/settings/import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    const d = await r.json();
    if (d.ok) { alert('Settings imported. Reloading…'); location.reload(); }
    else alert('Import failed: ' + (d.error || 'unknown error'));
  } catch(e) { alert('Invalid JSON file.'); }
}
function confirmBtn(id, defaultText) {
  const btn = document.getElementById(id);
  btn.textContent = 'Saved ✓'; btn.classList.add('saved');
  setTimeout(() => { btn.textContent = defaultText; btn.classList.remove('saved'); }, 2000);
}

async function clearHistory() {
  await fetch('/api/history/clear', {method:'POST'});
  lastHistoryKey = '';
  document.getElementById('historyList').innerHTML = '<div class="empty-state">No transcriptions yet</div>';
  fetchStatus();
}

// ── Version ────────────────────────────────────────────────────────────────
async function checkForUpdates() {
  const btn = document.getElementById('updateBtn');
  btn.textContent = 'Checking…'; btn.style.color = 'var(--amber)';
  try {
    const data = await (await fetch('/api/version')).json();
    document.getElementById('versionBadge').textContent = 'v' + data.current;
    document.getElementById('versionFooter').textContent = 'v' + data.current;
    const banner = document.getElementById('updateBanner');
    const msg    = document.getElementById('updateMsg');
    if (data.update_available) {
      msg.textContent = `✨ v${data.latest} available (you have v${data.current})`;
      banner.style.display = 'flex'; btn.textContent = '⬆️ Update Available'; btn.style.color = 'var(--amber)';
    } else if (data.latest) {
      msg.textContent = `✅ You're on the latest version (v${data.current})`;
      document.getElementById('updateLink').style.display = 'none';
      banner.style.display = 'flex'; btn.textContent = 'Up to Date ✓'; btn.style.color = 'var(--green)';
      setTimeout(() => { banner.style.display = 'none'; document.getElementById('updateLink').style.display = ''; btn.textContent = 'Check for Updates'; btn.style.color = 'var(--dim)'; }, 4000);
    } else {
      btn.textContent = 'No Internet'; setTimeout(() => { btn.textContent = 'Check for Updates'; btn.style.color = 'var(--dim)'; }, 3000);
    }
  } catch(e) { btn.textContent = 'Error'; setTimeout(() => { btn.textContent = 'Check for Updates'; btn.style.color = 'var(--dim)'; }, 3000); }
}

// ── Vocab ──────────────────────────────────────────────────────────────────
let vocabRows = [];
async function loadVocab() { const d = await (await fetch('/api/vocab')).json(); vocabRows = d.map(e => ({from:e.from,to:e.to})); renderVocab(); }
function renderVocab() { const l = document.getElementById('vocabList'); if (!vocabRows.length) { l.innerHTML = '<div class="empty-state" style="padding:20px 0">No entries yet</div>'; return; } l.innerHTML = vocabRows.map((r,i) => `<div class="vocab-row"><input class="vocab-input" placeholder="mishear" value="${escHtml(r.from)}" oninput="vocabRows[${i}].from=this.value"><span class="vocab-arrow">→</span><input class="vocab-input" placeholder="correction" value="${escHtml(r.to)}" oninput="vocabRows[${i}].to=this.value"><button class="vocab-del" onclick="removeVocabRow(${i})">×</button></div>`).join(''); }
function addVocabRow() { vocabRows.push({from:'',to:''}); renderVocab(); }
function removeVocabRow(i) { vocabRows.splice(i,1); renderVocab(); }
async function saveVocab() { await fetch('/api/vocab', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(vocabRows.filter(r=>r.from.trim()))}); confirmBtn('vocabSaveBtn','Save Vocabulary'); }

// ── App Tones ──────────────────────────────────────────────────────────────
let appToneRows = [];
async function loadAppTones() { const d = await (await fetch('/api/app_tones')).json(); appToneRows = Object.entries(d).map(([app,tone]) => ({app,tone})); renderAppTones(); }
function renderAppTones() { const l = document.getElementById('appToneList'); if (!appToneRows.length) { l.innerHTML = '<div class="empty-state" style="padding:20px 0">No app profiles yet</div>'; return; } const tones=['neutral','professional','casual','concise']; l.innerHTML = appToneRows.map((r,i) => `<div class="app-tone-row"><input class="app-tone-input" placeholder="App name" value="${escHtml(r.app)}" oninput="appToneRows[${i}].app=this.value"><span class="vocab-arrow">→</span><select class="app-tone-select" onchange="appToneRows[${i}].tone=this.value">${tones.map(t=>`<option value="${t}" ${r.tone===t?'selected':''}>${t}</option>`).join('')}</select><button class="vocab-del" onclick="removeAppToneRow(${i})">×</button></div>`).join(''); }
function addAppToneRow() { appToneRows.push({app:'',tone:'neutral'}); renderAppTones(); }
function removeAppToneRow(i) { appToneRows.splice(i,1); renderAppTones(); }
async function saveAppTones() { const obj={}; appToneRows.filter(r=>r.app.trim()).forEach(r=>obj[r.app.trim()]=r.tone); await fetch('/api/app_tones',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)}); confirmBtn('appToneSaveBtn','Save App Tones'); }

// ── Init ───────────────────────────────────────────────────────────────────
applyTheme(currentTheme);
fetch('/api/version').then(r=>r.json()).then(d => {
  document.getElementById('versionBadge').textContent = 'v' + d.current;
  document.getElementById('versionFooter').textContent = 'v' + d.current;
});
loadComboOptions();
fetchStatus();
setInterval(fetchStatus, 1000);
setInterval(() => { if (isCapturingKb) fetchStatus(); }, 200);
setInterval(async () => {
  try {
    const d = await (await fetch('/api/combo/status')).json();
    const el = document.getElementById('comboTester');
    if (!el) return;
    if (d.active) {
      el.textContent = '✓ Combo active!';
      el.style.color = 'var(--amber)';
      el.style.borderColor = 'var(--amber)';
    } else if (d.held.length) {
      el.textContent = d.held.map(k => ({'cmd':'⌘','shift':'⇧','alt':'⌥','ctrl':'⌃'}[k]||k)).join('') + '…';
      el.style.color = 'var(--dim)';
      el.style.borderColor = 'var(--border)';
    } else {
      el.textContent = '—';
      el.style.color = 'var(--dim)';
      el.style.borderColor = 'var(--border)';
    }
  } catch(e) {}
}, 150);
</script>
</body>
</html>"""

if __name__ == "__main__":
    import atexit

    @atexit.register
    def _cleanup_overlay():
        global _overlay_proc
        if _overlay_proc is not None and _overlay_proc.poll() is None:
            _overlay_proc.terminate()

    show_overlay()  # Pre-launch so it's ready when first recording starts
    start_listener()
    print("━" * 50)
    print("🎤  Dictation server ready")
    print("    Open http://localhost:5001 in Arc")
    print("    Press Ctrl+C to quit")
    print("━" * 50)
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
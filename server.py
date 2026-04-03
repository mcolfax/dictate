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

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
STATS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stats.json')
_DATA_DIR   = os.environ.get("APP_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_DATA_DIR, 'config.json')
STATS_FILE  = os.path.join(_DATA_DIR, 'stats.json')
SAMPLE_RATE     = 16000
OLLAMA_URL      = "http://localhost:11434/api/generate"
APP_VERSION     = "1.4.9"
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
    "hotkey":           "alt_r",
    "hotkey_label":     "Right Option (⌥)",
    "hotkey_type":      "keyboard",
    "ui_shortcut":      None,
    "ui_shortcut_label": None,
    "whisper_model":    "mlx-community/whisper-small-mlx",
    "ollama_model":     "llama3.2",
    "tone":             "neutral",
    "cleanup":          True,
    "clipboard_only":   False,
    "sound_feedback":   True,
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
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        return {**DEFAULT_CONFIG, **json.load(open(CONFIG_FILE))}
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

config = load_config()

# ── STATS ─────────────────────────────────────────────────────────────────────

def load_stats():
    today = str(date.today())
    if os.path.exists(STATS_FILE):
        s = json.load(open(STATS_FILE))
        if s.get("date") != today:
            s = {"date": today, "words_today": 0, "sessions_today": 0,
                 "words_total": s.get("words_total", 0), "sessions_total": s.get("sessions_total", 0)}
    else:
        s = {"date": today, "words_today": 0, "sessions_today": 0, "words_total": 0, "sessions_total": 0}
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

# ── STATE ─────────────────────────────────────────────────────────────────────

state = {
    "enabled":           False,
    "recording":         False,
    "transcribing":      False,
    "capturing":         False,
    "capturing_ui":      False,   # Capturing UI shortcut
    "mic_testing":       False,
    "mic_level":         0,
    "history":           [],
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

SOUNDS = {
    "start": "/System/Library/Sounds/Tink.aiff",
    "stop":  "/System/Library/Sounds/Pop.aiff",
    "done":  "/System/Library/Sounds/Glass.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
}

def play_sound(name):
    if not config.get("sound_feedback", True): return
    path = SOUNDS.get(name)
    if path and os.path.exists(path):
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

def show_overlay():
    """Start the overlay subprocess via a mini .app bundle (no dock icon)."""
    global _overlay_proc
    if not config.get("overlay_enabled", True):
        return
    if _overlay_proc is not None and _overlay_proc.poll() is None:
        return  # Already running
    try:
        exe = _build_overlay_bundle()
        _overlay_proc = subprocess.Popen(
            [exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # Wait for socket to bind
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

def _ensure_stream():
    global _persistent_stream
    if _persistent_stream is None or not _persistent_stream.active:
        if not _check_mic_permission():
            return
        try:
            _persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                dtype="int16", blocksize=1600
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
                    import sys
                    lang = config.get("transcribe_language") or None
                    lang_arg = f', language="{lang}"' if lang else ''
                    cmd = [sys.executable, "-c", f"""
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

            if config.get("pause_detection", True) and config.get("mode") not in ("toggle", "hold"):
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
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
        state["history"] = state["history"][:30]
        play_sound("done")
    except Exception as e:
        print(f"❌ Error: {e}")
        play_sound("error")
    finally:
        os.unlink(tmp_path)
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

def get_kb_hotkey():
    try:
        return getattr(kb.Key, config.get("hotkey", "alt_r"))
    except AttributeError:
        return kb.Key.alt_r

def get_ui_hotkey():
    ui = config.get("ui_shortcut")
    if not ui: return None
    try:
        return getattr(kb.Key, ui)
    except AttributeError:
        return None

def _key_label(key):
    labels = {
        kb.Key.alt_r: "Right Option (⌥)", kb.Key.alt: "Left Option (⌥)",
        kb.Key.f13: "F13", kb.Key.f14: "F14", kb.Key.f15: "F15",
        kb.Key.caps_lock: "Caps Lock", kb.Key.space: "Space",
    }
    return labels.get(key, key.name.replace("_", " ").title())

def on_kb_press(key):
    # Capture mode for dictation hotkey
    if state["capturing"]:
        if isinstance(key, kb.Key) and key in (
            kb.Key.shift, kb.Key.shift_r, kb.Key.ctrl, kb.Key.ctrl_r,
            kb.Key.alt, kb.Key.cmd, kb.Key.cmd_r): return
        key_name  = key.name if isinstance(key, kb.Key) else (key.char or str(key))
        key_label = _key_label(key) if isinstance(key, kb.Key) else (key.char.upper() if key.char else str(key))
        config["hotkey"] = key_name; config["hotkey_label"] = key_label
        config["hotkey_type"] = "keyboard"
        save_config(config); state["capturing"] = False
        print(f"✅ Hotkey set: {key_label}"); return

    # Capture mode for UI shortcut
    if state["capturing_ui"]:
        if isinstance(key, kb.Key) and key in (
            kb.Key.shift, kb.Key.shift_r, kb.Key.ctrl, kb.Key.ctrl_r,
            kb.Key.alt, kb.Key.cmd, kb.Key.cmd_r): return
        key_name  = key.name if isinstance(key, kb.Key) else (key.char or str(key))
        key_label = _key_label(key) if isinstance(key, kb.Key) else (key.char.upper() if key.char else str(key))
        config["ui_shortcut"] = key_name; config["ui_shortcut_label"] = key_label
        save_config(config); state["capturing_ui"] = False
        print(f"✅ UI shortcut set: {key_label}"); return

    # UI shortcut
    ui_key = get_ui_hotkey()
    if ui_key and key == ui_key:
        handle_ui_shortcut()
        return

    if config.get("hotkey_type") == "keyboard" and key == get_kb_hotkey():
        handle_trigger_press()

def on_kb_release(key):
    if config.get("hotkey_type") == "keyboard" and key == get_kb_hotkey():
        handle_trigger_release()

# ── MOUSE LISTENER ────────────────────────────────────────────────────────────

MOUSE_BUTTON_LABELS = {
    "Button.left": "Left Click", "Button.right": "Right Click",
    "Button.middle": "Middle Click", "Button.x1": "Mouse Button 4", "Button.x2": "Mouse Button 5",
}

def on_ms_click(x, y, button, pressed):
    btn_str = str(button)
    if state["capturing"] and pressed:
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
        "mic_testing": state["mic_testing"], "mic_level": state["mic_level"],
        "overlay_text": state["overlay_text"],
        "config": config, "history": state["history"], "stats": load_stats(),
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

@app.route("/api/capture/start",    methods=["POST"])
def api_capture_start():  state["capturing"] = True;     return jsonify({"capturing": True})

@app.route("/api/capture/cancel",   methods=["POST"])
def api_capture_cancel(): state["capturing"] = False;    return jsonify({"capturing": False})

@app.route("/api/capture_ui/start",  methods=["POST"])
def api_capture_ui_start():  state["capturing_ui"] = True;  return jsonify({"capturing_ui": True})

@app.route("/api/capture_ui/cancel", methods=["POST"])
def api_capture_ui_cancel(): state["capturing_ui"] = False; return jsonify({"capturing_ui": False})

@app.route("/api/mic/start", methods=["POST"])
def api_mic_start():
    threading.Thread(target=start_mic_test, daemon=True).start()
    return jsonify({"mic_testing": True})

@app.route("/api/mic/stop", methods=["POST"])
def api_mic_stop():
    stop_mic_test(); return jsonify({"mic_testing": False})

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
    state["history"] = []; return jsonify({"ok": True})

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

@app.route("/api/onboarding/complete", methods=["POST"])
def api_onboarding_complete():
    config["onboarding_done"] = True; save_config(config)
    return jsonify({"ok": True})

@app.route("/")
def index(): return HTML

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

  html,body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;min-height:100vh;line-height:1.5;transition:background .2s,color .2s}
  .app{max-width:820px;margin:0 auto;padding:48px 24px 80px}

  /* ── Header ── */
  .header{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:48px;padding-bottom:24px;border-bottom:1px solid var(--border)}
  .wordmark{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px}
  .wordmark span{color:var(--amber)}
  .header-right{display:flex;align-items:center;gap:12px}
  .version-badge{font-size:11px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
  .theme-toggle{background:none;border:1px solid var(--border);border-radius:20px;padding:4px 10px;cursor:pointer;font-size:12px;color:var(--dim);font-family:'JetBrains Mono',monospace;transition:all .15s}
  .theme-toggle:hover{border-color:var(--amber);color:var(--amber)}

  /* ── Power ── */
  .power-section{display:flex;align-items:center;gap:32px;margin-bottom:40px;padding:28px 32px;background:var(--surface);border:1px solid var(--border);border-radius:4px}
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
  .stats-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:32px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:12px 14px}
  .stat-value{font-family:'Syne',sans-serif;font-size:22px;font-weight:700;color:var(--amber);margin-bottom:2px}
  .stat-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}

  /* ── Tabs ── */
  .tabs{display:flex;gap:2px;margin-bottom:24px;border-bottom:1px solid var(--border)}
  .tab{padding:10px 18px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);cursor:pointer;border:none;background:none;font-family:'JetBrains Mono',monospace;border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .15s}
  .tab:hover{color:var(--text)}.tab.active{color:var(--amber);border-bottom-color:var(--amber)}
  .tab-panel{display:none}.tab-panel.active{display:block}

  /* ── Fields ── */
  .section-label{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--dim);margin-bottom:12px;padding-left:2px}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .field{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px;transition:border-color .15s}
  .field:hover{border-color:var(--muted)}
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
  .hotkey-field{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px;transition:border-color .15s}
  .hotkey-field.capturing{border-color:var(--amber);animation:capture-pulse 1s ease infinite}
  .hotkey-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .hotkey-value{color:var(--text);font-size:13px;flex:1}
  .hotkey-value.capturing{color:var(--amber)}
  .capture-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 10px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;white-space:nowrap}
  .capture-btn:hover{color:var(--text);background:#444}.capture-btn.active{background:rgba(245,158,11,.15);color:var(--amber)}

  /* ── Tone ── */
  .tone-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
  .tone-btn{padding:10px 0;background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--dim);font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;transition:all .15s;text-align:center}
  .tone-btn:hover{border-color:var(--muted);color:var(--text)}.tone-btn.active{border-color:var(--amber);color:var(--amber);background:rgba(245,158,11,.06)}
  .tone-btn.disabled{opacity:.3;pointer-events:none}

  /* ── Mic ── */
  .mic-section{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:16px;margin-bottom:12px}
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
  .history-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  .clear-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:none;border:none;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:color .15s}
  .clear-btn:hover{color:var(--red)}
  .history-list{display:flex;flex-direction:column;gap:8px}
  .history-item{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px;animation:slide-in .2s ease}
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
  .modal-icon{font-size:48px;margin-bottom:20px;text-align:center}
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
      <div class="modal-icon">🎤</div>
      <div class="modal-title">Welcome to Dictate</div>
      <div class="modal-subtitle">System-wide AI dictation for your Mac. Let's get you set up in 3 quick steps.</div>
      <button class="modal-btn" onclick="nextStep()">Get Started →</button>
      <button class="modal-btn-skip" onclick="skipOnboarding()">Skip setup</button>
    </div>

    <!-- Step 2: Hotkey -->
    <div class="modal-step" id="step1">
      <div class="modal-icon">⌨️</div>
      <div class="modal-title">Set Your Hotkey</div>
      <div class="modal-subtitle">Press the key you want to use to start/stop recording. You can change this anytime in Settings.</div>
      <div class="hotkey-field" id="onboardHotkeyField" style="margin-bottom:16px">
        <div class="field-label">Hotkey</div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="onboardHotkeyValue">Right Option (⌥)</span>
          <button class="capture-btn" id="onboardCaptureBtn" onclick="startOnboardCapture()">Assign</button>
        </div>
      </div>
      <button class="modal-btn" onclick="nextStep()">Next →</button>
    </div>

    <!-- Step 3: Permissions + mic test -->
    <div class="modal-step" id="step2">
      <div class="modal-icon">🔐</div>
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

<div class="app">
  <div class="header">
    <div class="wordmark">dict<span>.</span>ate</div>
    <div class="header-right">
      <span class="version-badge" id="versionBadge">loading...</span>
      <button class="theme-toggle" onclick="cycleTheme()" id="themeBtn">🌙</button>
    </div>
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

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" onclick="showTab('general')">General</button>
    <button class="tab" onclick="showTab('language')">Language</button>
    <button class="tab" onclick="showTab('overlay')">Overlay</button>
    <button class="tab" onclick="showTab('vocab')">Vocabulary</button>
    <button class="tab" onclick="showTab('apptones')">App Tones</button>
  </div>

  <!-- General tab -->
  <div class="tab-panel active" id="tab-general">
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Trigger Mode</div>
        <select id="mode" onchange="autoSave()">
          <option value="toggle">Toggle (press once)</option>
          <option value="hold">Hold (hold to record)</option>
        </select>
      </div>
      <div class="hotkey-field" id="hotkeyField">
        <div class="field-label">Dictation Hotkey</div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="hotkeyValue">Right Option (⌥)</span>
          <button class="capture-btn" id="captureBtn" onclick="toggleCapture()">Assign</button>
        </div>
      </div>
    </div>
    <div class="settings-grid">
      <div class="hotkey-field" id="uiShortcutField">
        <div class="field-label">Open UI Shortcut</div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="uiShortcutValue">Not set</span>
          <button class="capture-btn" id="captureUiBtn" onclick="toggleCaptureUi()">Assign</button>
        </div>
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

    <div class="section-label" style="margin-top:8px;">Cleanup Tone</div>
    <div class="tone-grid">
      <button class="tone-btn" data-tone="neutral"      onclick="setTone('neutral')">Neutral</button>
      <button class="tone-btn" data-tone="professional" onclick="setTone('professional')">Professional</button>
      <button class="tone-btn" data-tone="casual"       onclick="setTone('casual')">Casual</button>
      <button class="tone-btn" data-tone="concise"      onclick="setTone('concise')">Concise</button>
    </div>

    <div class="section-label">Microphone Test</div>
    <div class="mic-section">
      <div class="mic-header">
        <span class="field-label" style="margin:0">Input Level</span>
        <button class="mic-btn" id="micBtn" onclick="toggleMicTest()">Start Test</button>
      </div>
      <div class="meter-track"><div class="meter-fill" id="meterFill"></div></div>
      <div class="meter-hint" id="meterHint">Click "Start Test" and speak to check your mic level</div>
    </div>

    <button class="save-btn" id="generalSaveBtn" onclick="saveAndConfirm()">Save Settings</button>
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

  <div class="divider"></div>

  <!-- Version footer -->
  <div class="version-footer">
    <div style="font-size:11px;color:var(--dim)">dict<span style="color:var(--amber)">.</span>ate &nbsp;·&nbsp; <span id="versionFooter">loading...</span></div>
    <button onclick="checkForUpdates()" id="updateBtn" style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;">Check for Updates</button>
  </div>
  <div id="updateBanner" class="update-banner">
    <span id="updateMsg" style="font-size:12px;"></span>
    <a id="updateLink" href="https://github.com/mcolfax/dictate/releases" target="_blank" style="color:var(--amber);text-decoration:none;font-size:10px;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;">View Release →</a>
  </div>

  <!-- History -->
  <div class="history-header">
    <div class="section-label" style="margin:0">Recent Transcriptions</div>
    <button class="clear-btn" onclick="clearHistory()">Clear</button>
  </div>
  <div class="history-list" id="historyList">
    <div class="empty-state">No transcriptions yet</div>
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

async function startOnboardCapture() {
  await fetch('/api/capture/start', {method:'POST'});
  document.getElementById('onboardCaptureBtn').textContent = 'Press a key…';
  document.getElementById('onboardCaptureBtn').classList.add('active');
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
let isCapturing    = false;
let isCapturingUi  = false;
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
  const { enabled, recording, transcribing, capturing, capturing_ui,
          mic_testing, mic_level, overlay_text, config, history, stats } = data;

  // Power
  document.getElementById('powerBtn').className    = 'power-btn' + (enabled ? ' on' : '');
  document.getElementById('powerStatus').className = 'power-status ' + (enabled ? 'on' : 'off');
  document.getElementById('powerStatus').textContent = enabled ? 'Enabled' : 'Disabled';
  document.getElementById('powerHint').textContent   = enabled
    ? `Listening — ${config.hotkey_label} to ${config.mode === 'toggle' ? 'start/stop' : 'hold and record'}`
    : 'Click to enable dictation';

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

  // Hotkey capture
  isCapturing = capturing;
  const field = document.getElementById('hotkeyField');
  const val   = document.getElementById('hotkeyValue');
  const cbtn  = document.getElementById('captureBtn');
  if (capturing) {
    field.classList.add('capturing'); val.classList.add('capturing');
    val.textContent = 'Press any key or mouse button…';
    cbtn.textContent = 'Cancel'; cbtn.classList.add('active');
  } else {
    field.classList.remove('capturing'); val.classList.remove('capturing');
    val.textContent = config.hotkey_label || 'Right Option (⌥)';
    cbtn.textContent = 'Assign'; cbtn.classList.remove('active');
    // Update onboarding hotkey display
    document.getElementById('onboardHotkeyValue').textContent = config.hotkey_label || 'Right Option (⌥)';
    document.getElementById('onboardCaptureBtn').textContent = 'Assign';
    document.getElementById('onboardCaptureBtn').classList.remove('active');
  }

  // UI shortcut capture
  isCapturingUi = capturing_ui;
  const uiField = document.getElementById('uiShortcutField');
  const uiVal   = document.getElementById('uiShortcutValue');
  const uiCbtn  = document.getElementById('captureUiBtn');
  if (capturing_ui) {
    uiField.classList.add('capturing'); uiVal.classList.add('capturing');
    uiVal.textContent = 'Press any key…';
    uiCbtn.textContent = 'Cancel'; uiCbtn.classList.add('active');
  } else {
    uiField.classList.remove('capturing'); uiVal.classList.remove('capturing');
    uiVal.textContent = config.ui_shortcut_label || 'Not set';
    uiCbtn.textContent = 'Assign'; uiCbtn.classList.remove('active');
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

    // Theme
    if (config.theme) { currentTheme = config.theme; applyTheme(currentTheme); }

    // Show onboarding if not done
    if (!config.onboarding_done) {
      setTimeout(() => showOnboarding(), 500);
    }
  }

  // History
  const newKey = history ? history.map(h => h.ts + h.cleaned).join('|') : '';
  if (newKey !== lastHistoryKey) {
    lastHistoryKey = newKey;
    const list = document.getElementById('historyList');
    if (!history || history.length === 0) {
      list.innerHTML = '<div class="empty-state">No transcriptions yet</div>';
    } else {
      list.innerHTML = history.map(h => `
        <div class="history-item">
          <div class="history-meta">
            <span>${h.ts}</span>
            ${h.app ? `<span class="badge app-badge">${escHtml(h.app)}</span>` : ''}
            ${h.lang ? `<span class="badge lang-badge">${h.lang}</span>` : ''}
            <span class="badge ${h.cleanup_used ? 'clean-badge' : 'raw-badge'}">${h.cleanup_used ? 'AI cleaned' : 'raw'}</span>
          </div>
          <div class="history-text">${escHtml(h.cleaned)}</div>
          ${h.cleanup_used && h.raw !== h.cleaned ? `<div class="history-raw-text">raw: ${escHtml(h.raw)}</div>` : ''}
        </div>`).join('');
    }
  }
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const panels = ['general','language','overlay','vocab','apptones'];
    t.classList.toggle('active', panels[i] === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'vocab')     loadVocab();
  if (name === 'apptones')  loadAppTones();
}

async function togglePower() { await fetch('/api/toggle', {method:'POST'}); fetchStatus(); }
async function toggleCapture() {
  await fetch(isCapturing ? '/api/capture/cancel' : '/api/capture/start', {method:'POST'});
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

function toggleCleanup() { cleanupEnabled = document.getElementById('cleanupToggle').checked; updateCleanupUI(); autoSave(); }
function toggleClipboard() { clipboardOnly = document.getElementById('clipboardToggle').checked; updateClipboardUI(); autoSave(); }
function toggleSound() { soundEnabled = document.getElementById('soundToggle').checked; updateSoundUI(); autoSave(); }
function togglePause() { pauseEnabled = document.getElementById('pauseToggle').checked; updatePauseUI(); autoSave(); }
function toggleOverlay() { overlayEnabled = document.getElementById('overlayToggle').checked; updateOverlayUI(); autoSave(); }

function updatePauseLabel() { const v = document.getElementById('pauseSeconds').value; document.getElementById('pauseSecondsVal').textContent = v + 's'; }
function updateCleanupUI() { const l = document.getElementById('cleanupLabel'); l.textContent = cleanupEnabled ? 'On' : 'Off — raw only'; l.className = 'toggle-label' + (cleanupEnabled ? '' : ' off'); document.querySelectorAll('.tone-btn').forEach(b => b.classList.toggle('disabled', !cleanupEnabled)); }
function updateClipboardUI() { const l = document.getElementById('clipboardLabel'); l.textContent = clipboardOnly ? 'Clipboard only' : 'Auto-paste'; l.className = 'toggle-label' + (clipboardOnly ? ' off' : ''); }
function updateSoundUI() { const l = document.getElementById('soundLabel'); l.textContent = soundEnabled ? 'On' : 'Off'; l.className = 'toggle-label' + (soundEnabled ? '' : ' off'); }
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
fetchStatus();
setInterval(fetchStatus, 1000);
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
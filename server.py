#!/usr/bin/env python3
"""
server.py — Dictation control server + UI
"""

from flask import Flask, jsonify, request
import logging, threading, json, os, tempfile, subprocess, urllib.request, time, re
import numpy as np, sounddevice as sd, scipy.io.wavfile as wavfile
from pynput import keyboard as kb
from pynput import mouse as ms
from datetime import date

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Data dir — set by launcher when running as .app, fallback to script dir for dev
_DATA_DIR   = os.environ.get("APP_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_DATA_DIR, 'config.json')
STATS_FILE  = os.path.join(_DATA_DIR, 'stats.json')
SAMPLE_RATE    = 16000
OLLAMA_URL     = "http://localhost:11434/api/generate"
APP_VERSION    = "1.3.0"
GITHUB_RAW     = "https://raw.githubusercontent.com/mcolfax/dictate/main"
MAX_RECORD_SECS = 120

DEFAULT_CONFIG = {
    "mode":             "toggle",
    "hotkey":           "alt_r",
    "hotkey_label":     "Right Option (⌥)",
    "hotkey_type":      "keyboard",
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
    "mic_testing":       False,
    "mic_level":         0,
    "history":           [],
    "_recorded_frames":  [],
}

_recording_thread = None
_stop_event       = threading.Event()
_kb_listener      = None
_ms_listener      = None
_lock             = threading.Lock()
_last_sound_time  = 0.0

# ── SOUND FEEDBACK ────────────────────────────────────────────────────────────

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

# ── AUDIO — uses sd.rec() chunks to avoid Python 3.14 libffi/CoreAudio crash ──

# Persistent stream — stays open to avoid macOS mic indicator flicker
_persistent_stream = None

def _ensure_stream():
    global _persistent_stream
    if _persistent_stream is None or not _persistent_stream.active:
        try:
            _persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                dtype="int16", blocksize=1600
            )
            _persistent_stream.start()
            print("🎤 Audio stream ready")
        except Exception as e:
            print(f"⚠️  Stream init error: {e}")

def _record_worker():
    global _last_sound_time, _persistent_stream
    _last_sound_time = time.time()
    all_frames = []

    _ensure_stream()
    if _persistent_stream is None:
        print("⚠️  No audio stream available")
        return

    try:
        while not _stop_event.is_set() and state["recording"]:
            chunk, _ = _persistent_stream.read(1600)
            all_frames.append(chunk.copy())

            if config.get("pause_detection", True):
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                if rms > 200:
                    _last_sound_time = time.time()
                elif time.time() - _last_sound_time >= float(config.get("pause_seconds", 2.0)):
                    print("🤫 Silence detected — auto-stopping")
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
    global _recording_thread
    with _lock:
        if state["recording"]: return
        state["recording"]        = True
        state["_recorded_frames"] = []
    _stop_event.clear()
    play_sound("start")
    print("🎙️  Recording…")
    _recording_thread = threading.Thread(target=_record_worker, daemon=True)
    _recording_thread.start()

def stop_and_transcribe():
    global _recording_thread
    with _lock:
        if not state["recording"]: return
        state["recording"] = False
    _stop_event.set()
    if _recording_thread:
        _recording_thread.join(timeout=2)
    play_sound("stop")

    frames = state.get("_recorded_frames", [])
    if not frames: return
    state["transcribing"] = True
    print("⏳ Transcribing…")
    audio_data = np.concatenate(frames, axis=0).flatten()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wavfile.write(f.name, SAMPLE_RATE, audio_data)
        tmp_path = f.name
    try:
        import mlx_whisper
        result   = mlx_whisper.transcribe(tmp_path)
        raw_text = result["text"].strip()
        if not raw_text: return
        corrected   = apply_vocabulary(raw_text)
        active_app  = get_frontmost_app()
        app_tones   = config.get("app_tones", {})
        active_tone = app_tones.get(active_app, config.get("tone", "neutral"))
        print(f"📝 Raw: {raw_text}")
        if config.get("cleanup", True) and len(corrected.split()) > 3:
            final = cleanup_with_ollama(corrected, active_tone)
            print(f"✨ Cleaned: {final}")
        else:
            final = corrected
        output_text(final)
        record_transcription_stats(final)
        entry = {"raw": raw_text, "cleaned": final, "ts": time.strftime("%H:%M:%S"),
                 "app": active_app, "cleanup_used": config.get("cleanup", True),
                 "tone_used": active_tone}
        state["history"].insert(0, entry)
        state["history"] = state["history"][:30]
        play_sound("done")
    except Exception as e:
        print(f"❌ Error: {e}")
        play_sound("error")
    finally:
        os.unlink(tmp_path)
        state["transcribing"] = False

# ── MIC TEST ──────────────────────────────────────────────────────────────────

def _mic_test_worker():
    _ensure_stream()
    if _persistent_stream is None:
        return
    try:
        while state["mic_testing"]:
            chunk, _ = _persistent_stream.read(1600)
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            state["mic_level"] = min(100, int((rms / 32768) * 800))
    except Exception as e:
        print(f"⚠️  Mic test error: {e}")

def start_mic_test():
    if state["mic_testing"]: return
    state["mic_testing"] = True
    state["mic_level"]   = 0
    threading.Thread(target=_mic_test_worker, daemon=True).start()

def stop_mic_test():
    state["mic_testing"] = False
    state["mic_level"]   = 0

# ── CLEANUP ───────────────────────────────────────────────────────────────────

TONE_INSTRUCTIONS = {
    "neutral":      "Make no style changes beyond fixing errors.",
    "professional": "Also make the language slightly more formal where natural.",
    "casual":       "Also keep the tone relaxed and conversational.",
    "concise":      "Also trim any redundant words, but do not change the meaning.",
}

def cleanup_with_ollama(text, tone_override=None):
    tone_key = tone_override or config.get("tone", "neutral")
    tone     = TONE_INSTRUCTIONS.get(tone_key, TONE_INSTRUCTIONS["neutral"])
    prompt = (
        f"You are a transcription corrector. The text below was spoken aloud and auto-transcribed. "
        f"Your job is ONLY to fix typos, capitalization, and punctuation — nothing else. "
        f"Do NOT rephrase, summarize, answer, interpret, or change the meaning in any way. "
        f"Do NOT add any words that were not spoken. "
        f"Output ONLY the corrected spoken words. {tone}\n\n"
        f"Transcription: {text}\n\nCorrected transcription:"
    )
    payload = json.dumps({"model": config["ollama_model"], "prompt": prompt, "stream": False}).encode()
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

def handle_trigger_press():
    if not state["enabled"]: return
    if config["mode"] == "toggle":
        if not state["recording"]:
            threading.Thread(target=start_recording, daemon=True).start()
        else:
            threading.Thread(target=stop_and_transcribe, daemon=True).start()
    elif config["mode"] == "hold":
        if not state["recording"]:
            threading.Thread(target=start_recording, daemon=True).start()

def handle_trigger_release():
    if not state["enabled"]: return
    if config["mode"] == "hold" and state["recording"]:
        threading.Thread(target=stop_and_transcribe, daemon=True).start()

# ── KEYBOARD LISTENER ─────────────────────────────────────────────────────────

def get_kb_hotkey():
    try:
        return getattr(kb.Key, config.get("hotkey", "alt_r"))
    except AttributeError:
        return kb.Key.alt_r

def _key_label(key):
    labels = {
        kb.Key.alt_r: "Right Option (⌥)", kb.Key.alt: "Left Option (⌥)",
        kb.Key.f13: "F13", kb.Key.f14: "F14", kb.Key.f15: "F15",
        kb.Key.caps_lock: "Caps Lock", kb.Key.space: "Space",
    }
    return labels.get(key, key.name.replace("_", " ").title())

def on_kb_press(key):
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
    if config.get("hotkey_type") == "mouse" and btn_str == config.get("hotkey"):
        if pressed: handle_trigger_press()
        else:       handle_trigger_release()

# ── START LISTENERS ───────────────────────────────────────────────────────────

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
        "mic_testing": state["mic_testing"], "mic_level": state["mic_level"],
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

@app.route("/api/capture/start",  methods=["POST"])
def api_capture_start():  state["capturing"] = True;  return jsonify({"capturing": True})

@app.route("/api/capture/cancel", methods=["POST"])
def api_capture_cancel(): state["capturing"] = False; return jsonify({"capturing": False})

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

@app.route("/")
def index(): return HTML

# ── UI ────────────────────────────────────────────────────────────────────────

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
  :root{--bg:#080808;--surface:#111111;--border:#222222;--muted:#333333;--text:#e8e8e8;--dim:#666666;--amber:#f59e0b;--green:#22c55e;--red:#ef4444;--blue:#3b82f6}
  html,body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;min-height:100vh;line-height:1.5}
  .app{max-width:820px;margin:0 auto;padding:48px 24px 80px}
  .header{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:48px;padding-bottom:24px;border-bottom:1px solid var(--border)}
  .wordmark{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px}
  .wordmark span{color:var(--amber)}
  .version{font-size:11px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
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
  .stats-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:32px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:12px 14px}
  .stat-value{font-family:'Syne',sans-serif;font-size:22px;font-weight:700;color:var(--amber);margin-bottom:2px}
  .stat-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
  .tabs{display:flex;gap:2px;margin-bottom:24px;border-bottom:1px solid var(--border);padding-bottom:0}
  .tab{padding:10px 18px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);cursor:pointer;border:none;background:none;font-family:'JetBrains Mono',monospace;border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .15s}
  .tab:hover{color:var(--text)}.tab.active{color:var(--amber);border-bottom-color:var(--amber)}
  .tab-panel{display:none}.tab-panel.active{display:block}
  .section-label{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--dim);margin-bottom:12px;padding-left:2px}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .field{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px;transition:border-color .15s}
  .field:hover{border-color:var(--muted)}
  .field-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
  select,input[type="text"]{width:100%;background:transparent;border:none;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;cursor:pointer;appearance:none;-webkit-appearance:none}
  select option{background:#1a1a1a}
  .toggle-row{display:flex;align-items:center;justify-content:space-between}
  .toggle-switch{position:relative;width:36px;height:20px;flex-shrink:0}
  .toggle-switch input{opacity:0;width:0;height:0}
  .toggle-slider{position:absolute;inset:0;background:var(--muted);border-radius:20px;cursor:pointer;transition:.2s}
  .toggle-slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:var(--dim);border-radius:50%;transition:.2s}
  .toggle-switch input:checked+.toggle-slider{background:rgba(245,158,11,.3)}
  .toggle-switch input:checked+.toggle-slider::before{transform:translateX(16px);background:var(--amber)}
  .toggle-label{font-size:13px;color:var(--text)}.toggle-label.off{color:var(--dim)}
  .hotkey-field{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px;transition:border-color .15s}
  .hotkey-field.capturing{border-color:var(--amber);animation:capture-pulse 1s ease infinite}
  .hotkey-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .hotkey-value{color:var(--text);font-size:13px;flex:1}
  .hotkey-value.capturing{color:var(--amber)}
  .capture-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 10px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;white-space:nowrap}
  .capture-btn:hover{color:var(--text);background:#444}.capture-btn.active{background:rgba(245,158,11,.15);color:var(--amber)}
  .tone-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
  .tone-btn{padding:10px 0;background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--dim);font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;transition:all .15s;text-align:center}
  .tone-btn:hover{border-color:var(--muted);color:var(--text)}.tone-btn.active{border-color:var(--amber);color:var(--amber);background:rgba(245,158,11,.06)}
  .tone-btn.disabled{opacity:.3;pointer-events:none}
  .mic-section{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:16px;margin-bottom:12px}
  .mic-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  .mic-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:var(--muted);border:none;border-radius:3px;padding:5px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;color:var(--dim)}
  .mic-btn:hover{color:var(--text);background:#444}.mic-btn.testing{background:rgba(239,68,68,.15);color:var(--red)}
  .meter-track{height:8px;background:var(--muted);border-radius:4px;overflow:hidden}
  .meter-fill{height:100%;width:0%;border-radius:4px;background:linear-gradient(90deg,var(--green) 0%,var(--amber) 70%,var(--red) 100%);transition:width .05s ease}
  .meter-hint{font-size:10px;color:var(--dim);margin-top:8px;letter-spacing:.05em}
  .vocab-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
  .vocab-row{display:flex;align-items:center;gap:8px}
  .vocab-input{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:8px 10px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;transition:border-color .15s}
  .vocab-input:focus{border-color:var(--muted)}
  .vocab-arrow{color:var(--dim);font-size:14px;flex-shrink:0}
  .vocab-del{background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;padding:0 4px;line-height:1;transition:color .15s;flex-shrink:0}
  .vocab-del:hover{color:var(--red)}
  .add-btn{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:8px 14px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;width:100%}
  .add-btn:hover{color:var(--text);background:#444}
  .app-tone-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
  .app-tone-row{display:grid;grid-template-columns:1fr auto 120px auto;align-items:center;gap:8px}
  .app-tone-input,.app-tone-select{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:8px 10px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none}
  .app-tone-select{appearance:none;-webkit-appearance:none;cursor:pointer}
  .app-tone-select option{background:#1a1a1a}
  .app-hint{font-size:11px;color:var(--dim);margin-bottom:12px;line-height:1.6}
  .save-btn{width:100%;padding:12px;background:transparent;border:1px solid var(--muted);border-radius:4px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:8px}
  .save-btn:hover{border-color:var(--amber);color:var(--amber)}.save-btn.saved{border-color:var(--green);color:var(--green)}
  .divider{height:1px;background:var(--border);margin:32px 0}
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
  .history-text{color:var(--text);line-height:1.6}
  .history-raw-text{font-size:11px;color:var(--dim);margin-top:4px}
  .empty-state{text-align:center;color:var(--dim);padding:40px 0;font-size:12px}
  @keyframes ring-pulse{0%,100%{opacity:.6;transform:scale(1)}50%{opacity:0;transform:scale(1.15)}}
  @keyframes dot-pulse{0%,100%{opacity:1}50%{opacity:.3}}
  @keyframes slide-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
  @keyframes capture-pulse{0%,100%{border-color:var(--amber)}50%{border-color:rgba(245,158,11,.3)}}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <div class="wordmark">dict<span>.</span>ate</div>
    <div class="version">Local AI Dictation</div>
  </div>
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
  <div class="stats-bar">
    <div class="stat-card"><div class="stat-value" id="statWordsToday">0</div><div class="stat-label">Words Today</div></div>
    <div class="stat-card"><div class="stat-value" id="statSessionsToday">0</div><div class="stat-label">Sessions Today</div></div>
    <div class="stat-card"><div class="stat-value" id="statWordsTotal">0</div><div class="stat-label">Words Total</div></div>
    <div class="stat-card"><div class="stat-value" id="statSessionsTotal">0</div><div class="stat-label">Sessions Total</div></div>
  </div>
  <div class="tabs">
    <button class="tab active" onclick="showTab('general')">General</button>
    <button class="tab" onclick="showTab('vocab')">Vocabulary</button>
    <button class="tab" onclick="showTab('apptones')">App Tones</button>
  </div>
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
        <div class="field-label">Hotkey / Mouse Button</div>
        <div class="hotkey-row">
          <span class="hotkey-value" id="hotkeyValue">Right Option (⌥)</span>
          <button class="capture-btn" id="captureBtn" onclick="toggleCapture()">Assign</button>
        </div>
      </div>
    </div>
    <div class="settings-grid">
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
      <div class="field">
        <div class="field-label">Ollama Model</div>
        <select id="ollama_model" onchange="autoSave()">
          <option value="llama3.2">llama3.2 (recommended)</option>
          <option value="llama3.2:1b">llama3.2:1b (fast)</option>
          <option value="qwen2.5:0.5b">qwen2.5:0.5b (fastest)</option>
          <option value="qwen2.5:1.5b">qwen2.5:1.5b (fast)</option>
          <option value="mistral">mistral</option>
          <option value="llama3.1">llama3.1</option>
        </select>
      </div>
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">AI Cleanup</div>
        <div class="toggle-row">
          <span class="toggle-label" id="cleanupLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="cleanupToggle" onchange="toggleCleanup()"><span class="toggle-slider"></span></label>
        </div>
      </div>
      <div class="field">
        <div class="field-label">Output Mode</div>
        <div class="toggle-row">
          <span class="toggle-label" id="clipboardLabel">Auto-paste</span>
          <label class="toggle-switch"><input type="checkbox" id="clipboardToggle" onchange="toggleClipboard()"><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>
    <div class="settings-grid">
      <div class="field">
        <div class="field-label">Sound Feedback</div>
        <div class="toggle-row">
          <span class="toggle-label" id="soundLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="soundToggle" onchange="toggleSound()"><span class="toggle-slider"></span></label>
        </div>
      </div>
      <div class="field">
        <div class="field-label">Pause Detection</div>
        <div class="toggle-row">
          <span class="toggle-label" id="pauseLabel">On</span>
          <label class="toggle-switch"><input type="checkbox" id="pauseToggle" onchange="togglePause()"><span class="toggle-slider"></span></label>
        </div>
      </div>
    </div>
    <div class="field" style="margin-bottom:12px;" id="pauseSecondsField">
      <div class="field-label">Silence Threshold (seconds)</div>
      <input type="range" id="pauseSeconds" min="1" max="5" step="0.5" value="2"
             oninput="updatePauseLabel()" onchange="autoSave()"
             style="width:100%;accent-color:var(--amber);margin-top:4px">
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-top:4px">
        <span>1s</span><span id="pauseSecondsVal">2s</span><span>5s</span>
      </div>
    </div>
    <div class="section-label" style="margin-top:8px;">Cleanup Tone</div>
    <div class="tone-grid" id="toneGrid">
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
  </div>
  <div class="tab-panel" id="tab-vocab">
    <div class="app-hint">Words Whisper consistently mishears — corrected before AI cleanup.</div>
    <div class="vocab-list" id="vocabList"></div>
    <button class="add-btn" onclick="addVocabRow()">+ Add Entry</button>
    <button class="save-btn" id="vocabSaveBtn" onclick="saveVocab()">Save Vocabulary</button>
  </div>
  <div class="tab-panel" id="tab-apptones">
    <div class="app-hint">Different cleanup tone per app. App name must match exactly (e.g. "Slack", "Mail", "Notes", "Arc").</div>
    <div class="app-tone-list" id="appToneList"></div>
    <button class="add-btn" onclick="addAppToneRow()">+ Add App</button>
    <button class="save-btn" id="appToneSaveBtn" onclick="saveAppTones()">Save App Tones</button>
  </div>
  <div class="divider"></div>
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;">
    <div style="font-size:11px;color:var(--dim)">dict<span style="color:var(--amber)">.</span>ate &nbsp;&middot;&nbsp; <span id="versionBadge">loading...</span></div>
    <button onclick="checkForUpdates()" id="updateBtn" style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);background:var(--muted);border:none;border-radius:3px;padding:4px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s;">Check for Updates</button>
  </div>
  <div id="updateBanner" style="display:none;background:rgba(245,158,11,.08);border:1px solid var(--amber);border-radius:4px;padding:12px 16px;margin-bottom:24px;align-items:center;justify-content:space-between;gap:16px;">
    <span id="updateMsg" style="font-size:12px;"></span>
    <a id="updateLink" href="https://github.com/mcolfax/dictate/releases" target="_blank" style="color:var(--amber);text-decoration:none;font-size:10px;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;">View Release →</a>
  </div>
  <div class="history-header">
    <div class="section-label" style="margin:0">Recent Transcriptions</div>
    <button class="clear-btn" onclick="clearHistory()">Clear</button>
  </div>
  <div class="history-list" id="historyList">
    <div class="empty-state">No transcriptions yet</div>
  </div>
</div>
<script>
let currentConfig={},currentTone='neutral',cleanupEnabled=true,clipboardOnly=false,soundEnabled=true,pauseEnabled=true,isCapturing=false,isMicTesting=false,lastHistoryKey='';
function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{['general','vocab','apptones'].forEach((p,j)=>{if(i===j)t.classList.toggle('active',p===name)})});
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));
  if(name==='vocab')loadVocab();
  if(name==='apptones')loadAppTones();
}
async function fetchStatus(){try{const data=await(await fetch('/api/status')).json();applyStatus(data);}catch(e){}}
function applyStatus(data){
  const{enabled,recording,transcribing,capturing,mic_testing,mic_level,config,history,stats}=data;
  document.getElementById('powerBtn').className='power-btn'+(enabled?' on':'');
  document.getElementById('powerStatus').className='power-status '+(enabled?'on':'off');
  document.getElementById('powerStatus').textContent=enabled?'Enabled':'Disabled';
  document.getElementById('powerHint').textContent=enabled?`Listening — ${config.hotkey_label} to ${config.mode==='toggle'?'start/stop':'hold and record'}`:'Click to enable dictation';
  const ind=document.getElementById('indicator');
  ind.className='indicator'+(recording?' recording':transcribing?' transcribing':'');
  document.getElementById('indicatorText').textContent=recording?'Recording':transcribing?'Processing':'Idle';
  if(stats){
    document.getElementById('statWordsToday').textContent=stats.words_today||0;
    document.getElementById('statSessionsToday').textContent=stats.sessions_today||0;
    document.getElementById('statWordsTotal').textContent=stats.words_total||0;
    document.getElementById('statSessionsTotal').textContent=stats.sessions_total||0;
  }
  isCapturing=capturing;
  const field=document.getElementById('hotkeyField'),val=document.getElementById('hotkeyValue'),cbtn=document.getElementById('captureBtn');
  if(capturing){field.classList.add('capturing');val.classList.add('capturing');val.textContent='Press any key or mouse button…';cbtn.textContent='Cancel';cbtn.classList.add('active');}
  else{field.classList.remove('capturing');val.classList.remove('capturing');val.textContent=config.hotkey_label||'Right Option (⌥)';cbtn.textContent='Assign';cbtn.classList.remove('active');}
  isMicTesting=mic_testing;
  const micBtn=document.getElementById('micBtn'),meter=document.getElementById('meterFill'),hint=document.getElementById('meterHint');
  if(mic_testing){micBtn.textContent='Stop Test';micBtn.classList.add('testing');meter.style.width=mic_level+'%';hint.textContent=mic_level<5?'No signal — check mic permissions':mic_level<20?'Low — speak louder':mic_level<70?'✓ Good level':'Too loud';}
  else{micBtn.textContent='Start Test';micBtn.classList.remove('testing');meter.style.width='0%';hint.textContent='Click "Start Test" and speak to check your mic level';}
  if(JSON.stringify(config)!==JSON.stringify(currentConfig)){
    currentConfig=config;
    document.getElementById('mode').value=config.mode||'toggle';
    document.getElementById('whisper_model').value=config.whisper_model||'mlx-community/whisper-small-mlx';
    document.getElementById('ollama_model').value=config.ollama_model||'llama3.2';
    cleanupEnabled=config.cleanup!==false;clipboardOnly=config.clipboard_only===true;
    soundEnabled=config.sound_feedback!==false;pauseEnabled=config.pause_detection!==false;
    const ps=config.pause_seconds||2.0;
    document.getElementById('cleanupToggle').checked=cleanupEnabled;
    document.getElementById('clipboardToggle').checked=clipboardOnly;
    document.getElementById('soundToggle').checked=soundEnabled;
    document.getElementById('pauseToggle').checked=pauseEnabled;
    document.getElementById('pauseSeconds').value=ps;
    document.getElementById('pauseSecondsVal').textContent=ps+'s';
    updateCleanupUI();updateClipboardUI();updateSoundUI();updatePauseUI();setTone(config.tone||'neutral');
  }
  const newKey=history?history.map(h=>h.ts+h.cleaned).join('|'):'';
  if(newKey!==lastHistoryKey){
    lastHistoryKey=newKey;
    const list=document.getElementById('historyList');
    if(!history||history.length===0){list.innerHTML='<div class="empty-state">No transcriptions yet</div>';}
    else{list.innerHTML=history.map(h=>`<div class="history-item"><div class="history-meta"><span>${h.ts}</span>${h.app?`<span class="badge app-badge">${escHtml(h.app)}</span>`:''}<span class="badge ${h.cleanup_used?'clean-badge':'raw-badge'}">${h.cleanup_used?'AI cleaned':'raw'}</span>${h.cleanup_used&&h.tone_used?`<span class="badge raw-badge">${h.tone_used}</span>`:''}</div><div class="history-text">${escHtml(h.cleaned)}</div>${h.cleanup_used&&h.raw!==h.cleaned?`<div class="history-raw-text">raw: ${escHtml(h.raw)}</div>`:''}</div>`).join('');}
  }
}
function escHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function togglePower(){await fetch('/api/toggle',{method:'POST'});fetchStatus();}
async function toggleCapture(){await fetch(isCapturing?'/api/capture/cancel':'/api/capture/start',{method:'POST'});fetchStatus();}
async function toggleMicTest(){await fetch(isMicTesting?'/api/mic/stop':'/api/mic/start',{method:'POST'});fetchStatus();}
function toggleCleanup(){cleanupEnabled=document.getElementById('cleanupToggle').checked;updateCleanupUI();autoSave();}
function toggleClipboard(){clipboardOnly=document.getElementById('clipboardToggle').checked;updateClipboardUI();autoSave();}
function toggleSound(){soundEnabled=document.getElementById('soundToggle').checked;updateSoundUI();autoSave();}
function togglePause(){pauseEnabled=document.getElementById('pauseToggle').checked;updatePauseUI();autoSave();}
function updatePauseLabel(){const v=document.getElementById('pauseSeconds').value;document.getElementById('pauseSecondsVal').textContent=v+'s';document.getElementById('pauseLabel').textContent=pauseEnabled?`On — ${v}s silence`:'Off';}
function updateCleanupUI(){const lbl=document.getElementById('cleanupLabel');lbl.textContent=cleanupEnabled?'On — fix punctuation & fillers':'Off — raw only';lbl.className='toggle-label'+(cleanupEnabled?'':' off');document.querySelectorAll('.tone-btn').forEach(b=>b.classList.toggle('disabled',!cleanupEnabled));}
function updateClipboardUI(){const lbl=document.getElementById('clipboardLabel');lbl.textContent=clipboardOnly?'Clipboard only':'Auto-paste';lbl.className='toggle-label'+(clipboardOnly?' off':'');}
function updateSoundUI(){const lbl=document.getElementById('soundLabel');lbl.textContent=soundEnabled?'On':'Off';lbl.className='toggle-label'+(soundEnabled?'':' off');}
function updatePauseUI(){const v=document.getElementById('pauseSeconds').value;const lbl=document.getElementById('pauseLabel');lbl.textContent=pauseEnabled?`On — ${v}s silence`:'Off';lbl.className='toggle-label'+(pauseEnabled?'':' off');document.getElementById('pauseSecondsField').style.opacity=pauseEnabled?'1':'0.4';}
function setTone(tone){currentTone=tone;document.querySelectorAll('.tone-btn').forEach(b=>b.classList.toggle('active',b.dataset.tone===tone));autoSave();}
async function autoSave(){await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:document.getElementById('mode').value,whisper_model:document.getElementById('whisper_model').value,ollama_model:document.getElementById('ollama_model').value,tone:currentTone,cleanup:cleanupEnabled,clipboard_only:clipboardOnly,sound_feedback:soundEnabled,pause_detection:pauseEnabled,pause_seconds:parseFloat(document.getElementById('pauseSeconds').value)})});}
async function clearHistory(){
  await fetch('/api/history/clear',{method:'POST'});
  lastHistoryKey='';
  document.getElementById('historyList').innerHTML='<div class="empty-state">No transcriptions yet</div>';
  fetchStatus();
}
let vocabRows=[];
async function loadVocab(){const data=await(await fetch('/api/vocab')).json();vocabRows=data.map(e=>({from:e.from,to:e.to}));renderVocab();}
function renderVocab(){const list=document.getElementById('vocabList');if(vocabRows.length===0){list.innerHTML='<div class="empty-state" style="padding:20px 0">No entries yet</div>';return;}list.innerHTML=vocabRows.map((r,i)=>`<div class="vocab-row"><input class="vocab-input" placeholder="mishear" value="${escHtml(r.from)}" oninput="vocabRows[${i}].from=this.value"><span class="vocab-arrow">→</span><input class="vocab-input" placeholder="correction" value="${escHtml(r.to)}" oninput="vocabRows[${i}].to=this.value"><button class="vocab-del" onclick="removeVocabRow(${i})">×</button></div>`).join('');}
function addVocabRow(){vocabRows.push({from:'',to:''});renderVocab();}
function removeVocabRow(i){vocabRows.splice(i,1);renderVocab();}
async function saveVocab(){await fetch('/api/vocab',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(vocabRows.filter(r=>r.from.trim()))});const btn=document.getElementById('vocabSaveBtn');btn.textContent='Saved ✓';btn.classList.add('saved');setTimeout(()=>{btn.textContent='Save Vocabulary';btn.classList.remove('saved');},2000);}
let appToneRows=[];
async function loadAppTones(){const data=await(await fetch('/api/app_tones')).json();appToneRows=Object.entries(data).map(([app,tone])=>({app,tone}));renderAppTones();}
function renderAppTones(){const list=document.getElementById('appToneList');if(appToneRows.length===0){list.innerHTML='<div class="empty-state" style="padding:20px 0">No app profiles yet</div>';return;}const tones=['neutral','professional','casual','concise'];list.innerHTML=appToneRows.map((r,i)=>`<div class="app-tone-row"><input class="app-tone-input" placeholder="App name" value="${escHtml(r.app)}" oninput="appToneRows[${i}].app=this.value"><span class="vocab-arrow">→</span><select class="app-tone-select" onchange="appToneRows[${i}].tone=this.value">${tones.map(t=>`<option value="${t}" ${r.tone===t?'selected':''}>${t}</option>`).join('')}</select><button class="vocab-del" onclick="removeAppToneRow(${i})">×</button></div>`).join('');}
function addAppToneRow(){appToneRows.push({app:'',tone:'neutral'});renderAppTones();}
function removeAppToneRow(i){appToneRows.splice(i,1);renderAppTones();}
async function saveAppTones(){const obj={};appToneRows.filter(r=>r.app.trim()).forEach(r=>obj[r.app.trim()]=r.tone);await fetch('/api/app_tones',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});const btn=document.getElementById('appToneSaveBtn');btn.textContent='Saved ✓';btn.classList.add('saved');setTimeout(()=>{btn.textContent='Save App Tones';btn.classList.remove('saved');},2000);}
async function checkForUpdates(){
  const btn=document.getElementById('updateBtn');
  btn.textContent='Checking…';btn.style.color='var(--amber)';
  try{
    const data=await(await fetch('/api/version')).json();
    document.getElementById('versionBadge').textContent='v'+data.current;
    const banner=document.getElementById('updateBanner');
    const msg=document.getElementById('updateMsg');
    if(data.update_available){
      msg.textContent=`✨ v${data.latest} is available (you have v${data.current})`;
      banner.style.display='flex';btn.textContent='⬆️ Update Available';btn.style.color='var(--amber)';
    }else if(data.latest){
      msg.textContent=`✅ You’re on the latest version (v${data.current})`;
      document.getElementById('updateLink').style.display='none';
      banner.style.display='flex';btn.textContent='Up to Date ✓';btn.style.color='var(--green)';
      setTimeout(()=>{banner.style.display='none';document.getElementById('updateLink').style.display='';btn.textContent='Check for Updates';btn.style.color='var(--dim)';},4000);
    }else{
      btn.textContent='No Internet';
      setTimeout(()=>{btn.textContent='Check for Updates';btn.style.color='var(--dim)';},3000);
    }
  }catch(e){btn.textContent='Error — try again';setTimeout(()=>{btn.textContent='Check for Updates';btn.style.color='var(--dim)';},3000);}
}
// Load version on page load
fetch('/api/version').then(r=>r.json()).then(data=>{
  document.getElementById('versionBadge').textContent='v'+data.current;
}).catch(()=>{});
fetchStatus();setInterval(fetchStatus,1000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    start_listener()
    print("━" * 50)
    print("🎤  Dictation server ready")
    print("    Open http://localhost:5001 in Arc")
    print("    Press Ctrl+C to quit")
    print("━" * 50)
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)

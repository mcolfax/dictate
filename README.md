# 🎤 dict.ai — Local AI Dictation for macOS

Free, private, system-wide voice dictation powered by Whisper + Ollama.  
No cloud. No subscriptions. No API keys. Everything runs on your Mac.

---

## Requirements

- macOS with **Apple Silicon** (M1/M2/M3/M4)
- ~5GB free disk space (for AI models)
- Internet connection during first launch only

---

## Install

1. Download **dict.ai.dmg** from the [latest release](https://github.com/mcolfax/dictai/releases/latest)
2. Open the DMG and drag **dict.ai.app** to your Applications folder
3. Open **dict.ai.app** from Applications  
   ⚠️ **Important:** Right-click → Open (do NOT double-click the first time)  
   If you see "damaged or incomplete", run this in Terminal:  
   `xattr -cr /Applications/dict.ai.app`  
   Then try opening again.
4. The app will automatically install all dependencies in the background — watch the menu bar for progress
5. Once setup is complete, a notification will appear and the UI will open

**Total setup time: ~5 minutes** depending on internet speed.

---

## First Launch Permissions

macOS will prompt for two permissions — both are required:

- **Microphone** — for voice capture
- **Accessibility** — for hotkey detection and text injection  
  *(System Settings → Privacy & Security → Accessibility → enable dict.ai)*

---

## Usage

| Action | Default |
|--------|---------|
| Start recording | Press **Right Option (⌥)** — configurable |
| Stop recording | Press **Right Option (⌥)** again |
| Open settings | http://localhost:5001 |

Text is automatically transcribed, cleaned up by AI, and pasted into whatever app you're using.

The menu bar icon shows your current status:
- 🖤 **Black waveform** — Dictate is enabled, ready to record
- 🟠 **Amber waveform** — Recording in progress

Your hotkey is fully configurable — assign any key or mouse button from the settings UI.

---

## Features

- 🎙️ **System-wide** — works in any app (Slack, Mail, Notes, browser, etc.)
- ✨ **AI cleanup** — fixes punctuation, removes filler words (um, uh, like)
- 🔑 **Custom hotkey** — any key or mouse button
- 🎭 **Tone profiles** — neutral, professional, casual, concise
- 📱 **Per-app tones** — different style per application
- 📖 **Custom vocabulary** — fix words Whisper consistently mishears
- 🤫 **Pause detection** — auto-stops after configurable silence
- 📊 **Session stats** — words and sessions tracked
- 🔇 **Clipboard mode** — copy without auto-pasting
- 🔈 **Sound feedback** — audio cues on start/stop/done
- 🔄 **Auto-update** — notifies you when a new version is available
- ⚡ **Multiple model options** — choose speed vs accuracy tradeoff

---

## Model Options

### Whisper (transcription)
| Model | Speed | Accuracy |
|-------|-------|---------|
| tiny | ⚡⚡⚡⚡⚡ | Basic |
| base | ⚡⚡⚡⚡ | Decent |
| small | ⚡⚡⚡ | Good (default) |
| medium | ⚡⚡ | Better |
| large-v3 | ⚡ | Best |

### Ollama (AI cleanup)
| Model | Speed | Quality |
|-------|-------|---------|
| qwen2.5:0.5b | ⚡⚡⚡⚡⚡ | Good |
| llama3.2:1b | ⚡⚡⚡⚡ | Good |
| llama3.2 | ⚡⚡⚡ | Great (default) |

---

## Auto-start at Login

**System Settings → General → Login Items → add dict.ai.app**

---

## Updating

Dictate checks for updates automatically. When a new version is available you'll see **"⬆️ Update Available"** in the menu bar. Click it to update in one step.

You can also check manually from the settings UI (bottom of the page).

---

## Troubleshooting

**Hotkey not working?**  
→ System Settings → Privacy & Security → Accessibility → make sure Dictate is enabled

**Mic not picking up audio?**  
→ Use the Mic Test in the settings UI  
→ System Settings → Privacy & Security → Microphone → enable dict.ai

**UI not loading?**  
→ Make sure dict.ai.app is running (check menu bar for waveform icon)  
→ Open http://localhost:5001 in your browser

**Ollama errors?**  
→ Quit and reopen dict.ai.app — it will restart Ollama automatically

---

## Uninstall

```bash
rm -rf /Applications/dict.ai.app
rm -rf ~/.dictate
```

To also remove Ollama models: `rm -rf ~/.ollama`

---

## How it works

```
Your voice
    ↓
Whisper (Apple Silicon, runs locally)
    ↓
Raw transcription
    ↓
Vocabulary corrections
    ↓
Ollama / llama3.2 (runs locally)
    ↓
Cleaned text → pasted into your app
```

All processing happens on your Mac. Nothing is sent to any server.

---

## Support This Project ☕

Dictate is free and always will be. If it's saving you time and you'd like to say thanks, a small tip goes a long way toward keeping updates coming.

**Venmo: [@mcolfax](https://venmo.com/mcolfax)**

No pressure — enjoying the app is thanks enough. 🙏

---

Built by [@mcolfax](https://github.com/mcolfax)

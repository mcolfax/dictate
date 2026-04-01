#!/bin/bash
# install.sh — Dictate installer for macOS (Apple Silicon)

set -e

APP_DIR="$HOME/.dictate"          # venv, models config live here (hidden)
APP_BUNDLE="/Applications/Dictate.app"
RESOURCES="$APP_BUNDLE/Contents/Resources"
GREEN='\033[0;32m'; AMBER='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${AMBER}→  $1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; exit 1; }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🎤  Dictate — Installer"
echo "  Local AI Dictation for macOS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check Apple Silicon ───────────────────────────────────────────────────────
[[ $(uname -m) == "arm64" ]] || err "Requires Apple Silicon (M1/M2/M3/M4)"
ok "Apple Silicon detected"

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    info "Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi
ok "Homebrew ready"

# ── Ollama ────────────────────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
    info "Installing Ollama…"
    brew install ollama
fi
ok "Ollama ready"

# ── ffmpeg ────────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
    info "Installing ffmpeg…"
    brew install ffmpeg
fi
ok "ffmpeg ready"

# ── Python venv in hidden app dir ─────────────────────────────────────────────
mkdir -p "$APP_DIR"
info "Creating Python environment…"
python3 -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"
info "Upgrading pip…"
pip install --quiet --upgrade pip
info "Installing Python packages (may take a few minutes)…"
pip install --quiet mlx-whisper sounddevice scipy numpy pynput flask rumps \
    pyobjc-framework-WebKit pyobjc-framework-Quartz pyobjc-framework-AVFoundation
ok "Python environment ready"

# ── Pull Ollama model ─────────────────────────────────────────────────────────
info "Downloading AI model (~2GB, one-time)…"
ollama serve > /tmp/ollama_install.log 2>&1 &
OLLAMA_PID=$!
sleep 3
ollama pull llama3.2
kill $OLLAMA_PID 2>/dev/null
ok "AI model ready"

# ── Build Dictate.app ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Building Dictate.app…"

MACOS="$APP_BUNDLE/Contents/MacOS"
rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS" "$RESOURCES"

# Generate icons
python3 "$SCRIPT_DIR/make_icons.py"
cp "$SCRIPT_DIR"/icon*.png "$RESOURCES/"
cp "$SCRIPT_DIR/icon.icns" "$RESOURCES/"

# Copy Python source files into the bundle
cp "$SCRIPT_DIR/server.py"          "$RESOURCES/"
cp "$SCRIPT_DIR/overlay.py"         "$RESOURCES/"
cp "$SCRIPT_DIR/settings_window.py" "$RESOURCES/"
cp "$SCRIPT_DIR/app.py"             "$RESOURCES/"
cp "$SCRIPT_DIR/make_icons.py"      "$RESOURCES/"

# Write default config into app data dir
cat > "$APP_DIR/config.json" << CONFIG
{
  "mode": "toggle",
  "hotkey": "alt_r",
  "hotkey_label": "Right Option (\u2325)",
  "hotkey_type": "keyboard",
  "whisper_model": "mlx-community/whisper-small-mlx",
  "ollama_model": "llama3.2:latest",
  "tone": "neutral",
  "cleanup": true,
  "clipboard_only": false,
  "sound_feedback": true,
  "pause_detection": true,
  "pause_seconds": 2.0,
  "vocabulary": [],
  "app_tones": {}
}
CONFIG

# Launcher — references files inside the bundle
cat > "$MACOS/Dictate" << 'LAUNCHER'
#!/bin/bash
BUNDLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$BUNDLE_DIR/Resources"
DATA_DIR="$HOME/.dictate"
PYTHON="$DATA_DIR/venv/bin/python3"

mkdir -p "$DATA_DIR"

# Copy resources to data dir if newer (enables bundle-level updates)
for f in server.py app.py make_icons.py overlay.py settings_window.py; do
    if [ ! -f "$DATA_DIR/$f" ] || [ "$RESOURCES/$f" -nt "$DATA_DIR/$f" ]; then
        cp "$RESOURCES/$f" "$DATA_DIR/" 2>/dev/null
    fi
done
cp "$RESOURCES"/icon*.png "$DATA_DIR/" 2>/dev/null

# Start Ollama if needed
if ! curl -s http://localhost:11434 > /dev/null 2>&1; then
    /opt/homebrew/bin/ollama serve > /tmp/ollama.log 2>&1 &
    sleep 2
fi

export APP_DATA_DIR="$DATA_DIR"
export APP_RESOURCES="$RESOURCES"
cd "$DATA_DIR"
exec arch -arm64 "$PYTHON" "$RESOURCES/app.py"
LAUNCHER
chmod +x "$MACOS/Dictate"

cat > "$APP_BUNDLE/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Dictate</string>
    <key>CFBundleDisplayName</key><string>Dictate</string>
    <key>CFBundleIdentifier</key><string>com.local.dictate</string>
    <key>CFBundleVersion</key><string>1.0.0</string>
    <key>CFBundleShortVersionString</key><string>1.0.0</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>CFBundleExecutable</key><string>Dictate</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><false/>
    <key>NSMicrophoneUsageDescription</key><string>Dictate needs microphone access for voice transcription.</string>
    <key>NSAppleEventsUsageDescription</key><string>Dictate needs accessibility access to type text into other apps.</string>
</dict>
</plist>
PLIST

ok "Dictate.app installed to /Applications"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}  🎉  Dictate installed!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Open Dictate from /Applications"
echo "     (right-click → Open the first time)"
echo ""
echo "  2. Grant permissions when prompted:"
echo "     • Microphone"
echo "     • Accessibility (System Settings →"
echo "       Privacy & Security → Accessibility)"
echo ""
echo "  3. Open http://localhost:5001 to configure"
echo ""
echo "  4. Optional — auto-start at login:"
echo "     System Settings → General → Login Items"
echo "     → add Dictate.app"
echo ""
open /Applications/Dictate.app

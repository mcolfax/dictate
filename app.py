#!/usr/bin/env python3
"""
app.py — Dictate menu bar app
First launch: silently installs dependencies with menu bar progress.
Subsequent launches: starts normally.
"""

import subprocess, sys, os, time, urllib.request, json, threading, shutil, re

APP_RESOURCES   = os.environ.get("APP_RESOURCES", os.path.dirname(os.path.abspath(__file__)))
APP_DATA_DIR    = os.environ.get("APP_DATA_DIR",  os.path.expanduser("~/.dictate"))
VENV_PYTHON     = os.path.join(os.path.expanduser("~/Documents/dictation"), "venv", "bin", "python3")
SERVER_PATH     = os.path.join(APP_RESOURCES, "server.py")
SETTINGS_PATH   = os.path.join(APP_RESOURCES, "settings_window.py")
OLLAMA_BIN      = "/opt/homebrew/bin/ollama"
BREW_BIN        = "/opt/homebrew/bin/brew"

CURRENT_VERSION = "1.4.3"
GITHUB_USER     = "mcolfax"
GITHUB_REPO     = "dictate"
GITHUB_RAW      = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main"
VERSION_URL     = f"{GITHUB_RAW}/version.txt"
UPDATE_URL      = f"{GITHUB_RAW}/update.sh"

import rumps

def is_setup_complete():
    """Check if all dependencies are installed."""
    return (
        os.path.exists(VENV_PYTHON) and
        os.path.exists(OLLAMA_BIN) and
        os.path.exists(os.path.join(APP_DATA_DIR, "config.json"))
    )

class DictateApp(rumps.App):
    def __init__(self):
        super().__init__("", quit_button=None)
        self.template       = True
        self._enabled       = False
        self._recording     = False
        self._server_proc   = None
        self._ollama_proc   = None
        self._update_version = None
        self._current_icon  = "icon_menubar.png"
        self._setup_done    = is_setup_complete()

        _icon_path  = os.path.join(APP_RESOURCES, "icon_menubar.png")
        self.icon   = _icon_path
        self.template = True

        self.toggle_item = rumps.MenuItem("Enable Dictation", callback=self.toggle_dictation)
        self.update_item = rumps.MenuItem("", callback=self.do_update)
        self.update_item.hide()

        self.menu = [
            rumps.MenuItem("Open Dictate UI", callback=self.open_ui),
            self.toggle_item,
            None,
            self.update_item,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        if not self._setup_done:
            threading.Thread(target=self._run_setup, daemon=True).start()
        else:
            threading.Thread(target=self._start_backend, daemon=True).start()
            threading.Thread(target=self._check_for_updates, daemon=True).start()
            threading.Thread(target=self._poll_state, daemon=True).start()

    # ── FIRST LAUNCH SETUP ────────────────────────────────────────────────────

    def _set_status(self, msg):
        """Show status in menu bar title during setup."""
        self.title = msg
        print(f"Setup: {msg}")

    def _run_setup(self):
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            self._set_status("Setting up…")

            # 1. Homebrew
            if not os.path.exists(BREW_BIN):
                self._set_status("Installing Homebrew…")
                subprocess.run(
                    '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                    shell=True, check=True
                )

            # 2. Ollama
            if not os.path.exists(OLLAMA_BIN):
                self._set_status("Installing Ollama…")
                subprocess.run([BREW_BIN, "install", "ollama"], check=True)

            # 3. ffmpeg
            if not shutil.which("ffmpeg"):
                self._set_status("Installing ffmpeg…")
                subprocess.run([BREW_BIN, "install", "ffmpeg"], check=True)

            # 4. Python venv
            if not os.path.exists(VENV_PYTHON):
                self._set_status("Setting up Python…")
                subprocess.run(["python3", "-m", "venv", os.path.join(APP_DATA_DIR, "venv")], check=True)
                self._set_status("Installing packages…")
                subprocess.run([
                    VENV_PYTHON, "-m", "pip", "install", "--quiet",
                    "mlx-whisper", "sounddevice", "scipy", "numpy",
                    "pynput", "flask", "rumps"
                ], check=True)

            # 5. Ollama model
            self._set_status("Downloading AI model…")
            ollama_serve = subprocess.Popen(
                [OLLAMA_BIN, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            subprocess.run([OLLAMA_BIN, "pull", "llama3.2"], check=True)
            ollama_serve.terminate()

            # 6. Write default config
            config_path = os.path.join(APP_DATA_DIR, "config.json")
            if not os.path.exists(config_path):
                with open(config_path, "w") as f:
                    json.dump({
                        "mode": "toggle",
                        "hotkey": "alt_r",
                        "hotkey_label": "Right Option (\u2325)",
                        "hotkey_type": "keyboard",
                        "whisper_model": "mlx-community/whisper-small-mlx",
                        "ollama_model": "llama3.2:latest",
                        "tone": "neutral",
                        "cleanup": True,
                        "clipboard_only": False,
                        "sound_feedback": True,
                        "pause_detection": True,
                        "pause_seconds": 2.0,
                        "vocabulary": [],
                        "app_tones": {}
                    }, f, indent=2)

            self._set_status("")  # Clear title
            self.title = ""
            self._setup_done = True

            rumps.notification(
                "Dictate is ready!",
                "Setup complete",
                "Open http://localhost:5001 to configure your hotkey and settings.",
                sound=True
            )

            # Start normally
            threading.Thread(target=self._start_backend, daemon=True).start()
            threading.Thread(target=self._check_for_updates, daemon=True).start()
            threading.Thread(target=self._poll_state, daemon=True).start()

        except Exception as e:
            self.title = ""
            rumps.alert("Setup Failed", f"Could not complete setup:\n{e}\n\nPlease run install.sh manually.")

    # ── NORMAL STARTUP ────────────────────────────────────────────────────────

    def _start_backend(self):
        try:
            urllib.request.urlopen("http://localhost:11434", timeout=1)
        except Exception:
            self._ollama_proc = subprocess.Popen(
                [OLLAMA_BIN, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(2)

        env = os.environ.copy()
        env["PATH"]         = "/opt/homebrew/bin:" + env.get("PATH", "")
        env["APP_DATA_DIR"] = APP_DATA_DIR
        self._server_proc = subprocess.Popen(
            [VENV_PYTHON, SERVER_PATH],
            cwd=APP_DATA_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        for _ in range(20):
            try:
                urllib.request.urlopen("http://127.0.0.1:5001", timeout=1)
                self._open_settings_window()
                self.template = True
                self.icon = os.path.join(APP_RESOURCES, "icon_menubar.png")
                break
            except Exception:
                time.sleep(0.5)

    # ── POLL STATE ────────────────────────────────────────────────────────────

    def _poll_state(self):
        while True:
            try:
                resp      = urllib.request.urlopen("http://127.0.0.1:5001/api/status", timeout=1)
                data      = json.loads(resp.read())
                enabled   = data.get("enabled", False)
                recording = data.get("recording", False)

                new_icon  = "icon_menubar_on.png" if recording else "icon_menubar.png"

                if new_icon != self._current_icon:
                    self._current_icon = new_icon
                    self.template = not recording
                    self.icon = os.path.join(APP_RESOURCES, new_icon)

                if enabled != self._enabled:
                    self._enabled = enabled
                    self.toggle_item.title = "Disable Dictation" if enabled else "Enable Dictation"

            except Exception:
                pass
            time.sleep(0.5)

    # ── UPDATE CHECKING ───────────────────────────────────────────────────────

    def _check_for_updates(self):
        while True:
            try:
                resp   = urllib.request.urlopen(VERSION_URL, timeout=5)
                latest = resp.read().decode().strip()
                if self._version_newer(latest, CURRENT_VERSION):
                    self._update_version = latest
                    rumps.notification(
                        "Dictate Update Available",
                        f"Version {latest} is ready",
                        "Click 'Update Available' in the menu bar to update.",
                        sound=False,
                    )
                    self.update_item.title = f"⬆️  Update to v{latest}"
                    self.update_item.show()
            except Exception:
                pass
            time.sleep(3600)

    def _version_newer(self, latest, current):
        try:
            return [int(x) for x in latest.split(".")] > [int(x) for x in current.split(".")]
        except Exception:
            return False

    def do_update(self, _):
        response = rumps.alert(
            title=f"Update to v{self._update_version}",
            message="Dictate will download the update and restart. Continue?",
            ok="Update", cancel="Cancel"
        )
        if response != 1:
            return
        try:
            files_to_update = ["server.py", "overlay.py", "settings_window.py", "app.py", "make_icons.py"]
            for fname in files_to_update:
                url  = f"{GITHUB_RAW}/{fname}"
                dest = os.path.join(APP_RESOURCES, fname)
                data = urllib.request.urlopen(url, timeout=15).read()
                with open(dest, "wb") as f:
                    f.write(data)
                print(f"✅ Updated {fname}")
                src = os.path.join(APP_RESOURCES, fname)
                dst = os.path.join(APP_DATA_DIR, fname)
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
            if self._server_proc:  self._server_proc.terminate()
            if self._ollama_proc:  self._ollama_proc.terminate()
            subprocess.Popen(["open", "/Applications/Dictate.app"])
            rumps.quit_application()
        except Exception as e:
            rumps.alert("Update Failed", f"Could not download update:\n{e}")

    # ── CONTROLS ──────────────────────────────────────────────────────────────

    def open_ui(self, _):
        self._open_settings_window()

    def _open_settings_window(self):
        subprocess.Popen([VENV_PYTHON, SETTINGS_PATH])

    def toggle_dictation(self, _):
        try:
            urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:5001/api/toggle", method="POST"),
                timeout=2
            )
        except Exception as e:
            print(f"Toggle error: {e}")

    def quit_app(self, _):
        if self._server_proc:  self._server_proc.terminate()
        if self._ollama_proc:  self._ollama_proc.terminate()
        rumps.quit_application()

if __name__ == "__main__":
    DictateApp().run()

#!/usr/bin/env python3
"""
app.py — Dictate menu bar app with auto-update checking
"""

import subprocess, sys, os, time, urllib.request, json, threading

APP_RESOURCES = os.environ.get("APP_RESOURCES", os.path.dirname(os.path.abspath(__file__)))
APP_DATA_DIR  = os.environ.get("APP_DATA_DIR",  os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON   = os.path.join(APP_DATA_DIR, "venv", "bin", "python3")
SERVER_PATH   = os.path.join(APP_RESOURCES, "server.py")
OLLAMA_BIN    = "/opt/homebrew/bin/ollama"

CURRENT_VERSION = "1.0.0"
GITHUB_USER     = "mcolfax"
GITHUB_REPO     = "dictate"
VERSION_URL     = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.txt"
UPDATE_URL      = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/update.sh"
GITHUB_RAW      = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main"

import rumps

class DictateApp(rumps.App):
    def __init__(self):
        super().__init__("", quit_button=None)
        self.icon     = os.path.join(APP_RESOURCES, "icon_menubar.png")
        self.template = False
        self._enabled        = False
        self._recording      = False
        self._server_proc    = None
        self._ollama_proc    = None
        self._update_version = None
        self._current_icon   = "icon_menubar.png"
        _icon_path = os.path.join(APP_RESOURCES, "icon_menubar.png")
        print(f"Icon path: {_icon_path}, exists: {os.path.exists(_icon_path)}")
        self.icon = _icon_path

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

        threading.Thread(target=self._start_backend, daemon=True).start()
        threading.Thread(target=self._check_for_updates, daemon=True).start()
        threading.Thread(target=self._poll_state, daemon=True).start()

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
                subprocess.run(["open", "http://127.0.0.1:5001"])
                self.template = True
                self.icon = os.path.join(APP_RESOURCES, "icon_menubar.png")
                break
            except Exception:
                time.sleep(0.5)

    def _poll_state(self):
        """Poll recording state every 0.5s — only touches icon when state changes."""
        while True:
            try:
                resp      = urllib.request.urlopen("http://127.0.0.1:5001/api/status", timeout=1)
                data      = json.loads(resp.read())
                enabled   = data.get("enabled", False)
                recording = data.get("recording", False)

                if recording:
                    new_icon = "icon_menubar_on.png"   # amber — mic hot
                elif enabled:
                    new_icon = "icon_menubar.png"       # white — enabled, idle
                else:
                    new_icon = "icon_menubar.png"       # white — disabled

                if new_icon != self._current_icon:
                    self._current_icon = new_icon
                    self.template = not recording  # amber when recording, black when idle
                    self.icon = os.path.join(APP_RESOURCES, new_icon)

                if enabled != self._enabled:
                    self._enabled = enabled
                    self.toggle_item.title = "Disable Dictation" if enabled else "Enable Dictation"

            except Exception:
                pass
            time.sleep(0.5)

    def _check_for_updates(self):
        """Check GitHub for a newer version. Runs on startup and every hour."""
        while True:
            try:
                resp    = urllib.request.urlopen(VERSION_URL, timeout=5)
                latest  = resp.read().decode().strip()
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
                pass  # Silently ignore — no internet, etc.
            time.sleep(3600)  # Check every hour

    def _version_newer(self, latest, current):
        """Compare semver strings."""
        try:
            l = [int(x) for x in latest.split(".")]
            c = [int(x) for x in current.split(".")]
            return l > c
        except Exception:
            return False

    def do_update(self, _):
        """Download and run update.sh from GitHub."""
        response = rumps.alert(
            title=f"Update to v{self._update_version}",
            message="Dictate will download the update and restart. Continue?",
            ok="Update", cancel="Cancel"
        )
        if response != 1:
            return

        try:
            # Download each file directly into the app bundle
            files_to_update = ["server.py", "app.py", "make_icons.py"]
            for fname in files_to_update:
                url  = f"{GITHUB_RAW}/{fname}"
                dest = os.path.join(APP_RESOURCES, fname)
                data = urllib.request.urlopen(url, timeout=15).read()
                with open(dest, "wb") as f:
                    f.write(data)
                print(f"✅ Updated {fname}")

            # Also update local dictation folder (only if different paths)
            import shutil
            for fname in files_to_update:
                src  = os.path.join(APP_RESOURCES, fname)
                dest = os.path.join(APP_DATA_DIR, fname)
                if os.path.abspath(src) != os.path.abspath(dest):
                    shutil.copy2(src, dest)

            if self._server_proc:
                self._server_proc.terminate()
            if self._ollama_proc:
                self._ollama_proc.terminate()

            # Relaunch app
            subprocess.Popen(["open", "/Applications/Dictate.app"])
            rumps.quit_application()
        except Exception as e:
            rumps.alert("Update Failed", f"Could not download update:\n{e}")


    def open_ui(self, _):
        subprocess.run(["open", "http://127.0.0.1:5001"])

    def toggle_dictation(self, _):
        try:
            urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:5001/api/toggle", method="POST"),
                timeout=2
            )
            # Flip state locally — no timer needed, no flicker
            self._enabled = not self._enabled
            if self._enabled:
                self.toggle_item.title = "Disable Dictation"
                self._current_icon = "icon_menubar_on.png"
            else:
                self.toggle_item.title = "Enable Dictation"
                self._current_icon = "icon_menubar.png"
            self.icon = os.path.join(APP_RESOURCES, self._current_icon)
        except Exception as e:
            print(f"Toggle error: {e}")

    def quit_app(self, _):
        if self._server_proc:  self._server_proc.terminate()
        if self._ollama_proc:  self._ollama_proc.terminate()
        rumps.quit_application()

if __name__ == "__main__":
    DictateApp().run()

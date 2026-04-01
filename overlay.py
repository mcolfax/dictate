#!/usr/bin/env python3
import json, os, socket, sys, threading
from AppKit import (NSApplication, NSBackingStoreBuffered, NSBorderlessWindowMask,
    NSColor, NSFont, NSMakeRect, NSPanel, NSTextAlignmentCenter, NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSFloatingWindowLevel, NSVisualEffectView, NSScreen, NSNonactivatingPanelMask)
from Foundation import NSObject, NSTimer
from objc import python_method

DATA_DIR    = os.environ.get("APP_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
SOCKET_PATH = os.path.join(DATA_DIR, "overlay.sock")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def load_position():
    try:
        cfg = json.load(open(CONFIG_FILE))
        return cfg.get("overlay_x"), cfg.get("overlay_y")
    except Exception:
        return None, None

def save_position(x, y):
    try:
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            cfg = json.load(open(CONFIG_FILE))
        cfg["overlay_x"] = x; cfg["overlay_y"] = y
        with open(CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)
    except Exception:
        pass

class OverlayDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self._pending_text = None
        self._lock = threading.Lock()
        saved_x, saved_y = load_position()
        w, h = 420, 44
        if saved_x is not None and saved_y is not None:
            rect = NSMakeRect(saved_x, saved_y, w, h)
        else:
            screen = NSScreen.mainScreen().frame()
            x = (screen.size.width - w) / 2
            rect = NSMakeRect(x, 100, w, h)

        self._win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSBorderlessWindowMask | NSNonactivatingPanelMask, NSBackingStoreBuffered, False)
        self._win.setLevel_(NSFloatingWindowLevel)
        self._win.setOpaque_(False)
        self._win.setAlphaValue_(0.72)
        self._win.setBackgroundColor_(NSColor.clearColor())
        self._win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)
        self._win.setMovableByWindowBackground_(True)
        self._win.setHasShadow_(True)

        vfx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        vfx.setMaterial_(1)
        vfx.setBlendingMode_(0)
        vfx.setState_(1)
        vfx.setWantsLayer_(True)
        vfx.layer().setCornerRadius_(22.0)
        vfx.layer().setMasksToBounds_(True)
        self._win.setContentView_(vfx)

        self._icon = NSTextField.alloc().initWithFrame_(NSMakeRect(14, 10, 24, 24))
        self._icon.setStringValue_("🎙")
        self._icon.setBezeled_(False); self._icon.setDrawsBackground_(False)
        self._icon.setEditable_(False); self._icon.setSelectable_(False)
        self._icon.setFont_(NSFont.systemFontOfSize_(16.0))
        vfx.addSubview_(self._icon)

        self._label = NSTextField.alloc().initWithFrame_(NSMakeRect(40, 8, w - 52, h - 14))
        self._label.setStringValue_("")
        self._label.setBezeled_(False); self._label.setDrawsBackground_(False)
        self._label.setEditable_(False); self._label.setSelectable_(False)
        self._label.setTextColor_(NSColor.labelColor())
        self._label.setFont_(NSFont.systemFontOfSize_weight_(14.0, 0.3))
        self._label.setAlignment_(NSTextAlignmentCenter)
        self._label.setLineBreakMode_(3)
        vfx.addSubview_(self._label)

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "pollUpdates:", None, True)
        threading.Thread(target=self._socket_server, daemon=True).start()

    @python_method
    def set_text(self, text):
        with self._lock: self._pending_text = text

    def pollUpdates_(self, timer):
        with self._lock:
            text = self._pending_text
            self._pending_text = None
        if text is None: return
        if text:
            self._label.setStringValue_(text)
            app = NSApplication.sharedApplication()
            app.activateIgnoringOtherApps_(True)
            self._win.orderFrontRegardless()
            app.setActivationPolicy_(1)
        else:
            self._win.orderOut_(None)
            origin = self._win.frame().origin
            threading.Thread(target=save_position, args=(origin.x, origin.y), daemon=True).start()

    @python_method
    def _socket_server(self):
        if os.path.exists(SOCKET_PATH): os.unlink(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH); server.listen(5); server.settimeout(1.0)
        while True:
            try:
                conn, _ = server.accept()
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk: break
                    data += chunk
                conn.close()
                if data:
                    msg = json.loads(data.decode("utf-8"))
                    self.set_text(msg.get("text", ""))
            except socket.timeout: continue
            except Exception as e: print(f"[overlay] socket error: {e}", file=sys.stderr)

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory from the start — no dock icon
    delegate = OverlayDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

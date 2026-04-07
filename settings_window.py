#!/usr/bin/env python3
"""
settings_window.py — Native macOS settings window for Dictate.
Hosts the existing Flask web UI in a WKWebView with a clean title-bar-integrated look.
"""
import os, sys, signal, subprocess, urllib.request
from AppKit import (NSApplication, NSBackingStoreBuffered,
    NSMakeRect, NSMakeSize, NSMakePoint, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSVisualEffectView, NSClosableWindowMask, NSTitledWindowMask,
    NSMiniaturizableWindowMask, NSResizableWindowMask)
from Foundation import NSObject, NSURL, NSURLRequest, NSTimer, NSPointInRect
from WebKit import WKWebView, WKWebViewConfiguration

SERVER_URL  = "http://127.0.0.1:5001"
LOCK_FILE   = "/tmp/dictate_settings.lock"
PREFS_FILE  = os.path.expanduser("~/.dictate/window_prefs.json")
MIN_WIDTH   = 520
MIN_HEIGHT  = 560

LOADING_HTML = """<!DOCTYPE html>
<html><head>
<meta name="color-scheme" content="light dark">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{display:flex;align-items:center;justify-content:center;height:100vh;
       background:transparent;font-family:-apple-system,sans-serif;}
  .wrap{display:flex;flex-direction:column;align-items:center;gap:20px}
  .dots{display:flex;gap:10px}
  .dot{width:9px;height:9px;border-radius:50%;background:#f59e0b;
       animation:p 1.2s ease-in-out infinite}
  .dot:nth-child(2){animation-delay:.2s}
  .dot:nth-child(3){animation-delay:.4s}
  @keyframes p{0%,100%{opacity:.2;transform:scale(.7)}50%{opacity:1;transform:scale(1)}}
  p{font-size:12px;color:#888;letter-spacing:.05em}
</style></head>
<body><div class="wrap">
  <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
  <p>Starting Dictate…</p>
</div></body></html>"""

def _already_running():
    """Return the PID of a running settings window process, or None."""
    try:
        pid = int(open(LOCK_FILE).read().strip())
        os.kill(pid, 0)  # raises OSError if process doesn't exist
        return pid
    except Exception:
        return None

def _write_lock():
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def _clear_lock():
    try: os.unlink(LOCK_FILE)
    except Exception: pass


class SettingsDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        w, h = self._restore_size()
        style = (NSTitledWindowMask | NSClosableWindowMask |
                 NSMiniaturizableWindowMask | NSResizableWindowMask)

        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        self._win.setTitle_("Dictate")
        self._win.setDelegate_(self)
        self._win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        self._win.setMinSize_(NSMakeSize(MIN_WIDTH, MIN_HEIGHT))
        self._restore_position()

        # Vibrancy background
        vfx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        vfx.setMaterial_(2)
        vfx.setBlendingMode_(0)
        vfx.setState_(1)
        vfx.setAutoresizingMask_(18)
        self._win.setContentView_(vfx)

        # WKWebView
        cfg = WKWebViewConfiguration.alloc().init()
        self._wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, w, h), cfg)
        self._wv.setAutoresizingMask_(18)
        self._wv.setValue_forKey_(True, "drawsTransparentBackground")
        self._wv.setNavigationDelegate_(self)
        vfx.addSubview_(self._wv)

        # Show loading screen immediately — window appears at once, no blank stall
        self._wv.loadHTMLString_baseURL_(LOADING_HTML, None)
        self._win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        # SIGUSR1 → raise existing window
        self._raise_requested = False
        signal.signal(signal.SIGUSR1, self._on_raise_signal)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "checkRaiseFlag:", None, True)

        self._load_attempts = 0
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "tryLoad:", None, True)

    def _restore_size(self):
        try:
            import json as _json
            prefs = _json.load(open(PREFS_FILE))
            return max(int(prefs.get("w", 780)), MIN_WIDTH), max(int(prefs.get("h", 840)), MIN_HEIGHT)
        except Exception:
            return 780, 840

    def _restore_position(self):
        try:
            import json as _json
            from AppKit import NSScreen
            prefs = _json.load(open(PREFS_FILE))
            x, y = prefs.get("x"), prefs.get("y")
            if x is not None and y is not None:
                # Verify position is still on a screen
                from Foundation import NSMakePoint
                point = NSMakePoint(x + 40, y + 40)  # check near top-left of window
                for screen in NSScreen.screens():
                    if NSPointInRect(point, screen.frame()):
                        frame = self._win.frame()
                        self._win.setFrameOrigin_(NSMakePoint(x, y))
                        return
            self._win.center()
        except Exception:
            self._win.center()

    def _save_prefs(self):
        try:
            import json as _json
            frame = self._win.frame()
            _json.dump({
                "x": frame.origin.x, "y": frame.origin.y,
                "w": frame.size.width, "h": frame.size.height,
            }, open(PREFS_FILE, "w"))
        except Exception:
            pass

    def _on_raise_signal(self, signum, frame):
        self._raise_requested = True

    def checkRaiseFlag_(self, timer):
        if self._raise_requested:
            self._raise_requested = False
            self._win.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def tryLoad_(self, timer):
        try:
            urllib.request.urlopen(SERVER_URL, timeout=0.5)
            timer.invalidate()
            url = NSURL.URLWithString_(SERVER_URL)
            req = NSURLRequest.requestWithURL_(url)
            self._wv.loadRequest_(req)
            # Start server health-check — auto-close if server dies
            self._health_failures = 0
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "checkServerAlive:", None, True)
        except Exception:
            self._load_attempts += 1
            if self._load_attempts > 50:  # 15 s — server not up, give up
                timer.invalidate()
                _clear_lock()
                NSApplication.sharedApplication().terminate_(None)

    def checkServerAlive_(self, timer):
        """Close the settings window if the Dictate server has gone away."""
        try:
            urllib.request.urlopen(SERVER_URL + "/api/status", timeout=0.8)
            self._health_failures = 0
        except Exception:
            self._health_failures = getattr(self, '_health_failures', 0) + 1
            if self._health_failures >= 3:   # ~6 s of silence → server is dead
                timer.invalidate()
                _clear_lock()
                NSApplication.sharedApplication().terminate_(None)

    def windowDidMove_(self, notification):
        self._save_prefs()

    def windowDidResize_(self, notification):
        self._save_prefs()


    def webView_didFinishNavigation_(self, wv, nav):
        js = """
        (function(){
          var s = document.createElement('style');
          s.textContent = [
            '.app { padding: 16px clamp(16px, 3vw, 40px) 24px !important;',
            '       max-width: 100% !important; }',
            '.settings-grid { grid-template-columns:',
            '  repeat(auto-fit, minmax(260px, 1fr)) !important; }',
            '.version-footer { width: 100% !important; box-sizing: border-box; }',
          ].join(' ');
          document.head.appendChild(s);
        })();
        """
        self._wv.evaluateJavaScript_completionHandler_(js, None)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, hasWindows):
        self._win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        return True

    def windowWillClose_(self, notification):
        _clear_lock()
        NSApplication.sharedApplication().terminate_(None)

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


if __name__ == "__main__":
    existing_pid = _already_running()
    if existing_pid:
        # Signal the running process to bring itself to front.
        try:
            os.kill(existing_pid, signal.SIGUSR1)
        except Exception:
            pass
        sys.exit(0)

    _write_lock()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory — no dock icon

    # Cleanly exit when the parent Dictate app quits (sends SIGTERM)
    def _on_sigterm(signum, frame):
        _clear_lock()
        NSApplication.sharedApplication().terminate_(None)
    signal.signal(signal.SIGTERM, _on_sigterm)

    delegate = SettingsDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

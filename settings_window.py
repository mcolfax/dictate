#!/usr/bin/env python3
"""
settings_window.py — Native macOS settings window for Dictate.
Hosts the existing Flask web UI in a WKWebView with a clean title-bar-integrated look.
"""
import os, sys, urllib.request
from AppKit import (NSApplication, NSBackingStoreBuffered,
    NSMakeRect, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSVisualEffectView, NSClosableWindowMask, NSTitledWindowMask,
    NSMiniaturizableWindowMask, NSResizableWindowMask)
from Foundation import NSObject, NSURL, NSURLRequest, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration
from objc import python_method

SERVER_URL = "http://127.0.0.1:5001"
LOCK_FILE  = "/tmp/dictate_settings.lock"

def _already_running():
    """Return True if another settings window process is already open."""
    try:
        pid = int(open(LOCK_FILE).read().strip())
        os.kill(pid, 0)  # Check if process exists
        return True
    except Exception:
        return False

def _write_lock():
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def _clear_lock():
    try: os.unlink(LOCK_FILE)
    except Exception: pass


class SettingsDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        w, h = 780, 840
        style = (NSTitledWindowMask | NSClosableWindowMask |
                 NSMiniaturizableWindowMask | NSResizableWindowMask)

        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        self._win.setTitle_("Dictate")
        self._win.center()
        self._win.setDelegate_(self)
        self._win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)

        # Vibrancy background
        vfx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        vfx.setMaterial_(2)   # sidebar — frosty/light frosted look
        vfx.setBlendingMode_(0)
        vfx.setState_(1)
        vfx.setAutoresizingMask_(18)
        self._win.setContentView_(vfx)

        # WKWebView fills the whole window including under titlebar
        cfg = WKWebViewConfiguration.alloc().init()
        self._wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, w, h), cfg)
        self._wv.setAutoresizingMask_(18)
        self._wv.setValue_forKey_(True, "drawsTransparentBackground")
        self._wv.setNavigationDelegate_(self)
        vfx.addSubview_(self._wv)

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "tryLoad:", None, True)

    def tryLoad_(self, timer):
        try:
            urllib.request.urlopen(SERVER_URL, timeout=0.5)
            timer.invalidate()
            url = NSURL.URLWithString_(SERVER_URL)
            req = NSURLRequest.requestWithURL_(url)
            self._wv.loadRequest_(req)
            self._win.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    def webView_didFinishNavigation_(self, wv, nav):
        js = """
        (function(){
          var s = document.createElement('style');
          s.textContent = [
            /* fluid padding, no fixed max-width cap */
            '.app { padding: 16px clamp(16px, 3vw, 40px) 24px !important;',
            '       max-width: 100% !important; }',
            /* settings grid: 1 col on narrow, 2 on medium, 3+ on wide */
            '.settings-grid { grid-template-columns:',
            '  repeat(auto-fit, minmax(260px, 1fr)) !important; }',
            /* version footer stretches full width */
            '.version-footer { width: 100% !important; box-sizing: border-box; }',
          ].join(' ');
          document.head.appendChild(s);
        })();
        """
        self._wv.evaluateJavaScript_completionHandler_(js, None)

    def windowWillClose_(self, notification):
        _clear_lock()
        NSApplication.sharedApplication().terminate_(None)

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


if __name__ == "__main__":
    if _already_running():
        # Bring existing window to front via AppleScript
        import subprocess
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to set frontmost of every process whose name is "Python" to true'],
            capture_output=True)
        sys.exit(0)

    _write_lock()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory — no dock icon
    delegate = SettingsDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

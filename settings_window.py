#!/usr/bin/env python3
"""
settings_window.py — Native macOS settings window for Dictate.
Hosts the existing Flask web UI in a WKWebView with a clean title-bar-integrated look.
"""
import os, sys, signal, subprocess, urllib.request
from AppKit import (NSApplication, NSBackingStoreBuffered,
    NSMakeRect, NSMakeSize, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSVisualEffectView, NSClosableWindowMask, NSTitledWindowMask,
    NSMiniaturizableWindowMask, NSResizableWindowMask)
from Foundation import NSObject, NSURL, NSURLRequest, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration

SERVER_URL  = "http://127.0.0.1:5001"
LOCK_FILE   = "/tmp/dictate_settings.lock"
MIN_WIDTH   = 520
MIN_HEIGHT  = 560

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
        w, h = 780, 840
        style = (NSTitledWindowMask | NSClosableWindowMask |
                 NSMiniaturizableWindowMask | NSResizableWindowMask)

        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        self._win.setTitle_("Dictate")
        self._win.center()
        self._win.setDelegate_(self)
        self._win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        self._win.setMinSize_(NSMakeSize(MIN_WIDTH, MIN_HEIGHT))

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

        # Install SIGUSR1 handler — signals the running process to raise itself.
        # Signal handlers run on Python's main thread; AppKit calls are safe here
        # because we dispatch them via a one-shot NSTimer onto the run loop.
        self._raise_requested = False
        signal.signal(signal.SIGUSR1, self._on_raise_signal)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "checkRaiseFlag:", None, True)

        self._load_attempts = 0
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "tryLoad:", None, True)

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
            self._win.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._check_mic_permission()
        except Exception:
            self._load_attempts += 1
            if self._load_attempts > 50:  # 15 s — server not up, give up
                timer.invalidate()
                _clear_lock()
                NSApplication.sharedApplication().terminate_(None)

    def _check_mic_permission(self):
        try:
            from AVFoundation import (AVCaptureDevice, AVMediaTypeAudio)
            status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
            if status == 0:  # Not determined — request
                def _handler(granted):
                    if not granted:
                        self._prompt_mic_settings()
                AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    AVMediaTypeAudio, _handler)
            elif status == 2:  # Denied
                self._prompt_mic_settings()
        except Exception:
            pass

    def _prompt_mic_settings(self):
        script = (
            'display dialog "Dictate needs Microphone access to record your voice.\\n\\n'
            'Go to System Settings → Privacy & Security → Microphone and enable Dictate." '
            'with title "Microphone Permission Required" '
            'buttons {"Open System Settings", "Later"} default button 1'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if "Open System Settings" in result.stdout:
            subprocess.Popen(["open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])

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
        # Works even for accessory apps since the process raises itself.
        try:
            os.kill(existing_pid, signal.SIGUSR1)
        except Exception:
            pass
        sys.exit(0)

    _write_lock()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory — no dock icon
    delegate = SettingsDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

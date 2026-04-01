#!/usr/bin/env python3
"""
settings_window.py — Native macOS settings window for Dictate.
Hosts the existing Flask web UI in a WKWebView panel (no browser chrome).
Launched as a subprocess by app.py when the user clicks "Open Dictate UI".
"""
import os, sys, time, urllib.request
from AppKit import (NSApplication, NSBackingStoreBuffered, NSClosableWindowMask,
    NSColor, NSMakeRect, NSResizableWindowMask, NSTitledWindowMask,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSVisualEffectView,
    NSFloatingWindowLevel)
from Foundation import NSObject, NSURL, NSURLRequest, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration
from objc import python_method

SERVER_URL = "http://127.0.0.1:5001"

class SettingsDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        w, h = 720, 620
        style = NSTitledWindowMask | NSClosableWindowMask | NSResizableWindowMask
        from AppKit import NSWindow
        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        self._win.setTitle_("Dictate Settings")
        self._win.center()
        self._win.setDelegate_(self)

        # Vibrancy background
        vfx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        vfx.setMaterial_(2)   # sidebar material
        vfx.setBlendingMode_(0)
        vfx.setState_(1)
        self._win.setContentView_(vfx)

        cfg = WKWebViewConfiguration.alloc().init()
        self._wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, w, h), cfg)
        self._wv.setAutoresizingMask_(18)  # width + height flexible
        vfx.addSubview_(self._wv)

        # Wait for server to be ready, then load
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
            pass  # Keep retrying

    def windowWillClose_(self, notification):
        NSApplication.sharedApplication().terminate_(None)

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)  # Regular — needs a window + dock icon while open
    delegate = SettingsDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()

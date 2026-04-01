#!/usr/bin/env python3
"""
make_icons.py — Generate waveform icons for Dictate app
Creates: icon_menubar.png, icon_menubar_on.png, icon_dock.png, icon.icns
"""

import os
import struct
import zlib
from pathlib import Path

APP_DIR = Path(__file__).parent

def write_png(path, width, height, pixels):
    """Write a minimal PNG file from RGBA pixel array."""
    def chunk(name, data):
        c = name + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    raw = b''
    for y in range(height):
        raw += b'\x00'  # filter type none
        for x in range(width):
            raw += bytes(pixels[y][x])

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw))
    png += chunk(b'IEND', b'')

    with open(path, 'wb') as f:
        f.write(png)


def make_pixels(width, height, bg, bars, bar_color):
    """Generate waveform bar pixels."""
    pixels = [[[0, 0, 0, 0] for _ in range(width)] for _ in range(height)]

    # Fill background
    for y in range(height):
        for x in range(width):
            pixels[y][x] = list(bg)

    cx = width // 2
    # Draw waveform bars centered
    total_bars = len(bars)
    bar_w = max(1, width // (total_bars * 2 + 1))
    gap = bar_w
    total_w = total_bars * bar_w + (total_bars - 1) * gap
    start_x = (width - total_w) // 2

    for i, bar_h_pct in enumerate(bars):
        bar_h = int(height * bar_h_pct)
        x0 = start_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        y0 = (height - bar_h) // 2
        y1 = y0 + bar_h

        for y in range(max(0, y0), min(height, y1)):
            for x in range(max(0, x0), min(width, x1)):
                pixels[y][x] = list(bar_color)

    return pixels


# Waveform pattern (height percentages per bar)
WAVEFORM = [0.25, 0.5, 0.75, 1.0, 0.75, 0.5, 0.25, 0.5, 0.75, 0.5, 0.25]

# ── Menu bar icon (22x22, transparent bg, white bars) ──
pixels = make_pixels(
    22, 22,
    bg=(0, 0, 0, 0),
    bars=WAVEFORM,
    bar_color=(255, 255, 255, 230)
)
write_png(APP_DIR / "icon_menubar.png", 22, 22, pixels)
print("✅ icon_menubar.png")

# ── Menu bar icon ON state (amber bars, static) ──
pixels = make_pixels(
    22, 22,
    bg=(0, 0, 0, 0),
    bars=WAVEFORM,
    bar_color=(245, 158, 11, 255)
)
write_png(APP_DIR / "icon_menubar_on.png", 22, 22, pixels)
print("✅ icon_menubar_on.png")

# ── Menu bar animation frames (6 frames, amber, wave cycles) ──
import math
ANIM_FRAMES = 6
for frame in range(ANIM_FRAMES):
    phase = (2 * math.pi * frame) / ANIM_FRAMES
    bars = [0.2 + 0.6 * (0.5 + 0.5 * math.sin(phase + i * 0.9)) for i in range(len(WAVEFORM))]
    pixels = make_pixels(22, 22, bg=(0,0,0,0), bars=bars, bar_color=(245, 158, 11, 255))
    write_png(APP_DIR / f"icon_menubar_anim_{frame}.png", 22, 22, pixels)
print(f"✅ icon_menubar_anim_0..{ANIM_FRAMES-1}.png")

# ── Dock icon (512x512, dark bg, amber bars) ──
DOCK_SIZE = 512
pixels = make_pixels(
    DOCK_SIZE, DOCK_SIZE,
    bg=(12, 12, 12, 255),
    bars=WAVEFORM,
    bar_color=(245, 158, 11, 255)
)

# Rounded corners mask
corner_r = 90
for y in range(DOCK_SIZE):
    for x in range(DOCK_SIZE):
        in_tl = x < corner_r and y < corner_r and (x - corner_r)**2 + (y - corner_r)**2 > corner_r**2
        in_tr = x >= DOCK_SIZE - corner_r and y < corner_r and (x - (DOCK_SIZE - corner_r))**2 + (y - corner_r)**2 > corner_r**2
        in_bl = x < corner_r and y >= DOCK_SIZE - corner_r and (x - corner_r)**2 + (y - (DOCK_SIZE - corner_r))**2 > corner_r**2
        in_br = x >= DOCK_SIZE - corner_r and y >= DOCK_SIZE - corner_r and (x - (DOCK_SIZE - corner_r))**2 + (y - (DOCK_SIZE - corner_r))**2 > corner_r**2
        if in_tl or in_tr or in_bl or in_br:
            pixels[y][x] = [0, 0, 0, 0]

write_png(APP_DIR / "icon_dock.png", DOCK_SIZE, DOCK_SIZE, pixels)
print("✅ icon_dock.png")

# ── .icns file (multi-size bundle for macOS) ──
# icns format: 'icns' header + icon family chunks
def make_icns(output_path, png_path_512):
    with open(png_path_512, 'rb') as f:
        png_data = f.read()

    # ic09 = 512x512, ic10 = 512x512@2x (we'll use same for both)
    chunks = b''
    for ostype in [b'ic09', b'ic10']:
        chunk_data = ostype + struct.pack('>I', len(png_data) + 8) + png_data
        chunks += chunk_data

    header = b'icns' + struct.pack('>I', len(chunks) + 8)
    with open(output_path, 'wb') as f:
        f.write(header + chunks)

make_icns(APP_DIR / "icon.icns", APP_DIR / "icon_dock.png")
print("✅ icon.icns")
print("\nAll icons generated!")

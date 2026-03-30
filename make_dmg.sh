#!/bin/bash
# make_dmg.sh — Build Dictate.dmg for distribution
# Run from ~/Documents/dictation after building Dictate.app

set -e
APP="/Applications/Dictate.app"
DMG_NAME="Dictate"
DMG_PATH="$HOME/Desktop/Dictate.dmg"
TMP_DIR=$(mktemp -d)

echo "🔨 Building $DMG_NAME.dmg…"

# Create temp folder with app + Applications symlink
cp -r "$APP" "$TMP_DIR/Dictate.app"
# Strip quarantine BEFORE bundling into DMG
xattr -cr "$TMP_DIR/Dictate.app"
ln -s /Applications "$TMP_DIR/Applications"

# Sign the app ad-hoc
codesign --force --deep --sign - "$TMP_DIR/dict.ai.app" 2>/dev/null || true

# Create DMG
hdiutil create \
    -volname "$DMG_NAME" \
    -srcfolder "$TMP_DIR" \
    -ov \
    -format UDZO \
    -nospotlight \
    "$DMG_PATH"

rm -rf "$TMP_DIR"

# Mount, strip quarantine from app inside, unmount
MOUNT=$(hdiutil attach "$DMG_PATH" -nobrowse -quiet | tail -1 | awk '{print $NF}')
xattr -cr "$MOUNT/Dictate.app" 2>/dev/null
hdiutil detach "$MOUNT" -quiet

echo "✅ Created: $DMG_PATH"
echo "   Share this file with users."

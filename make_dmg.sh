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
ln -s /Applications "$TMP_DIR/Applications"

# Create DMG
hdiutil create \
    -volname "$DMG_NAME" \
    -srcfolder "$TMP_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

rm -rf "$TMP_DIR"

echo "✅ Created: $DMG_PATH"
echo "   Share this file with users."

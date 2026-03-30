#!/bin/bash
# make_dmg.sh — Build dict.ai.dmg for distribution
# Run from ~/Documents/dictation after building dict.ai.app

set -e
APP="/Applications/dict.ai.app"
DMG_NAME="dict.ai"
DMG_PATH="$HOME/Desktop/dict.ai.dmg"
TMP_DIR=$(mktemp -d)

echo "🔨 Building $DMG_NAME.dmg…"

# Create temp folder with app + Applications symlink
cp -r "$APP" "$TMP_DIR/dict.ai.app"
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

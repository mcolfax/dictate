#!/bin/bash
# make_dmg.sh — Build Dictate.dmg for distribution
# Usage:
#   Ad-hoc (no Apple Developer account):
#     ./make_dmg.sh
#
#   Signed + notarized (requires Apple Developer Program membership):
#     SIGN_ID="Developer ID Application: Your Name (TEAMID)" \
#     APPLE_ID="you@example.com" \
#     TEAM_ID="YOURTEAMID" \
#     APP_PASSWORD="xxxx-xxxx-xxxx-xxxx" \
#     ./make_dmg.sh

set -e

APP="/Applications/Dictate.app"
DMG_PATH="$HOME/Desktop/Dictate.dmg"
TMP_DIR=$(mktemp -d)
VERSION=$(cat "$(dirname "$0")/version.txt" 2>/dev/null || echo "unknown")

echo "🔨 Building Dictate.dmg (v$VERSION)…"

# ── Copy app + Applications symlink ──────────────────────────────────────────
cp -r "$APP" "$TMP_DIR/Dictate.app"
ln -s /Applications "$TMP_DIR/Applications"

# ── Code sign ─────────────────────────────────────────────────────────────────
if [ -n "$SIGN_ID" ]; then
    echo "✍️  Signing with: $SIGN_ID"
    codesign --force --deep --options runtime \
        --sign "$SIGN_ID" "$TMP_DIR/Dictate.app"
else
    echo "⚠️  No SIGN_ID set — using ad-hoc signature (users must right-click → Open)"
    codesign --force --deep --sign - "$TMP_DIR/Dictate.app" 2>/dev/null || true
fi

# Strip quarantine bits
xattr -cr "$TMP_DIR/Dictate.app"

# ── Build DMG ─────────────────────────────────────────────────────────────────
hdiutil create \
    -volname "Dictate" \
    -srcfolder "$TMP_DIR" \
    -ov \
    -format UDZO \
    -nospotlight \
    "$DMG_PATH"

rm -rf "$TMP_DIR"

# ── Notarize (only if credentials supplied) ───────────────────────────────────
if [ -n "$SIGN_ID" ] && [ -n "$APPLE_ID" ] && [ -n "$TEAM_ID" ] && [ -n "$APP_PASSWORD" ]; then
    echo "📤 Submitting for notarization…"
    xcrun notarytool submit "$DMG_PATH" \
        --apple-id "$APPLE_ID" \
        --team-id  "$TEAM_ID" \
        --password "$APP_PASSWORD" \
        --wait

    echo "📎 Stapling notarization ticket…"
    # Must staple the app inside the DMG, not the DMG itself
    MOUNT=$(hdiutil attach "$DMG_PATH" -nobrowse -quiet | tail -1 | awk '{print $NF}')
    xcrun stapler staple "$MOUNT/Dictate.app"
    hdiutil detach "$MOUNT" -quiet

    # Rebuild DMG with stapled app
    TMP2=$(mktemp -d)
    hdiutil attach "$DMG_PATH" -nobrowse -quiet -mountpoint "$TMP2/vol"
    cp -r "$TMP2/vol/Dictate.app" "$TMP2/Dictate.app"
    ln -s /Applications "$TMP2/Applications"
    hdiutil detach "$TMP2/vol" -quiet
    hdiutil create \
        -volname "Dictate" \
        -srcfolder "$TMP2" \
        -ov -format UDZO -nospotlight \
        "$DMG_PATH"
    rm -rf "$TMP2"
    echo "✅ Notarized and stapled."
else
    echo "ℹ️  Skipping notarization (APPLE_ID/TEAM_ID/APP_PASSWORD not set)."
    echo "   To notarize, set those env vars and re-run. See script header for details."
fi

echo ""
echo "✅ Created: $DMG_PATH"
SHA=$(shasum -a 256 "$DMG_PATH" | awk '{print $1}')
echo "   SHA256:  $SHA"
echo "   (paste this into dictate.rb as the sha256 value)"

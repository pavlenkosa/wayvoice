#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(PYTHONPATH="$ROOT/app/src" python3 -c 'from wayvoice import __version__; print(__version__)')"
PKG="$ROOT/build/pkg"
DIST="$ROOT/dist"
OUT="$DIST/wayvoice_${VERSION}_all.deb"

rm -rf "$PKG"
mkdir -p \
  "$PKG/DEBIAN" \
  "$PKG/usr/bin" \
  "$PKG/usr/lib/wayvoice/app" \
  "$PKG/usr/lib/wayvoice" \
  "$PKG/usr/lib/systemd/user" \
  "$PKG/usr/lib/udev/rules.d" \
  "$PKG/usr/share/applications" \
  "$PKG/usr/share/metainfo" \
  "$PKG/usr/share/icons/hicolor/scalable/apps" \
  "$PKG/usr/share/icons/hicolor/symbolic/apps" \
  "$PKG/usr/share/doc/wayvoice" \
  "$DIST"

cp -a "$ROOT/app/." "$PKG/usr/lib/wayvoice/app/"
# All entry points, including setup-user, live in <prefix>/bin and locate the
# Python sources relative to themselves, so nothing in the package (or in
# wayvoice-setup.service) depends on the installation prefix.
cp "$ROOT/scripts/wayvoice" "$ROOT/scripts/wayvoice-daemon" "$ROOT/scripts/wayvoice-settings" "$ROOT/scripts/wayvoice-engine-setup" "$PKG/usr/bin/"
cp "$ROOT/scripts/setup-user" "$PKG/usr/bin/setup-user"
cp "$ROOT/systemd/"*.service "$PKG/usr/lib/systemd/user/"
cp "$ROOT/data/80-wayvoice-uinput.rules" "$PKG/usr/lib/udev/rules.d/80-wayvoice-uinput.rules"
cp "$ROOT/data/io.github.stepan.WayVoice.desktop" "$PKG/usr/share/applications/"
cp "$ROOT/data/io.github.stepan.WayVoice.metainfo.xml" "$PKG/usr/share/metainfo/"
cp "$ROOT/data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
cp "$ROOT/data/icons/hicolor/symbolic/apps/io.github.stepan.WayVoice-symbolic.svg" "$PKG/usr/share/icons/hicolor/symbolic/apps/"
cp "$ROOT/README.md" "$ROOT/CHANGELOG.md" "$ROOT/LICENSE" "$PKG/usr/share/doc/wayvoice/"
cp "$ROOT/packaging/DEBIAN/postinst" "$ROOT/packaging/DEBIAN/postrm" "$ROOT/packaging/DEBIAN/prerm" "$PKG/DEBIAN/"

cat > "$PKG/DEBIAN/control" <<EOF
Package: wayvoice
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: WayVoice Project <noreply@localhost>
Depends: python3 (>= 3.11), python3-venv, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, libadwaita-1-0, pipewire-bin, wl-clipboard, libnotify-bin
Recommends: ydotool
Description: local speech-to-text dictation for GNOME and Wayland
 WayVoice is a GTK4/libadwaita background dictation utility for GNOME on
 Wayland. It records microphone audio through PipeWire, supports multiple
 local recognition engines, normalizes punctuation and inserts recognized text
 into the currently focused application.
EOF

chmod 0755 "$PKG/DEBIAN"
chmod g-s "$PKG/DEBIAN"
chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm" "$PKG/DEBIAN/prerm"
chmod 0755 "$PKG/usr/bin/wayvoice" "$PKG/usr/bin/wayvoice-daemon" "$PKG/usr/bin/wayvoice-settings" "$PKG/usr/bin/wayvoice-engine-setup" "$PKG/usr/bin/setup-user"
find "$PKG/usr/lib/wayvoice/app" -type d -name __pycache__ -prune -exec rm -rf {} +
(cd "$PKG" && find usr -type f -print0 | sort -z | xargs -0 -r md5sum) > "$PKG/DEBIAN/md5sums"

dpkg-deb --build --root-owner-group "$PKG" "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
echo "$OUT"

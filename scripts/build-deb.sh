#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(PYTHONPATH="$ROOT/app/src" python3 -c 'from wayvoice import __version__; print(__version__)')"
PKG="$ROOT/build/pkg"
DIST="$ROOT/dist"
# The package bundles natively compiled binaries (the ydotool helper), so it can
# never be Architecture: all: ELF objects only run on the architecture they were
# built for. The build host's dpkg architecture is what the compiler targets
# here, so it is what the control file and the artifact name advertise.
DEB_ARCH="$(dpkg --print-architecture)"
OUT_NAME="wayvoice_${VERSION}_${DEB_ARCH}.deb"
OUT="$DIST/$OUT_NAME"

rm -rf "$PKG"
mkdir -p \
  "$PKG/DEBIAN" \
  "$PKG/usr/bin" \
  "$PKG/usr/lib/wayvoice/app" \
  "$PKG/usr/lib/wayvoice" \
  "$PKG/usr/lib/systemd/user" \
  "$PKG/usr/lib/udev/rules.d" \
  "$PKG/usr/share/polkit-1/actions" \
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
# The launcher for the bundled helper. It finds the copy relative to itself, so
# nothing here or in the unit names an installation prefix.
cp "$ROOT/scripts/wayvoice-ydotoold" "$PKG/usr/bin/wayvoice-ydotoold"
cp "$ROOT/systemd/"*.service "$PKG/usr/lib/systemd/user/"
# /dev/uinput is root-owned, and the paste helper needs it. The rule grants it to
# the logged-in user through uaccess, which is what a seat device is for.
cp "$ROOT/data/80-wayvoice-uinput.rules" "$PKG/usr/lib/udev/rules.d/80-wayvoice-uinput.rules"
# The bundled ydotool, unless this machine has no compiler. Not a dependency of
# anything: with no helper the application falls back to the clipboard, and says
# so. Skipped quietly rather than fatally - a machine that cannot compile C can
# still use voice dictation.
if "$ROOT/scripts/build-ydotool.sh" "$PKG/usr/lib/wayvoice/ydotool"; then
    # AGPL-6: the object code travels with its licence and a pointer to where
    # the sources are. Both are inside the package, which is the only place a
    # user who has the binary will look.
    cp "$ROOT/third_party/ydotool/LICENSE" "$PKG/usr/lib/wayvoice/ydotool/LICENSE"
    cp "$ROOT/third_party/ydotool/README.wayvoice.md" "$PKG/usr/lib/wayvoice/ydotool/README.md"
else
    echo "wayvoice: continuing without the bundled ydotool" >&2
fi
# Lets the settings UI and `wayvoice deps --install` run the package manager
# through pkexec; without it polkit would deny every install attempt.
cp "$ROOT/packaging/io.github.stepan.WayVoice.manage-deps.policy" "$PKG/usr/share/polkit-1/actions/"
cp "$ROOT/data/io.github.stepan.WayVoice.desktop" "$PKG/usr/share/applications/"
cp "$ROOT/data/io.github.stepan.WayVoice.metainfo.xml" "$PKG/usr/share/metainfo/"
cp "$ROOT/data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
cp "$ROOT/data/icons/hicolor/symbolic/apps/io.github.stepan.WayVoice-symbolic.svg" "$PKG/usr/share/icons/hicolor/symbolic/apps/"
cp "$ROOT/README.md" "$ROOT/CHANGELOG.md" "$ROOT/LICENSE" "$PKG/usr/share/doc/wayvoice/"
# Debian policy 12.5: the machine-readable copyright file belongs in the package,
# and the licence of every bundled program has to be stated in it - which it is,
# including ydotool's.
cp "$ROOT/packaging/DEBIAN/copyright" "$PKG/usr/share/doc/wayvoice/copyright"
cp "$ROOT/packaging/DEBIAN/postinst" "$ROOT/packaging/DEBIAN/postrm" "$ROOT/packaging/DEBIAN/prerm" "$PKG/DEBIAN/"

cat > "$PKG/DEBIAN/control" <<EOF
Package: wayvoice
Version: $VERSION
Section: utils
Priority: optional
Architecture: ${DEB_ARCH}
Maintainer: WayVoice Project <noreply@localhost>
License: AGPL-3.0-or-later
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
chmod 0755 "$PKG/usr/bin/wayvoice" "$PKG/usr/bin/wayvoice-daemon" "$PKG/usr/bin/wayvoice-settings" "$PKG/usr/bin/wayvoice-engine-setup" "$PKG/usr/bin/setup-user" "$PKG/usr/bin/wayvoice-ydotoold"
# md5sums is generated below from every file under usr, so the polkit policy is
# covered automatically; only its mode needs pinning (0644, world readable).
chmod 0644 "$PKG/usr/share/polkit-1/actions/io.github.stepan.WayVoice.manage-deps.policy"
find "$PKG/usr/lib/wayvoice/app" -type d -name __pycache__ -prune -exec rm -rf {} +
(cd "$PKG" && find usr -type f -print0 | sort -z | xargs -0 -r md5sum) > "$PKG/DEBIAN/md5sums"

dpkg-deb --build --root-owner-group "$PKG" "$OUT"
# The checksum file is meant for `sha256sum -c` inside dist/, so it must name
# the artifact by its basename; an absolute path would pin the file to this
# machine's directory layout and break verification everywhere else.
(
    cd "$DIST"
    sha256sum "$OUT_NAME" > "$OUT_NAME.sha256"
)
echo "$OUT"

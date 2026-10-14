#!/usr/bin/env bash
# Build in a Fedora environment with rpm-build, gcc, python3 and systemd-rpm-macros.
# Only build and dist are written; package scriptlets are never executed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 "$ROOT/scripts/check-version.py")"
RPM_VERSION="${VERSION//-/~}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || stat -c %Y "$ROOT/app/src/wayvoice/__init__.py")}"
mkdir -p "$ROOT/build"
# Every run owns its output. Reusing RPMS can silently republish an older
# Fedora variant when rebuilding the same version on another Fedora release.
TOP="$(mktemp -d "$ROOT/build/rpm.XXXXXX")"
trap 'rm -rf "$TOP"' EXIT
mkdir -p "$TOP"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS} "$ROOT/dist"
# Include current source files, but never generated artifacts or local notes.
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
    --exclude=__pycache__ --exclude='*.pyc' \
    --transform="s,^,wayvoice-$VERSION/," \
    -czf "$TOP/SOURCES/wayvoice-$VERSION.tar.gz" -C "$ROOT" \
    app scripts systemd data packaging third_party README.md CHANGELOG.md LICENSE \
    CONTRIBUTING.md SUPPORT.md SECURITY.md \
    docs/installation.md docs/getting-started.md docs/desktop-support.md \
    docs/shortcuts-and-paste.md docs/engines-and-models.md docs/troubleshooting.md \
    docs/architecture.md docs/development.md docs/releasing.md releases/TEMPLATE.md
rpmbuild -bb --define "_topdir $TOP" --define "wayvoice_version $RPM_VERSION" --define "source_version $VERSION" \
    --define "use_source_date_epoch_as_buildtime 1" --define "clamp_mtime_to_source_date_epoch 1" \
    "$ROOT/packaging/wayvoice.spec"
ARCH="$(rpm --eval '%{_arch}')"
for package in "$TOP/RPMS/$ARCH/wayvoice-$RPM_VERSION-1"*.rpm; do
    test -f "$package"
    test "$(rpm -qp --qf '%{VERSION}' "$package")" = "$RPM_VERSION"
    test "$(rpm -qp --qf '%{ARCH}' "$package")" = "$ARCH"
    cp "$package" "$ROOT/dist/"
    (cd "$ROOT/dist" && sha256sum "$(basename "$package")" > "$(basename "$package").sha256")
done

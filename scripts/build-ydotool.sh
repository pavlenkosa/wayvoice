#!/bin/sh
# Build the bundled ydotool, the program that presses the keys.
#
# It is a separate step because a compiler is not always here: the package is
# still built without it, and automatic paste falls back to the clipboard, which
# is what the application says it does when the helper is missing. The sources
# are vendored under third_party/ydotool; see the README there for why, for the
# licence, and for which upstream revision they are.
#
# Two programs, no dependencies beyond libc, so this is a compiler invocation and
# not a build system. Nothing is patched, so this script never has to know what
# upstream changed.
set -eu

ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
SRC="$ROOT/third_party/ydotool"
OUT="${1:?usage: build-ydotool.sh <output-directory>}"

if ! command -v cc >/dev/null 2>&1; then
    echo "ydotool: no C compiler found, skipping the bundled copy" >&2
    exit 1
fi
if [ ! -d "$SRC/Client" ] || [ ! -f "$SRC/Daemon/ydotoold.c" ]; then
    echo "ydotool: sources are missing at $SRC" >&2
    exit 1
fi

mkdir -p "$OUT"
# No -Wall: the sources are vendored unpatched, so their warnings are not ours
# to fix here, and four of them on every package build would drown out the ones
# from our own code. -Werror would fail the build on upstream's code, which is
# not a thing a package that vendors code should be doing either.
COMMON="-O2 -pipe -DVERSION=\"bundled\""
# shellcheck disable=SC2086  # the flags are a list, deliberately unquoted
cc $COMMON -o "$OUT/ydotoold" "$SRC/Daemon/ydotoold.c"
# shellcheck disable=SC2086
cc $COMMON -I"$SRC/Client" -o "$OUT/ydotool" \
    "$SRC/Client/ydotool.c" \
    "$SRC/Client/tool_click.c" \
    "$SRC/Client/tool_mousemove.c" \
    "$SRC/Client/tool_type.c" \
    "$SRC/Client/tool_key.c" \
    "$SRC/Client/tool_stdin.c"

# Both must run before they are put in a package: a helper that cannot start is
# worse than none, because the settings window would report auto-paste as
# available and every dictation would fall back silently.
"$OUT/ydotoold" --help >/dev/null 2>&1 || {
    echo "ydotool: the built daemon does not run" >&2
    exit 1
}
"$OUT/ydotool" --help >/dev/null 2>&1 || {
    echo "ydotool: the built client does not run" >&2
    exit 1
}

echo "ydotool: built into $OUT"

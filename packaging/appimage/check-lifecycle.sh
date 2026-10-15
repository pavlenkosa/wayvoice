#!/bin/sh
# Disposable-container control probe: never captures microphone audio.
set -eu
image=$1
version=$2
sandbox=$(mktemp -d)
export HOME="$sandbox/home"
export XDG_RUNTIME_DIR="$sandbox/runtime"
export XDG_CONFIG_HOME="$sandbox/config"
export XDG_DATA_HOME="$sandbox/data"
export XDG_STATE_HOME="$sandbox/state"
export XDG_CACHE_HOME="$sandbox/cache"
export TMPDIR="$sandbox/tmp"
export APPIMAGE_EXTRACT_AND_RUN=1
mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$TMPDIR"
chmod 700 "$XDG_RUNTIME_DIR"
outer=''
cleanup() {
    "$image" quit >/dev/null 2>&1 || :
    if [ -n "$outer" ]; then kill -TERM "$outer" 2>/dev/null || :; wait "$outer" 2>/dev/null || :; fi
    rm -rf "$sandbox"
}
trap cleanup EXIT
"$image" --daemon >"$sandbox/daemon.log" 2>&1 &
outer=$!
ready=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$image" ping >/dev/null 2>&1; then ready=true; break; fi
    echo "Waiting for portable daemon ($attempt/10)"
    sleep 1
done
if [ "$ready" != true ]; then cat "$sandbox/daemon.log"; exit 1; fi
# The first CLI runtime has exited; the independent daemon runtime stays valid.
"$image" status >"$sandbox/status.json"
grep -F "$version" "$sandbox/status.json"
"$image" quit
wait "$outer"
outer=''
if find "$sandbox" -type s -print -quit | grep -q .; then echo 'Daemon socket remained'; exit 1; fi
if find "$sandbox" -name '*.wav' -print -quit | grep -q .; then echo 'Recording remained'; exit 1; fi
trap - EXIT
rm -rf "$sandbox"
echo 'AppImage independent daemon lifetime and shutdown OK'

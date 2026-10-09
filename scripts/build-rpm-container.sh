#!/usr/bin/env bash
# Use Fedora's registry; Docker Hub anonymous pulls can fail on shared CI runners.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE=registry.fedoraproject.org/fedora:44
for attempt in 1 2 3; do
    if docker pull "$IMAGE"; then
        break
    fi
    if (( attempt == 3 )); then
        echo "Fedora image pull failed after three attempts; RPM build was not started." >&2
        exit 1
    fi
    sleep "$((attempt * 5))"
done
# Do not retry build failures: compiler and packaging errors must remain visible.
docker run --rm --pull=never -v "$ROOT:/src" -w /src "$IMAGE" sh -ec '
    dnf install -y rpm-build gcc python3 systemd-rpm-macros tar gzip
    ./scripts/build-rpm.sh
    rpm -qip dist/*.rpm
    rpm -qlp dist/*.rpm
    rpm -qp --scripts dist/*.rpm
'

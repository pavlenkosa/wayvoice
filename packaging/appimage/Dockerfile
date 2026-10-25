FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv python3-pip python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-freedesktop libgtk-4-common libglib2.0-bin libglib2.0-data \
    libgdk-pixbuf2.0-bin librsvg2-common librsvg2-bin fonts-dejavu-core \
    pipewire-bin libpipewire-0.3-modules libspa-0.2-modules wl-clipboard libnotify-bin \
    gcc libc6-dev patchelf file git ca-certificates squashfs-tools xvfb xauth dbus-x11 \
    && rm -rf /var/lib/apt/lists/*
# Docker --user does not create an NSS identity. D-Bus requires getpwuid()
# to succeed for the host UID (GitHub runners commonly use 1001).
ARG BUILD_UID=1000
ARG BUILD_GID=1000
RUN (getent group "$BUILD_GID" >/dev/null || groupadd --gid "$BUILD_GID" wayvoice-build) \
    && (getent passwd "$BUILD_UID" >/dev/null || useradd --uid "$BUILD_UID" --gid "$BUILD_GID" \
        --home-dir /tmp/wayvoice-user --no-create-home wayvoice-build)
WORKDIR /work

FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv python3-pip python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-common libglib2.0-bin libglib2.0-data \
    libgdk-pixbuf2.0-bin librsvg2-common librsvg2-bin fonts-dejavu-core \
    pipewire-bin libpipewire-0.3-modules libspa-0.2-modules wl-clipboard libnotify-bin \
    patchelf file git ca-certificates squashfs-tools xvfb xauth dbus-x11 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work

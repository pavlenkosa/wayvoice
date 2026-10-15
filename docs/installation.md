# Installation

[WayVoice](../README.md)

Download the package for your system from [GitHub Releases](https://github.com/pavlenkosa/wayvoice/releases). Release assets include SHA-256 checksums.

### Debian / Ubuntu

Install the downloaded DEB with APT so its dependencies are installed too:

```bash
sudo apt install ./wayvoice_*_amd64.deb
```

Run the command in the directory containing the downloaded package. Use the package matching your architecture, if one is provided. The application requires Python 3.11 or newer, GTK 4, libadwaita, PipeWire recording tools and `wl-clipboard`.

### Fedora

RPM builds currently target Fedora 44:

```bash
sudo dnf install ./wayvoice-*-1.fc44.x86_64.rpm
```

Installation, upgrade and removal have been checked in a disposable Fedora 44 container. A live Fedora desktop, microphone permissions and automatic paste still need verification. Other RPM distributions are not validated.

### AppImage

The experimental x86_64 `wayvoice-VERSION-x86_64.AppImage` targets Linux with glibc 2.39 or newer (Ubuntu 24.04 baseline). It includes Python 3.12, GTK 4/libadwaita, PipeWire recording tools, wl-clipboard, Faster-Whisper dependencies and the Parakeet CPU runtime. Model weights are downloaded only through explicit preparation; ordinary dictation stays offline.

```bash
chmod +x wayvoice-VERSION-x86_64.AppImage
./wayvoice-VERSION-x86_64.AppImage
```

Verify the file using the release `SHA256SUMS` first. If FUSE mounting is unavailable, use `./wayvoice-VERSION-x86_64.AppImage --appimage-extract-and-run`. No package installation or privileged host setup is performed. Automatic paste still requires suitable host permissions/tools; clipboard fallback remains available.

Keep the file at a stable path. A shortcut command is `"/absolute/path/WayVoice.AppImage" toggle`; update the shortcut if you move the file. Quit any already-running WayVoice before switching between native and portable builds: they share normal configuration, model cache and daemon socket. This is portable packaging, not a sandbox or isolated test session.

Update manually: finish dictation, quit WayVoice and its daemon (`./wayvoice-VERSION-x86_64.AppImage quit`), then replace the executable with the newer verified file. The DEB/RPM updater never installs packages from an AppImage. Portable daemon/setup processes launch the image separately so closing the interface does not remove their runtime files. Live GNOME/Wayland and FUSE lifecycle acceptance remains open.

Build without installing the application with `python3 scripts/build-appimage.py` from a clean committed checkout. Docker builds a Ubuntu 24.04 toolchain in an isolated container, constructs an explicit AppDir, checks runtime imports/resources, packs with pinned official AppImage tools, extracts the actual artifact to a relocated path and inspects it. Output and checksum appear under `dist/`; unique verification workspaces remain under `build/`.

Flatpak is no longer produced from 0.8.0. Existing older installations retain their Flatpak-specific safety guards; migration is manual.

## Updating

You can enable **Automatically check for updates** in Settings and save the change.
WayVoice then checks GitHub when you open the interface and every six hours while
it remains open. A notice on Home shows a newer release with an **Update…** button.
Automatic checks are off by default and do not download or install packages.

Install a newer DEB or RPM using the same package-manager command as above. Native package upgrades restart the background services.

On builds that include the updater, choose **Check for updates…** from the menu, or run:

```bash
wayvoice update --check
wayvoice update --install
```

The updater downloads a matching DEB or RPM from the latest stable GitHub release, checks its SHA-256 and package metadata, and opens the system package manager with administrator authorization. If that release has no package for your architecture, it reports an error. Finish dictation and model preparation first. New recordings are blocked during installation; the services restart and the daemon's version is checked afterwards. Starting with 0.6.11, updates run inside the app and restart the interface automatically. Updating from 0.6.10 still opens a terminal and requires reopening Settings once.

The updater requires a compatible running daemon. Older installations need a manual package upgrade first. It does not update source checkouts, AppImage or legacy Flatpak installations, and it does not automatically roll back failed package transactions. Release checksums verify integrity against the published release; they are not independent package signatures. The update trust boundary includes the project’s GitHub account, repository access, release workflow and published assets, as well as HTTPS delivery. An attacker who can replace both the package and its release checksum can pass this integrity check. Package name, version and architecture checks prevent mismatched installations; they do not authenticate the publisher independently.

Alpha/beta GitHub releases are installed manually. The native updater follows the stable release feed; it does not install prereleases.

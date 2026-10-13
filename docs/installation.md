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

### Flatpak

Experimental `wayvoice-VERSION-x86_64.flatpak` bundles are built for GitHub Releases. WayVoice is not currently published on Flathub. Installed sandbox dictation still requires user acceptance testing.

The bundle contains WayVoice and its Faster-Whisper runtime, but GNOME Platform 50 is a separate dependency. With Flatpak and the Flathub remote configured:

```bash
flatpak install --user flathub org.gnome.Platform//50
flatpak install --user ./wayvoice-VERSION-x86_64.flatpak
flatpak run io.github.stepan.WayVoice
```

Replace `VERSION` with the downloaded release version. Verify the bundle against the release `SHA256SUMS` before installation. Models are prepared explicitly in Settings; ordinary dictation does not download them.

Update manually by downloading and installing the newer bundle with the same `flatpak install --user ./...flatpak` command. These bundles do not configure a WayVoice update remote, and the native package updater is unavailable in Flatpak.

To build without installing the application, use `python3 scripts/build-flatpak.py` from a clean tracked checkout with GNOME SDK/Platform 50, `flatpak-builder` and `ostree` available. The script retains a unique workspace under `build/`, inspects the exported bundle, and writes it with a checksum under `dist/`.

Flatpak builds use direct background processes instead of systemd user services. Configure the shortcut in your desktop and paste manually from the clipboard; native shortcut registration and the bundled paste helper are not supported in the sandbox.

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

The updater requires a compatible running daemon. Older installations need a manual package upgrade first. It does not update source checkouts or Flatpak installations, and it does not automatically roll back failed package transactions. Release checksums verify integrity against the published release; they are not independent package signatures. The update trust boundary includes the project’s GitHub account, repository access, release workflow and published assets, as well as HTTPS delivery. An attacker who can replace both the package and its release checksum can pass this integrity check. Package name, version and architecture checks prevent mismatched installations; they do not authenticate the publisher independently.

Alpha/beta GitHub releases are installed manually. The native updater follows the stable release feed; it does not install prereleases.

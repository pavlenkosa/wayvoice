# Desktop support

[WayVoice](../README.md)

WayVoice is Linux-first, with GTK 4/libadwaita and PipeWire capture. GNOME/Wayland is the primary tested desktop. KDE Plasma shortcut integration exists, but complete installed dictation/paste acceptance remains open. wlroots desktops are not validated.

Native DEB/RPM installations support systemd user services and direct-process fallback when no user manager answers. Flatpak uses direct processes. Fedora 44 package lifecycle has been checked in a disposable container; this does not verify a live Fedora desktop. Other RPM distributions are unvalidated.

Xvfb/X11 GTK tests check widgets and callbacks. They do not verify Wayland focus, desktop shortcuts, physical microphone capture, clipboard lifetime, virtual keyboard permissions or insertion into another application.

For each desktop/package combination, manual acceptance should cover short/long speech, silence, cancellation, repeated dictation, selected microphone loss, editor/browser/terminal insertion, clipboard fallback and shutdown cleanup. Record exact commit, package, desktop/session, engine/model/device, result and diagnostic reference. Do not automatically collect audio or dictated text. Unavailable checks remain **NOT TESTED**, not successful.

Global shortcuts and installed package upgrades require separate checks from isolated source acceptance. No universal support or speed threshold is claimed.

See [shortcut setup](shortcuts-and-paste.md), [installation](installation.md) and [support](../SUPPORT.md).

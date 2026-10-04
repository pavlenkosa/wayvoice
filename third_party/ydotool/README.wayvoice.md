# ydotool, bundled

The tool that presses the keys. WayVoice records and recognises on its own, but
the last step — sending `Ctrl+V` to whichever window you were typing in — needs
something with access to `/dev/uinput`. On Wayland there is no supported way to
do that from a client, and `ydotool` is the program that does it.

Vendored here rather than asked for, because it is not in every distribution's
repositories. Debian 13 has no `ydotool` package at all — not in `main`, not in
`contrib` — so on that system the automatic paste could not be installed by the
user, and asking them to build a C program is not an answer a settings window can
give. It is in `trixie-backports` and in Ubuntu's `universe`, and on those systems
the distribution's own copy is used and this one is never started: see
"Which copy runs" below.

## What is here

The upstream sources, unmodified:

| Path | What |
|---|---|
| `Client/` | the `ydotool` command: key, type, click, mousemove |
| `Daemon/ydotoold.c` | the daemon that owns `/dev/uinput` |
| `LICENSE` | AGPL-3.0-or-later, as upstream |
| `CMakeLists.txt.upstream` | upstream's build file, kept for reference |

Upstream: <https://github.com/ReimuNotMoe/ydotool>, revision `708e96f` (2025-12-22),
which is version 1.0.4. About 1600 lines of C with no dependencies beyond libc —
the 1.0 rewrite dropped libevdev, which is why a prebuilt copy is portable at all.

WayVoice is licensed under AGPL-3.0-or-later for exactly this reason; the two
licences are the same one, and `GPL-3.0-only` would have made this combination
impossible.

## How it is built

`scripts/build-deb.sh` compiles both programs with `cc` into
`/usr/lib/wayvoice/ydotool/`, not into `/usr/bin`: a copy in `PATH` would sit
next to a distribution package of the same name, and two `ydotoold` processes
fighting over `/dev/uinput` is a bad afternoon for whoever has to debug it.

If no compiler is present the package is still built, without the bundled copy,
and says so. Nothing depends on it: automatic paste degrades to the clipboard.

## Which copy runs

`wayvoice-ydotool.service` starts `ydotoold` from `PATH` if the distribution
provides one, and only falls back to the bundled copy when it does not. So:

- a system with a `ydotool` package runs that one, updated by its own security
  fixes, and the bundled copy is inert;
- a system without one runs the bundled copy, which is why automatic paste works
  out of the box there.

The same order is used when WayVoice looks for the `ydotool` *client* command,
and when the settings window decides whether the dependency is missing.

## What this is not

Not in the Flatpak. A daemon that writes to `/dev/uinput` has to run on the
host, and inside a sandbox `/dev/uinput` is not mapped — the manifest documents
that `--device=all` buys nothing for this reason. The Flatpak build therefore
needs `ydotool` installed on the host, exactly as the daemon does.

## Updating

Replace the files from a newer upstream revision, keep `LICENSE`, and record the
revision above. No patches are applied to these sources, so a diff against
upstream should be empty; if that stops being true, say so in this file.
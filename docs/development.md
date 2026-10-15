# Development

[WayVoice](../README.md)

Bug reports, fixes, translations and desktop testing are welcome. For a bug report, include the version, package type, desktop, session type and recognition engine. For paste problems, say whether the text reached the clipboard. Remove private dictation text from logs and reports.

Clone the repository and run the checks with Python 3.11 or newer:

```bash
git clone https://github.com/pavlenkosa/wayvoice.git
cd wayvoice
make lint
make test
```

GTK tests require GTK 4/libadwaita bindings and a display. Tests that cannot run without those dependencies are skipped; unit tests do not verify microphone capture or insertion into real applications.

Build native packages with:

```bash
make deb  # Debian packaging tools and a C compiler required
make rpm  # Fedora with rpm-build and systemd-rpm-macros
make appimage  # Clean committed tree and Docker required
```

Or build the RPM in a Fedora container with `./scripts/build-rpm-container.sh`. Build output goes to `dist/`; the scripts do not install packages or start host services.

Keep pull requests focused. Add user-facing strings to all interface catalogues and preserve their format parameters. For larger changes, open an issue first. See [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).

Development happens in `develop`; open focused `feature/*` or `fix/*` pull requests. `main` holds integrated, checked code. A version change alone never publishes a release. See [release process](../docs/releasing.md).

## Testing builds from develop

Successful pushes to `develop` (or a manual CI run on `develop`) produce a `wayvoice-testing-COMMIT-ATTEMPT` artifact in GitHub Actions. It contains DEB, RPM and inspected x86_64 AppImage packages, individual checksums, `SHA256SUMS` and `BUILD_INFO.json` with the source commit and workflow URL. Download it from the successful CI run's **Artifacts** section; artifacts expire after 14 days. All normal test, GTK, quality and manifest jobs must pass before the testing bundle is built.

Package filenames include `testing` and the short commit SHA. The installed version and application identity remain unchanged: installing a testing build replaces the same application and uses its settings. These are development snapshots, not stable releases or an isolated test session. They are not published to GitHub Releases or distributed by the native updater. Verify the extracted files with `sha256sum --check SHA256SUMS` before installation.

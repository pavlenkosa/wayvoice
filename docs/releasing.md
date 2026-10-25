# Release process

[WayVoice](../README.md)

Development and publication are separate decisions. A commit or version bump on `main` never publishes. Only pushing an existing `vMAJOR.MINOR.PATCH` tag starts the release workflow. Package versions currently remain numeric; alpha/beta status comes from release policy. Suffix versions are rejected until package-manager ordering and updater comparison have been implemented and tested. The current policy is beta, including numeric versions: new releases are marked Pre-release and are not promoted as Latest. Do not change old published versions or tags.

## Branches and review

- `develop`: everyday integration.
- `feature/*` and `fix/*`: focused work, normally targeting `develop`.
- `main`: checked integration through a pull request; required CI must pass before merge.
- `release/vX.Y.Z`: temporary stabilization branch only for a larger release.
- `hotfix/*`: focused repair of a published version, reviewed and brought back to `develop`.

No permanent third stable branch is needed. Commit related fixes together into one meaningful release. A post-tag change requires a new version.

## Repository settings required before publication

Repository administrators must configure these on GitHub; workflow files alone do not enable them:

1. Protect `main`: require pull requests, successful `Test & build native packages`, `GTK UI tests`, `AppImage recipe assertions` and `Static analysis & coverage` checks; block force pushes and deletion. Add review requirements appropriate to the available maintainers.
2. Create environment `release`, restrict deployments to `v*` tags, and configure required reviewers. Prevent self-review when another maintainer can review; a solo maintainer must explicitly choose their approval policy rather than assume independent approval exists.
3. After verifying these controls, set repository Actions variable `RELEASE_ENVIRONMENT_READY=true`. Publication fails without this acknowledgement. The variable does not itself enforce protection: keep environment rules enabled.
4. Protect release tags against mutation/deletion. Consider enabling GitHub Immutable Releases for future releases. Draft-first publication attaches all assets before immutability applies; do not retrofit or replace old assets.
5. Enable private vulnerability reporting; Discussions are optional once there is a community to support.

Suggested GitHub About: “Private, offline voice typing for Linux and Wayland.” Website: the repository README. Suggested topics: `linux`, `wayland`, `speech-to-text`, `dictation`, `gtk4`, `libadwaita`, `whisper`. These are administrator actions, not claims that settings have been applied.

## Prepare one candidate

Update the version consistently, add the technical CHANGELOG entry, and write `releases/VERSION.md` from the [template](../releases/TEMPLATE.md). Include actual highlights, compatibility, downloads and limitations. Keep alpha/beta/stable status consistent with `.github/release-policy.json`, package metadata and the public README.

Open a PR into `main`. Check Python/GTK/static analysis, package builds and metadata. Inspect actual artifacts and record available installed DEB/RPM/AppImage acceptance: installation, upgrade, uninstall, dictation, cancellation, clipboard/insertion and cleanup. Check interrupted updates and recovery; the native updater does **not** promise automatic rollback. A rollback test must use a deliberate package-manager recovery procedure in a disposable environment. Missing desktop/package tests stay NOT TESTED.

GTK tests under Xvfb/X11 are not Wayland E2E. Do not declare release readiness solely from unit-test counts. A release build comes from a clean committed tree, with no internal documents or secrets.

## Tag, build, approve, publish

After review and an explicit release decision, an authorized maintainer creates and pushes the version tag at the checked `main` commit. No script creates or moves it automatically. Validation requires the tag, checkout and workflow SHA to match, the commit to be reachable from `main`, version consistency, and substantive notes without placeholders.

The read-only build job runs checks and builds DEB/RPM/AppImage and source archive from the tagged commit. AppImage failure blocks release. One artifact contains packages, checksums and the exact notes. The separate publish job waits for the `release` environment, verifies the remote tag and checksums, then creates a draft with all assets before publishing. Only this job has `contents: write`.

Existing releases are never overwritten. If upload fails, inspect the draft and failed run; do not silently delete or replace it. Re-running build is safe; resolving a partially created draft requires an explicit maintainer decision. Remote CI and repository environment configuration must be verified before the next real tag.

## Integrity and future supply-chain work

SHA256 checks integrity against the release; it is not independent publisher authentication. Trust includes the GitHub account, repository, workflow and HTTPS distribution. See [update trust](installation.md#updating).

Signatures, independent keys, SBOM generation, dependency audits and build provenance are follow-up work until actually implemented and verified. Immutable release attestations require the corresponding GitHub setting. Do not claim they exist based on this document.

The native updater follows stable releases only. Alpha/beta releases and AppImage files are installed manually. A future “What's new” UI should reuse curated public notes and respect the app's localization; it is a separate product slice, not implemented by this repository change.

# Changelog

## 0.7.0-beta.2 - 2026-11-01

Fixes from testing the public beta with an empty library.

- Full-library checkboxes, **IN SET**, and pack counts use the same effective membership. Removing a build starts a custom set while retaining the other builds.
- M64 metadata refresh downloads checksum-checked files for the games in your library without host Git or a full metadata checkout. Source labels reflect actual cached metadata; whole-library refresh retires files removed upstream.
- System Files import errors appear inside the open sheet. Archive import errors name the original file, and failed batches leave the library unchanged.
- Guides match the import, image-reuse, and card-review controls. Native imports support game files and ZIP archives; extract 7z archives separately.

The download is Developer ID signed, notarized, and stapled. Clean external-Mac acceptance and physical-card tests remain open. The broad first-user walkthrough used `0.7.0-beta.1`; this release receives separate focused checks.

## 0.7.0-beta.1 - 2026-11-01

First public Mac beta. Requires an Apple Silicon Mac with macOS 14 or later.

- Native pixel interface for the M64 with EverDrive-64 PRO and Chromatic with EverDrive-GB X7.
- Collection browsing, exact-build selection, metadata refresh, ROM verification, and recoverable duplicate cleanup.
- Themed packs as folders, ZIPs, or compact FAT32 images. Direct card installation uses the full card capacity.
- Verified backups before replacement, game and save-source previews, whole save-bundle history, and one-write earlier-save choices.
- Exact-hash recipes that share collection choices without ROMs, saves, or local paths.
- Separately chosen library folders and a pinned bundled Python runtime.
- Official system-file import and staged-updater status. Vendor firmware and menu OS files remain user-supplied.
- GPL-3.0-only source, original documentation graphics, and an OFL N64 theme font atlas.

The Mac download is Developer ID signed, notarized, and stapled. Automated and virtual-card checks passed. Clean external-Mac acceptance and physical tests of this rebuilt beta remain open, including card recovery, save and clock persistence, native contention, and the public N64 font atlas. See the [beta checklist](docs/beta-testing.md).

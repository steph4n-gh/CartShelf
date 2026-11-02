# CartShelf

CartShelf is a native Mac app for building game collections and preparing flashcart SD cards. Its pixel interface keeps the library browser, pack builder, and card station in one window.

This beta targets two device pairs:

| Console | Cartridge |
| --- | --- |
| ModRetro M64 | EverDrive-64 PRO |
| ModRetro Chromatic | EverDrive-GB X7 |

Release version, minimum macOS version, and build number come from [release.json](release.json). This source tree contains application code, reviewed theme assets, and synthetic tests. Bring your own ROMs and cartridge system files. Store your library outside the source checkout and the app bundle.

## Use the app

The packaged beta targets Apple Silicon Macs. Copy CartShelf.app out of the download archive into Applications or another normal mounted drive before opening it.

1. Open CartShelf and choose **CONNECT LIBRARY** to open an existing library, or create a new library folder. Choose a folder outside Git repositories, source archives, and the application bundle.
2. Import your game files and select exact builds in **LIBRARY**.
3. Open **PACKS** to build a folder, ZIP, or compact FAT32 image.
4. In **CARD**, choose a pack and the inserted card. Review the game changes, save sources, backup destination, and device identity.
5. Start the replacement and keep the card connected until CartShelf reports safe ejection.

CartShelf verifies a backup before formatting. Direct card installation uses the card's full capacity. Matching saves come from the current card or verified save history. An explicitly selected earlier save applies only to the reviewed write. Unknown or conflicting save associations remain in the backup with an explanation.

Use **SAVE HISTORY** to inspect, verify, and export a save bundle. **USE IN NEXT WRITE** chooses a verified earlier bundle. Clock and SRAM files stay together. The Card tab can clear earlier-save choices before review.

Use **EXPORT RECIPE** to share a collection definition. **OPEN RECIPE** checks exact identities against your library before importing a set. Recipes contain titles, versions, hashes, and notes. They contain no game files, save bytes, or local file paths.

The Card tab supports friendly card names and prior verified operation details. Names are advisory. The live device identity and capacity still control replacement. An EFU file on a card is an available firmware updater. It does not establish the firmware currently installed on the cartridge.

See [backup and recovery](docs/backup-and-recovery.md), [system files](docs/system-files.md), and the [beta test checklist](docs/beta-testing.md).

## Build from source

Build on macOS with Python 3.13 or later and the Xcode command-line tools. The distributed app uses its own pinned Python runtime. End users do not need Homebrew or Python installed separately.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/python tools/check_publication.py
.venv/bin/python app/build.py
```

The output is `dist/CartShelf.app`. The default local build has an ad-hoc signature. A release download needs its own signing and notarization checks. [Build and release instructions](docs/build-and-release.md) explain the available commands and the distinction.

To exercise the CLI, first create a separate empty folder outside the checkout. Pass its absolute path explicitly.

```sh
.venv/bin/python library_manager.py --root /path/to/your-library init
.venv/bin/python library_manager.py --root /path/to/your-library scan
```

Tests create synthetic ROM headers, save bytes, and metadata in temporary folders. These fixtures check program behavior and are not playable games. Tests do not require a personal game collection or download vendor firmware.

## Beta qualification

Software checks, virtual-card checks, and physical console acceptance are separate results. An earlier private build passed on-device game, existing-save, and new-save power-cycle checks for both target pairs. That history does not qualify a rebuilt beta or the replacement public N64 font atlas. Record each beta's physical tests separately using [the beta checklist](docs/beta-testing.md).

Public source checks inspect the working tree, staged files, all reachable Git history, and source archives. The packaged app has a separate resource and runtime check. Run these before sharing a repository or release archive.

## Contribute

Read [CONTRIBUTING.md](CONTRIBUTING.md) before adding a device or changing save or card behavior. Report suspected security problems using [SECURITY.md](SECURITY.md). Do not attach ROMs, private saves, or unredacted library reports to issues.

CartShelf source is GPL-3.0-only. Fonts retain their OFL license. [Third-party notices](THIRD_PARTY_NOTICES.md) describe the theme tooling and packaged runtime dependencies. Cartridge names identify compatibility targets. CartShelf is an independent project.

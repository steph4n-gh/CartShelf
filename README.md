![CartShelf pixel branding](docs/images/cartshelf-banner.svg)

# CartShelf

A native Mac app for your ModRetro game collection. Browse exact ROM builds, make themed packs, and prepare EverDrive SD cards with verified backups and save history.

**[Download the Mac beta](https://github.com/steph4n-gh/CartShelf/releases/tag/v0.7.0-beta.1)** · [Get started](docs/getting-started.md) · [Report a bug](https://github.com/steph4n-gh/CartShelf/issues/new/choose)

[![Source and Mac candidate checks](https://github.com/steph4n-gh/CartShelf/actions/workflows/checks.yml/badge.svg)](https://github.com/steph4n-gh/CartShelf/actions/workflows/checks.yml)

**macOS 14 or later. Apple Silicon.** The packaged beta includes its Python runtime. You do not need Homebrew or a separate Python installation.

![CartShelf pack builder with two fictional M64 game builds and a verified image](docs/images/pack-builder.jpg)

The screenshots use generated, nonplayable ROM and save fixtures. They contain no personal game collection or save data.

## Two device pairs

| Console | Cartridge |
| --- | --- |
| ModRetro M64 | EverDrive-64 PRO |
| ModRetro Chromatic | EverDrive-GB X7 |

Bring your own game files and obtain cartridge system files from the manufacturer. The repository and app contain no ROM collection, save files, or vendor firmware.

## What you can do

- Browse game versions, refresh metadata, verify ROMs, and review duplicate files before recoverable cleanup.
- Build themed folders, ZIPs, or compact FAT32 images. Direct card installation uses the card's full capacity.
- Review games added, retained, and removed before replacing a card. Check each save's source and the backup destination in the same review.
- Keep matching progress through pack changes. Verify or export an earlier save bundle, then choose it for one reviewed write. SRAM and clock files stay together.
- Share pack recipes containing titles, versions, and exact content hashes. ROMs, saves, and local paths stay out of recipes.

[See the save timeline](docs/images/save-timeline.jpg). [See the card review layout](docs/images/review-card.jpg). The card review image is a disabled display fixture using the production view. It demonstrates layout, not a physical write.

## Start with your own library

1. Download the beta ZIP and move `CartShelf.app` to Applications or another normal mounted drive.
2. Open CartShelf and create or connect a writable library folder outside the app and source checkout.
3. Import your games and use **SYSTEM FILES** to verify the manufacturer's supported files.
4. Select exact builds in **LIBRARY**, then open **PACKS** to build an image.
5. Follow [the first card-write guide](docs/getting-started.md#write-a-spare-card) with a spare card and a verified backup.

Your library folder holds games, downloaded metadata, system files, packs, backups, and browsing preferences. It can live on an external drive. CartShelf reconnects to that folder when you reopen the app.

## Beta status

Version `0.7.0-beta.1` is Developer ID signed and notarized. The candidate passed 389 automated tests and ten virtual card operations. See the [release notes](https://github.com/steph4n-gh/CartShelf/releases/tag/v0.7.0-beta.1) for qualification details.

Clean external-Mac testing and physical SD-card tests of this rebuilt beta remain open. Those checks include card recovery, clock and save persistence, native card-operation contention, and the public N64 font atlas. Earlier private-build hardware results do not qualify this beta. Use [the beta checklist](docs/beta-testing.md) to record a result.

## Build from source

Build on macOS with Python 3.13 or later and the Xcode command-line tools.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/python tools/check_publication.py
.venv/bin/python app/build.py
```

The output is `dist/CartShelf.app`. A default local build has an ad-hoc signature. [Build and release instructions](docs/build-and-release.md) cover distribution signing, notarization, and source checks.

## Read more or contribute

- [Back up and recover a card](docs/backup-and-recovery.md).
- [Prepare cartridge system files](docs/system-files.md).
- [Understand the code and data boundaries](docs/architecture.md).
- [Read the changelog](CHANGELOG.md).
- [Contribute a change](CONTRIBUTING.md) and follow the [community guidelines](CODE_OF_CONDUCT.md).
- [Report a security problem privately](SECURITY.md).

CartShelf source is GPL-3.0-only. Fonts retain their OFL license. See [LICENSE](LICENSE), [third-party notices](THIRD_PARTY_NOTICES.md), and [image provenance](docs/images/README.md).

CartShelf is an independent project. Console and cartridge names identify compatibility targets.

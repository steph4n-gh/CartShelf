# Third-party notices

CartShelf source is licensed under GNU GPL version 3 only. The full text is in [LICENSE](LICENSE). Fonts and runtime dependencies retain the licenses described below. No cartridge firmware, commercial ROM, user save, preservation database, or downloaded game artwork is included in this source distribution.

## Silkscreen fonts

`app/Resources/Silkscreen-Regular.ttf`, `Silkscreen-Bold.ttf`, and the theme font source are from [The Silkscreen Project](https://github.com/googlefonts/silkscreen). They are licensed under SIL Open Font License 1.1. Keep their `OFL.txt` notices with redistributed font files.

The original CartShelf N64 menu atlas is generated from that OFL font and original control glyphs. It replaces the historical vendor bitmap atlas. See [the theme asset notice](themes/CartShelf-64/LICENSE-ASSETS.md) and its exact asset manifest. The new atlas and theme need their own physical-device qualification.

## EverDrive-GB palette tooling

The fixed CartShelf-GB patches were generated using orangeglo's [EverDrive GB Theme Editor](https://github.com/orangeglo/everdrive-gb-editor), pinned at commit `be10b0397974f174df58dcf71defb8f0530236bd`. The retained applier and patches are GPL-3.0-only. The original editor is credited even though its JavaScript application is not bundled here.

The patches change four fixed two-byte palette words in an exact stock OS 1.06 copy. Vendor OS bytes are not part of this source tree or app bundle. Users acquire system files separately from the manufacturer.

## Packaged Python runtime

The app builder obtains CPython from [Astral's python-build-standalone project](https://github.com/astral-sh/python-build-standalone). Exact architecture-specific URLs, lengths, versions, and SHA-256 pins are in [app/runtime-lock.json](app/runtime-lock.json).

CPython retains its Python Software Foundation license and third-party notices. Runtime dependencies such as OpenSSL retain their own licenses. Exact upstream notices and their archive provenance are in [app/runtime-licenses](app/runtime-licenses). The package copies these notices into the runtime, where its content manifest covers them. Review the actual packaged inventory before distributing a candidate.

Pillow 13 identifies its license as MIT-CMU. Its retained wheel license also includes notices for bundled native dependencies. PyYAML is MIT licensed. Their exact packaged versions come from `app/runtime-lock.json`, and their wheel license files stay in the runtime. A version pin does not replace its license text.

Optional 7-Zip support uses an external installation. This source distribution does not include a 7-Zip executable. Do not advertise support for nested 7-Zip imports on a clean Mac unless the required capability is available and tested.

# Prepare cartridge system files

Get cartridge system files from the manufacturer. Open **SYSTEM FILES** in CartShelf, select the target device, and import the downloaded BIN, EFU, or supported ZIP. CartShelf checks supported stock identities before caching them in your selected library. The source repository and app bundle do not include vendor firmware or menu OS binaries.

## EverDrive-64 PRO

Use the [official PRO-series support directory](https://krikzz.com/pub/support/everdrive-64/pro-series/). The library caches EFU files under `cache/firmware`. Packs can carry the cached updater in `Firmware`.

Copying an EFU file does not flash the cartridge. Run the manufacturer's update procedure on the cartridge when needed. CartShelf reports the file included in a pack and leaves the installed cartridge version unknown without a supported device query.

## EverDrive-GB X7

Use the [official X-series OS directory](https://krikzz.com/pub/support/everdrive-gb/x-series/OS/). The CartShelf palette patch targets exact stock OS 1.06 bytes. Unknown OS revisions are rejected rather than patched at guessed offsets.

The library's verified stock OS cache uses `cache/firmware/gb-x7/GBCSYS/GBCOS.BIN` and its source receipt. The cache remains stock. Pack preparation applies the fixed palette patch to an independent copy.

Game-only folder exports are distinct from bootable full-card replacements. Read the pack's system-file status before using it to replace a card. Do not erase a working system layout to install an incomplete pack.

For X7 migration, finish saving, return to its menu, and launch another title before powering off. This is a conservative precaution. The public manufacturer information used during development does not establish the exact SRAM-to-SD flush timing.

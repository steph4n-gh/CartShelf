# Test a beta candidate

Use spare media and a backed-up collection. Record the app version, architecture, cartridge revision, system-file versions, and reader. Keep personal paths and save bytes out of public results.

## Check a fresh installation

1. Start the app on a Mac without Homebrew or a separately installed Python.
2. Create a library outside the app and source checkout.
3. Import a redistributable homebrew game that you have permission to use.
4. Select a game and check that selection feedback appears before artwork completes.
5. Build a pack and inspect its manifest and system-file status.
6. Quit, reopen, and check the selected library and browsing state.
7. Disconnect the library volume and check that CartShelf requests reconnection rather than creating another library.

## Check the new workflows

- Export a recipe, inspect it, and import the exact matches as a new set.
- Open a recipe containing a missing exact build. Confirm that another revision is not substituted.
- Open save history, verify a bundle, and export all members together.
- Choose an earlier save for the next write, then clear that choice before review.
- Name a card and check its last verified pack and backup details.
- Check that staged firmware is distinguished from installed cartridge firmware.
- Review added, retained, and removed games and each save source before replacement.

## Check a physical card operation

1. Capture an independent verified backup of the spare card's initial files and saves.
2. Write pack A through the app and wait for safe ejection.
3. Boot the console and check the theme, games, and existing progress.
4. Make a new save, power off fully, and check it on the next boot.
5. Write pack B, then return to pack A. Check the archived progress again.
6. On Chromatic, check a game with a real-time clock and confirm that SRAM and clock restore together.
7. Restore the initial backup and compare its files and saves before console testing.

A card-write pass does not imply a backup-restore pass. Record them separately. Native UI, isolated runtime, virtual FAT32, and console tests also remain separate qualifications.

## Report a result

Include what you selected, the expected behavior, the observed behavior, and the stage where it differed. Include sanitized receipt fields and app version. Do not attach ROMs, save bundles, entire card images, library catalogs, or full unredacted logs.

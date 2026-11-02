# Back up and recover a card

Finish saving in your game, allow card activity to complete, and power off before removing the SD card. CartShelf preserves bytes already written to SD.

## Choose a backup

Use **CARD FILES** for routine pack changes. It records game files, saves, system files, settings, custom hidden files, and empty directories. It excludes only actual card-root macOS `.Spotlight-V100`, `.fseventsd`, and `.Trashes` directories. The receipt lists exclusions present on that card. Unreadable user data stops the operation before formatting.

Use **WHOLE CARD IMAGE** for a sector copy. It includes unused capacity and macOS metadata, so its size follows the card rather than the files stored on it.

Keep verified backups on a separate volume from the SD card. CartShelf protects the physical disks holding the selected library, running application, and startup system.

## Replace a pack

1. Select a prepared image and an eligible removable card.
2. Review games added, retained, and removed.
3. Review saves from the current card, history, or an explicit earlier-save choice. Check the reasons for saves retained only in backup.
4. Check the live device ID, capacity, backup method, and destination.
5. Start the replacement and keep the card connected.
6. Remove the card only after safe ejection is reported.

The image stays compact when exported. A direct pack installation creates a full-capacity FAT32 filesystem. A reused filename does not make two ROM revisions interchangeable. Save matching uses exact content identity, with N64 byte-order normalization.

## Restore a verified backup

1. Open **RESTORE A CARTSHELF BACKUP** in the Card tab.
2. Select the verified `.files` folder or whole-card image.
3. Select the destination card and review the recovery details.
4. Continue through the backup-first operation.
5. Test the recovered card in the console, including a new save after power-off.

Folder recovery restores the recorded supported files and directories onto a full-capacity filesystem. Whole-card recovery restores the original sectors and partition size. Its destination must hold the original image's exact byte count.

Older complete file archives containing protected macOS root metadata remain inspectable, but automatic file recovery refuses unsupported layouts before writing. Use an appropriate whole-card image when that metadata must be preserved.

## Recover after an interrupted operation

If formatting has begun, keep the verified pre-write backup and the failed operation receipt. Safely eject the card before recovery. Open the backup through the restore picker and review a fresh recovery plan. A partial or unverified backup is not a restore source.

A readback-verified write and a successful console boot answer different questions. Test existing progress and a new save across full power-off after recovery.

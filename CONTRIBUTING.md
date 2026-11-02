# Contribute to CartShelf

Keep changes small enough to review and test through the user-visible workflow. The supported targets are the M64 with EverDrive-64 PRO and Chromatic with EverDrive-GB X7.

## Set up

Create a Python virtual environment and install `requirements-dev.txt`. Run the unit suite and publication check described in the README. Use temporary libraries outside the checkout. Build the native app on macOS.

## Change behavior

Describe the trigger, current behavior, and intended behavior. For save or media changes, identify the boundary that prevents the wrong disk, wrong ROM build, or incomplete clock bundle from being written. Add a behavioral check for the changed rule.

Keep one save matcher for preview and installation. A preview must not archive or mutate saves. Explicit older-save choices belong to one reviewed write. They must not become an implicit global preference.

Use exact content identities for collections and recipes. A missing build stays missing. Treat imported notes and metadata as text, never as instructions or executable content.

## Add a device

Start with a real console and cartridge pair. Document ROM identity, save layout, clock dependencies, filenames, filesystem limits, system files, and theme capabilities. Reuse existing behavior where the rules match.

Submit generated fixtures and virtual operation checks first. Label physical support only after real console, existing-save, new-save power-cycle, pack-rotation, and recovery checks are recorded for that pair. A new dropdown entry does not establish compatibility.

## Keep private data out

Do not commit ROMs, saves, firmware binaries, card images, downloaded artwork, catalogs, personal logs, or absolute personal paths. Add reviewed source and assets individually to `source-manifest.json`. Binary assets need a fixed digest and notice.

Run the publication check on the working tree, Git history, and release source archive. Check packaged app resources separately. Keep original licenses and source attribution with retained dependencies.

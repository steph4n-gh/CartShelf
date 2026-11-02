# Build and prepare a Mac release

Build on macOS with the Xcode command-line tools and Python 3.13 or later. Install host dependencies with `requirements-dev.txt`. Runtime downloads are pinned by architecture in `app/runtime-lock.json` and checked before packaging.

## Run source checks

```sh
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/python tools/check_publication.py
```

The publication check uses `source-manifest.json`. New source files need an explicit entry. Binary assets need their exact digest and a notice. Review a file before adding it. Do not regenerate the allowlist by recursively approving every file in a library folder.

Keep all data roots outside the checkout. Build output stays under ignored `build` and `dist` directories. Never initialize Git around a personal library.

## Build a local candidate

```sh
.venv/bin/python app/build.py
.venv/bin/python tools/check_publication.py --app dist/CartShelf.app
```

The default candidate is ad-hoc signed. It is useful for local verification and does not establish Developer ID signing, notarization, or a fresh-Mac acceptance result.

The app includes a pinned Python runtime, its dependencies, trusted core modules, original theme assets, and notices. The selected library contains imported games, downloaded metadata and firmware, collections, backups, saves, and operation logs. The app never executes Python code from that library.

## Sign a distribution candidate

Pass an existing Developer ID Application identity explicitly.

```sh
.venv/bin/python app/build.py --identity 'Developer ID Application: Your Name (TEAMID)'
```

To submit that signed candidate with an existing notarytool profile, also pass `--notary-profile PROFILE`. The build's adjacent notary receipt records the submission outcome. A release manifest saying `not-submitted` is not a notarization receipt. Check the submitted artifact's signature, staple, Gatekeeper assessment, and fresh-Mac behavior before release.

A QA-only default library requires a distinct bundle ID and `--library-root`. Never distribute that build. The public publication check rejects embedded local-library defaults.

## Check the source archive

Stage and review only allowlisted source. Check every reachable commit before sharing the repository.

```sh
.venv/bin/python tools/check_publication.py --require-clean
git archive --format=tar --output=build/CartShelf-source.tar HEAD
.venv/bin/python tools/check_publication.py --require-clean --source-archive build/CartShelf-source.tar
```

Create source archives from Git. A recursive archive of the working directory can include ignored private files or downloads.

Package the checked app without Finder metadata, then inspect the archive too.

```sh
ditto --norsrc --noextattr -c -k --keepParent dist/CartShelf.app build/CartShelf-mac.zip
.venv/bin/python tools/check_publication.py --require-clean --app-archive build/CartShelf-mac.zip
```

## Record release evidence

Record the exact source commit, app digest, build and runtime manifests, signing identity, notarization receipt, source and app publication results, and beta-test results. Keep personal evidence in the library. Publish a sanitized summary with the candidate.

Use `release.json` as the version source. Keep earlier qualification receipts historical. A rebuilt app, changed theme, new cartridge, or changed storage implementation needs its own relevant checks.

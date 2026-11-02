# Report a security problem

Use [GitHub's private vulnerability report](https://github.com/steph4n-gh/CartShelf/security/advisories/new) for suspected security flaws. Report ordinary bugs through [the issue forms](https://github.com/steph4n-gh/CartShelf/issues/new/choose).

Include the affected CartShelf version, macOS version, target device, minimal reproduction, expected boundary, and observed impact. Use generated files and disposable media. Describe a disk by its role and capacity rather than a personal path or serial number.

Keep ROMs, saves, firmware, card images, credentials, personal paths, and unredacted library reports out of reports. A small synthetic example is more useful than a full collection. Do not publish a working exploit against the disk writer in a public issue.

Review focuses on removable-disk admission, reviewed-plan binding, backup verification before erase, exact-build save matching, archive containment, immutable save history, trusted bundled code, and separation of public source from user data.

The project does not promise a response deadline. The current beta is the starting point for reproduction. Include the exact older version if the problem occurs elsewhere.

---
title: Open-source licenses
description: Which licenses cover teaport, where their texts are on a device, and where the source of the LGPL libraries is.
---

teaport is licensed under the MIT license. The brain package also contains third-party Python packages, each under its own license, and the engine has license terms of its own. This page says where each license text is.

<!-- source: LICENSE:1-3#34fc7ea6; THIRD_PARTY_LICENSES.md:3-6#b9e3b72d -->

## teaport's license

The teaport repository, including the brain and the plugin, is under the MIT license, copyright 2026 Teaspoon AI. The `LICENSE` file at the root of the repository has the text, and the brain package installs it in its `copyright` file.

<!-- source: LICENSE:1-3#34fc7ea6; THIRD_PARTY_LICENSES.md:3-3#4f6dcb4d; packaging/nfpm.yaml:22#7924db66; packaging/deb/copyright:6-10#a558a392 -->

## License files on the device

The brain package installs these files. The table lists where each one is and what it holds.

| Location | What it holds |
| --- | --- |
| `/usr/share/doc/teaport-brain/copyright` | The text of teaport's MIT license, and a note that the Python packages in the brain keep their own licenses. |
| `/usr/share/doc/teaport-brain/THIRD_PARTY_LICENSES.md` | The brain's third-party components, their licenses, and the notes on the three LGPL libraries. |
| `teaport_brain/static/OFL-dm-sans.txt` and `OFL-archivo.txt`, in the brain package under `/opt/teaport/brain/lib/python3.12/site-packages/` | The SIL Open Font License 1.1 and copyright lines of the two fonts, DM Sans and Archivo, that the configuration and licenses pages use. The package carries the font files themselves beside them, so the pages load nothing from Google. |
| The `.dist-info` directory of each Python package, under `/opt/teaport/brain/lib/python3.12/site-packages/` | That package's own license files. |
| `THIRD_PARTY_NOTICES.txt` in the install root, `/opt/teaport` by default | The attributions for the components the engine is built from. The installer downloads it with the engine. |

<!-- source: packaging/nfpm.yaml:69-74,119-133#f61e6e53; packaging/deb/copyright:6-36#cbeddc17; THIRD_PARTY_LICENSES.md:81-86#6d99e3e2; install.sh:121,889,1291#12015c61; packaging/nfpm.yaml:69-74,119-134#873f7aec; brain/pyproject.toml:119-120#e59aebe7; brain/teaport_brain/licenses.py:22-32#c3bc2c75; packaging/build-deb.sh:38#636d2d54; THIRD_PARTY_LICENSES.md:121-139#490bc890; brain/teaport_brain/config_ui.py:483-498#baf6f752; brain/teaport_brain/static/licenses.html:13#2e66ca6d -->

## See the licenses on the device

The configuration page has an Open-source licenses link at the bottom. It opens a page with the notices the package installs, including the two font licenses, and the Python packages in the brain with each one's version and license. Select a package to read its license text. The page is `/settings/licenses` on the Settings site and `http://127.0.0.1:7861/licenses` on the device itself. Like the configuration page, it loads its data with the Settings session or the token.

<!-- source: brain/teaport_brain/static/config.html:389#8731c6bc; brain/teaport_brain/config_ui.py:501-512#526a7abf; brain/teaport_brain/gateway_server.py:710#db147ffa; brain/teaport_brain/config_ui.py:102-118#6e4abba8; brain/teaport_brain/licenses.py:26-32#71f1da32 -->

`teaport licenses` shows the same on the command line. See the [teaport licenses](/reference/cli/#teaport-licenses) reference.

## The engine's license

The engine is a separate component with its own license terms. The installer shows the engine license and asks you to accept it by typing `accept`. It then asks who accepts it: your email address, and whether the engine is for your personal use or under a Teaspoon AI license, whose ID it asks for.

The installer records your acceptance with Teaspoon AI, together with the license version, the exact text you accepted, the installer's version, your device's IP address and a hash of its machine ID. The engine and its models download only after that. The installer also keeps a record in `eula-accepted.json` in the state directory, `/var/lib/teaport` by default, readable only by root. It asks again when the license version changes, when that record is missing, and when Teaspoon AI no longer has the acceptance on record, for example 7 years after it was made.

To accept without the prompt, name the version you read: `--accept-eula=<version>`, with the version the license's title names, and give `--email`, plus `--license-id` for licensed use. A bare `--accept-eula`, or another version, stops the installer.

<!-- source: THIRD_PARTY_LICENSES.md:3-5#1e314cfb; install.sh:123,150-160,418-450,524-639#6e62d947; worker/src/index.js:258-264,310-315#290d30f9; install.sh:491-502#bffd8d4a -->

## The LGPL libraries

Three libraries in the brain package are under the GNU Lesser General Public License (LGPL): soxr, which resamples audio, and libsndfile, which reads and writes audio files, both 2.1 or later, and num2words, which converts numbers to words, version 2.1. The package contains all three unmodified. soxr links libsoxr into its Python extension, libsndfile is a shared object in the soundfile package, and num2words is plain Python.

<!-- source: THIRD_PARTY_LICENSES.md:58-80,99-101#7d35d2ad -->

You can replace any of them. The source of the exact versions in the brain package is in the source distributions on PyPI and in the upstream projects: [soxr](https://github.com/dofuuz/python-soxr), [libsndfile](https://github.com/libsndfile/libsndfile), [soundfile](https://github.com/bastibe/python-soundfile), and [num2words](https://github.com/savoirfairelinux/num2words). Replacing a file in `/opt/teaport/brain` shows up in `teaport doctor` as a changed file, and the next install of the package restores the shipped one. The [third-party licenses file](https://github.com/Teaspoon-AI/teaport/blob/main/THIRD_PARTY_LICENSES.md) in the repository describes how to replace each library and how to keep a replacement.

<!-- source: THIRD_PARTY_LICENSES.md:87-106#5f3ab273 -->

## What's next

- [Installer reference](/reference/installer/)
- [Update teaport](/guides/updating/)

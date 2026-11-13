---
title: Open-source licenses
description: Which licenses cover teaport, where their texts are on a device, and where the source of the two LGPL libraries is.
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
| `/usr/share/doc/teaport-brain/THIRD_PARTY_LICENSES.md` | The brain's third-party components, their licenses, and the notes on the two LGPL libraries. |
| The `.dist-info` directory of each Python package, under `/opt/teaport/brain/lib/python3.12/site-packages/` | That package's own license files. |
| `THIRD_PARTY_NOTICES.txt` in the install root, `/opt/teaport` by default | The attributions for the components the engine is built from. The installer downloads it with the engine. |

<!-- source: packaging/nfpm.yaml:61-66,84-100#85615cdf; packaging/deb/copyright:6-33#5f81607f; THIRD_PARTY_LICENSES.md:77-80#079adf2d; install.sh:82,521,844#228177a9 -->

## The engine's license

The engine is a separate component with its own license terms. The installer shows the engine license and asks you to accept it. `--accept-eula`, or `TEAPORT_ACCEPT_EULA=1`, accepts it without the prompt. The installer records the accepted version in `eula-accepted.json` in the state directory, `/var/lib/teaport` by default, and asks again only when the version changes.

<!-- source: THIRD_PARTY_LICENSES.md:3-5#1e314cfb; install.sh:84,104,217-219,225#1d85658a -->

## The LGPL libraries

Two libraries in the brain package are under the GNU Lesser General Public License (LGPL) 2.1 or later: soxr, which resamples audio, and libsndfile, which reads and writes audio files. The package contains both unmodified. soxr links libsoxr into its Python extension, and libsndfile is a shared object in the soundfile package.

<!-- source: THIRD_PARTY_LICENSES.md:56-75,88-90#193badec -->

You can replace either library. The source of the exact versions in the brain package is in the source distributions on PyPI and in the upstream projects: [soxr](https://github.com/dofuuz/python-soxr), [libsndfile](https://github.com/libsndfile/libsndfile), and [soundfile](https://github.com/bastibe/python-soundfile). Replacing a file in `/opt/teaport/brain` shows up in `teaport doctor` as a changed file, and the next install of the package restores the shipped one. The [third-party licenses file](https://github.com/Teaspoon-AI/teaport/blob/main/THIRD_PARTY_LICENSES.md) in the repository describes how to replace each library and how to keep a replacement.

<!-- source: THIRD_PARTY_LICENSES.md:81-99#c0cedd6f -->

## What's next

- [Installer reference](/reference/installer/)
- [Update teaport](/guides/updating/)

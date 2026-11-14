---
title: Update teaport
description: Check an installed device against its release manifest, update the brain or the engine on its own, and roll either one back.
---

The installer applies every update. The `teaport update --check` command compares the device with the release manifest and names the parts that differ; it changes nothing. The brain and the engine also have narrower paths: `install.sh --only brain` installs the brain package alone, and `install.sh --only engine` updates the engine alone. `install.sh --rollback brain` and `install.sh --rollback engine` put the previous one back.

<!-- source: cli/teaport:467-483#1c777d04; install.sh:9-13#3bf0e543 -->

## Before you begin

- An SSH connection to the device: every command on this page runs on it.
- A device with teaport installed. `teaport update --check` reads `/opt/teaport/manifest.json`, the manifest the installer wrote, and exits with an error if the file is missing.

<!-- source: brain/teaport_brain/updates.py:55-66,336-337#c715a888; install.sh:859,3461#841be564 -->

## Check for updates

To see which parts of the installation differ from the release manifest:

1. Sign in to the device over SSH.
2. Run the check:

   ```bash
   teaport update --check
   ```

   The command fetches a fresh copy of the release manifest, compares it with the stored one, and prints a line for the version and for each tracked part: the engine release, the brain tag, the plugin version, the OpenClaw version, and the model files. `->` marks a part that differs; `==` marks one that matches. The check ends with `up to date.` when nothing differs, or with a `changed:` list. It changes nothing on the device.

<!-- source: brain/teaport_brain/updates.py:50,294-307#b34be85f -->

## Apply an update

Run the comparison without `--check` to see the parts that changed and the command that applies them:

```bash
teaport update
```

The command prints the same comparison and, when a part differs, names the run to make: `install.sh --only brain` for the brain, `install.sh --only engine` for an engine release, and a full `install.sh` run for the rest. The installer does the update; the command does not.

<!-- source: cli/teaport:467-483#1c777d04 -->

### Update only the brain

The brain is a package, `teaport-brain`, that the project builds in CI. Nothing is built on the device. The package holds the brain's Python environment (`/opt/teaport/brain`), the `teaport` command (`/usr/bin/teaport`), and the helper that the configuration page runs as root.

<!-- source: install.sh:1160-1163#cee553a8; packaging/nfpm.yaml:57-85#bb01814d -->

From a checkout of the teaport repository, update the source and install the package the release manifest names:

```bash
git pull && ./install.sh --only brain
```

On a device without a checkout, append the flag to the one-line install:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only brain
```

To install a package file that you have, for example a build from a pull request, name it in `TEAPORT_BRAIN_DEB`:

```bash
TEAPORT_BRAIN_DEB=/path/to/teaport-brain_VERSION_arm64.deb ./install.sh --only brain
```

<!-- source: install.sh:3,17-19#108120c1; install.sh:1208-1266#2a8829bc -->

The run downloads the package and checks its sha256, keeps a copy in `/var/cache/teaport`, and installs it with `apt-get`. It renders the units that run brain code (`teaport-brain`, plus the audio bridge and the Wi-Fi setup when the device has them), restarts `teaport-brain`, and checks that the brain comes back. If the brain does not come back healthy, the run installs the package that was installed before and fails. If `apt-get` cannot install the package at all, for example because another package manager holds the lock for more than two minutes, the run stops with the device as it was and restarts nothing.

<!-- source: install.sh:17-23#03f10c8d; install.sh:1368-1444#13642914 -->

The `teaport-brain` unit waits for the brain to report that it is serving, so a restart fails when the new brain never gets ready. The swap restarts the local audio bridge with the brain when the `teaport-local-audio` service is active; the brain's health alone decides whether the swap stays.

<!-- source: systemd/teaport-brain.service.in:12-17#78e8f1b4; install.sh:1368-1444#13642914 -->

A Talk session in progress ends at the restart. A phone call stays connected at the gateway, and the brain rejoins it with a short notice that it lost the connection.

<!-- source: install.sh:1149#38ebb95b; brain/teaport_brain/sip_server.py:45-52#9213415f -->

Nothing else changes: no engine download, no license prompt, no system packages beyond the package's own dependencies, and no edit to `/etc/teaport`. A brain change that needs a new setting there ships in a full run instead.

<!-- source: install.sh:25-27#d7143d85 -->

On a device that still runs the retired `teaport-sip-brain` unit, the run stops that unit before the swap and removes it after; the SIP gateway keeps serving, and the new brain connects to it. The unit's drop-in tuning moves into `/etc/teaport/brain.env`.

<!-- source: install.sh:28-30#a64f5eaf -->

### Update only the engine

To install a new engine release without a full run, run the installer with `--only engine`. From a checkout of the teaport repository:

```bash
git pull && ./install.sh --only engine
```

On a device without a checkout, append the flag to the one-line install:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only engine
```

<!-- source: install.sh:3,12,37-38#5088e1dc -->

The engine comes from the release manifest, and the `teaport-engine` unit comes from the checkout. The run downloads only the files whose sha256 differs from the manifest, out of the engine's binary, its pronunciation dictionary, its part-of-speech tagger, `THIRD_PARTY_NOTICES.txt`, and its two models. It downloads them beside the live files, so the engine keeps serving, and checks them as a full run does. If a download is interrupted, the next run continues it where it stopped.

<!-- source: install.sh:37-41,486-491,522,530-544,846-847#2a1a01bd -->

Then the run keeps a copy of the live engine for a rollback, moves the new files in, renders `teaport-engine.service`, and restarts the engine. The brain reconnects to it. The run checks that the engine listens on its port within three minutes, stays up, and loaded its pronunciation dictionary. If the engine does not come back healthy, the run puts back the previous engine with its models and its unit, removes the keys it added to `engine.env`, and fails.

<!-- source: install.sh:42-45,889,897,908,974-992#671772e7 -->

If the run stops while it moves the new files in, it puts the previous engine back. If a power cut stops it there, the next run of the installer does that first. The installer runs one at a time: a second run started while one is running stops right away with `another install.sh is running on this box`.

<!-- source: install.sh:148-172,619-624,647-680#82ebe49d -->

The engine takes about a minute to load its models after a restart, and a Talk session or a phone call in progress loses the engine. When the engine's files, its unit, and `engine.env` are already current and the engine is running, the run checks the engine and doesn't restart it.

<!-- source: install.sh:49,923,966-972#7a387a56; brain/teaport_brain/config_schema.toml:66#80379fdf -->

`/etc/teaport/engine.env` gains only the keys it lacks. A value that is already in the file stays, so a `KOKORO_RESERVE_FPT` that you tuned survives the update. A full run writes the installer's values over it.

<!-- source: install.sh:46-47,2211,2225,2595-2596#1d268b76 -->

The run asks you to accept the engine license only when its version differs from the one you accepted. It installs no system packages: it checks that `espeak-ng`, `libopenblas.so.0`, and `libcudnn.so.9` are present, and stops when one is missing. Nothing else changes: no brain build and no other unit.

<!-- source: install.sh:47-48,3440-3446#d2f3ce52 -->

If a drop-in changes the engine's unit, for example one that a manual deployment left in `/run`, the run names it and leaves it in place. Remove it if you don't need it, and then run `sudo systemctl daemon-reload`.

<!-- source: install.sh:933-942#c43a4851 -->

### Update everything

Run the installer with no component flag:

```bash
./install.sh
```

The full run repeats the installation and downloads only the artifacts that differ. It installs the brain package the way the brain-only run does, and after every unit and env file is written, it restarts the engine and the brain in one step and checks that they come back. A full run has no automatic rollback; roll the brain or the engine back by hand afterward.

<!-- source: install.sh:5,31-33#69faf65a; install.sh:50-52#c6d99e84; cli/teaport:482#06b7c6ce -->

## Roll back the brain

To put the previous brain package back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback brain
   ```

The run installs the newest other package kept in `/var/cache/teaport`, restarts the brain, and checks it comes back. The `teaport` command and the configuration helper come with the package, so they go back together with the brain. The package that the run replaces stays kept, so running the rollback again undoes it.

<!-- source: install.sh:3400-3420#a43d31fd -->

The installer accepts two components, `brain` and `engine`, and `--only` and `--rollback` do not combine in one run.

<!-- source: install.sh:115-118#ffc7d7a8 -->

The brain-only update rolls back on its own when the brain fails its health check. A full run does not; use the rollback by hand there.

<!-- source: install.sh:22-23,31-33#741a2838 -->

## Roll back the engine

To put the previous engine back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback engine
   ```

The run takes the newest engine kept in `engine.rollback/` in the install root and makes it the live engine again, with the unit it ran under and any model it kept. It keeps the live engine there first, moves the kept files back, restarts the engine, and checks that it comes back. To undo the rollback, run it again.

<!-- source: install.sh:13,50-51,687-706,3467-3506#069be2b9 -->

The engine-only update rolls back on its own when the engine fails its check. A rollback doesn't: if the engine doesn't come back, the run stops and says so. A full run doesn't roll back either; use the rollback by hand there.

<!-- source: install.sh:45,51-52,851,3469-3470,3502#a9536167 -->

## Choose a release channel

A channel is a manifest. The repository ships two: `stable.json`, the default, and `beta.json`, which has the same shape and placeholder values until the first beta release is cut.

<!-- source: manifest/stable.json:2-4#1b522f83; manifest/beta.json:2-4#00ca757c -->

Both the installer and `teaport update --check` read the URL or local path in `TEAPORT_MANIFEST_URL`, `https://get.teaspoon.tech/manifest/stable.json` by default. To follow the beta channel, name the beta manifest:

```bash
TEAPORT_MANIFEST_URL=https://get.teaspoon.tech/manifest/beta.json ./install.sh
```

<!-- source: install.sh:56,81#0f9477e2; brain/teaport_brain/updates.py:83-89#e5101603 -->

The installer takes its channel from that variable alone. `teaport update --check` also takes `--channel stable` or `--channel beta` for one run. The brain checks every day too: set `TEAPORT_UPDATE_CHANNEL=beta` in `/etc/teaport/brain.env` to make `beta` the channel it and the command look at, and `TEAPORT_UPDATE_CHECK_SECS=0` to turn the daily check off. The brain sees `TEAPORT_MANIFEST_URL` only when it is in `/etc/teaport/brain.env`; exported in a shell, it changes the command and not the daily check.

<!-- source: install.sh:101-111#ef93f814; brain/teaport_brain/updates.py:74-89,183-196#03e7c3dd -->

## Retained releases

The installer keeps the newest three brain packages in `/var/cache/teaport`, plus the installed one, and removes the rest after each install. A rollback goes back to the newest other package in that directory.

<!-- source: install.sh:1178-1179#5997041c; install.sh:1277-1286#4f210a86 -->

A run that changes the engine's files first keeps the engine they replace in `engine.rollback/` in the install root: its files, the unit it ran under, its entry from the installed manifest, and any model the run replaces. Each copy is numbered, so the newest copy is the one with the highest number, whatever the device's clock says. The installer keeps the newest two and removes the rest. A run that changes no engine file keeps nothing. A copy that holds a model takes that model's disk space until the installer removes the copy; the speech-to-text model is about 4.4 GB.

<!-- source: install.sh:492-499,506-512,520,582-593,718-722#e163601c -->

## What's next

- [Configure teaport](/guides/configuration/)
- [Troubleshooting](/guides/troubleshooting/)
- [Make phone calls](/guides/phone-calls/)

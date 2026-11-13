---
title: Update teaport
description: Check an installed device against its release manifest, update the brain or the engine on its own, and roll either one back.
---

The installer applies every update. The `teaport update --check` command compares the device with the release manifest and names the parts that differ; it changes nothing. The brain and the engine also have narrower paths: `install.sh --only brain` rebuilds the brain alone, and `install.sh --only engine` updates the engine alone. `install.sh --rollback brain` and `install.sh --rollback engine` put the previous one back.

<!-- source: cli/teaport:464-483#137a0282; install.sh:9-13#e917cb0e -->

## Before you begin

- An SSH connection to the device: every command on this page runs on it.
- A device with teaport installed. `teaport update --check` reads `/opt/teaport/manifest.json`, the manifest the installer wrote, and exits with an error if the file is missing.

<!-- source: brain/teaport_brain/updates.py:55-66,336-337#c715a888; install.sh:863,3595#841be564 -->

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

<!-- source: cli/teaport:468-483#4fbac22b -->

### Update only the brain

From a checkout of the teaport repository, update the source and rebuild the brain from it:

```bash
git pull && ./install.sh --only brain
```

On a device without a checkout, append the flag to the one-line install:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only brain
```

<!-- source: install.sh:3,17-19#774ff01d -->

The run builds a new Python environment from `brain/uv.lock` beside the live one, so the brain keeps serving while it builds. It checks the new environment, swaps it in together with the `teaport` command and the configuration helper built in it, restarts `teaport-brain`, and checks that the brain comes back. If the brain does not come back healthy, the run puts the previous environment back by itself.

<!-- source: install.sh:19-23#191a38f6 -->

The swap restarts the local audio bridge with the brain when the `teaport-local-audio` service is active; the brain's health alone decides whether the swap stays.

<!-- source: install.sh:1320-1322,1338-1340#ab020d70 -->

A Talk session in progress ends at the restart. A phone call stays connected at the gateway, and the brain rejoins it with a short notice that it lost the connection.

<!-- source: install.sh:1398#38ebb95b; brain/teaport_brain/sip_server.py:45-52#9213415f -->

Nothing else changes: no engine download, no license prompt, no systemd unit or `/etc/teaport` edit. A brain change that needs a new unit or a new setting there ships in a full run instead.

<!-- source: install.sh:23-25#483a9f4f; install.sh:29-30#67547537 -->

On a device that still runs the retired `teaport-sip-brain` unit, the run stops that unit before the swap and removes it after; the SIP gateway keeps serving, and the new brain connects to it. The unit's drop-in tuning moves into `/etc/teaport/brain.env`.

<!-- source: install.sh:26-28#06ec7b3c -->

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

<!-- source: install.sh:37-41,490-495,526,534-548,850-851#2a1a01bd -->

Then the run keeps a copy of the live engine for a rollback, moves the new files in, renders `teaport-engine.service`, and restarts the engine. The brain reconnects to it. The run checks that the engine listens on its port within three minutes, stays up, and loaded its pronunciation dictionary. If the engine does not come back healthy, the run puts back the previous engine with its models and its unit, removes the keys it added to `engine.env`, and fails.

<!-- source: install.sh:42-45,893,901,912,978-996#671772e7 -->

If the run stops while it moves the new files in, it puts the previous engine back. If a power cut stops it there, the next run of the installer does that first. The installer runs one at a time: a second run started while one is running stops right away with `another install.sh is running on this box`.

<!-- source: install.sh:148-172,623-628,651-684#82ebe49d -->

The engine takes about a minute to load its models after a restart, and a Talk session or a phone call in progress loses the engine. When the engine's files, its unit, and `engine.env` are already current and the engine is running, the run checks the engine and doesn't restart it.

<!-- source: install.sh:49,927,970-976#7a387a56; brain/teaport_brain/config_schema.toml:66#80379fdf -->

`/etc/teaport/engine.env` gains only the keys it lacks. A value that is already in the file stays, so a `KOKORO_RESERVE_FPT` that you tuned survives the update. A full run writes the installer's values over it.

<!-- source: install.sh:46-47,2326,2340,2701-2702#1d268b76 -->

The run asks you to accept the engine license only when its version differs from the one you accepted. It installs no system packages: it checks that `espeak-ng`, `libopenblas.so.0`, and `libcudnn.so.9` are present, and stops when one is missing. Nothing else changes: no brain build and no other unit.

<!-- source: install.sh:47-48,3574-3580#d2f3ce52 -->

If a drop-in changes the engine's unit, for example one that a manual deployment left in `/run`, the run names it and leaves it in place. Remove it if you don't need it, and then run `sudo systemctl daemon-reload`.

<!-- source: install.sh:937-946#c43a4851 -->

### Update everything

Run the installer with no component flag:

```bash
./install.sh
```

The full run repeats the installation and downloads only the artifacts that differ. It builds the brain the way the brain-only run does, and after every unit and env file is written, it points the `venv` link at the new release, restarts the engine and the brain in one step, and checks that they come back. A full run has no automatic rollback; roll the brain or the engine back by hand afterward.

<!-- source: install.sh:5,29-33#a295df7b; install.sh:50-52#c6d99e84; cli/teaport:483#9c02008d -->

## Roll back the brain

To put the previous environment back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback brain
   ```

The run makes `venv.previous`, the environment the last brain update replaced, the live environment again: it points `venv` at that release, restarts the brain, and checks it comes back. The `teaport` command and the configuration helper come with it, because each environment carries the ones built in it.

<!-- source: install.sh:11,20-21,1042-1047,1409-1413#8bcd3541 -->

The installer accepts two components, `brain` and `engine`, and `--only` and `--rollback` do not combine in one run.

<!-- source: install.sh:115-118#ffc7d7a8 -->

The brain-only update rolls back on its own when the brain fails its health check. A full run does not; use the rollback by hand there.

<!-- source: install.sh:22-23,32-33#dcf52617 -->

## Roll back the engine

To put the previous engine back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback engine
   ```

The run takes the newest engine kept in `engine.rollback/` in the install root and makes it the live engine again, with the unit it ran under and any model it kept. It keeps the live engine there first, moves the kept files back, restarts the engine, and checks that it comes back. To undo the rollback, run it again.

<!-- source: install.sh:13,50-51,691-710,3601-3640#069be2b9 -->

The engine-only update rolls back on its own when the engine fails its check. A rollback doesn't: if the engine doesn't come back, the run stops and says so. A full run doesn't roll back either; use the rollback by hand there.

<!-- source: install.sh:45,51-52,855,3603-3604,3636#a9536167 -->

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

Every brain build lands in a new environment under `venvs/` in the install root. The `venv` symlink names the live one; `venv.previous`, the one before it, is the rollback target. The installer keeps the newest three releases, plus the live release and the rollback target, and prunes the rest before each build.

<!-- source: install.sh:34-35,1042-1047,1460-1467#bb84da25 -->

A run that changes the engine's files first keeps the engine they replace in `engine.rollback/` in the install root: its files, the unit it ran under, its entry from the installed manifest, and any model the run replaces. Each copy is numbered, so the newest copy is the one with the highest number, whatever the device's clock says. The installer keeps the newest two and removes the rest. A run that changes no engine file keeps nothing. A copy that holds a model takes that model's disk space until the installer removes the copy; the speech-to-text model is about 4.4 GB.

<!-- source: install.sh:496-503,510-516,524,586-597,722-726#e163601c -->

## What's next

- [Configure teaport](/guides/configuration/)
- [Troubleshooting](/guides/troubleshooting/)
- [Make phone calls](/guides/phone-calls/)

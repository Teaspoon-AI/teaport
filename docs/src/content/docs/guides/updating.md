---
title: Update teaport
description: Check a device against its release manifest, install the update with teaport update, update the brain or engine on its own, and roll either back.
---

The `teaport update --check` command compares the device with the release manifest and names the parts that differ; it changes nothing. `teaport update` installs a new brain, engine, or model by itself, through the installer's narrower paths: `install.sh --only brain` installs the brain package alone, and `install.sh --only engine` updates the engine alone. You can run those yourself too. A change to anything else takes a full installer run. `install.sh --rollback brain` and `install.sh --rollback engine` put the previous one back.

<!-- source: cli/teaport:485-495#1d13aff3; install.sh:9-13#3bf0e543 -->

## Before you begin

- An SSH connection to the device: every command on this page runs on it.
- A device with teaport installed. `teaport update --check` reads `/opt/teaport/manifest.json`, the manifest the installer wrote, and exits with an error if the file is missing.

<!-- source: brain/teaport_brain/updates.py:75-86,512-513#c715a888; install.sh:964,3916#cb7eb1ce -->

## Check for updates

To see which parts of the installation differ from the release manifest:

1. Sign in to the device over SSH.
2. Run the check:

   ```bash
   teaport update --check
   ```

   The command fetches a fresh copy of the release manifest, compares it with the stored one, and prints a line for the version and for each tracked part: the engine release, the brain package, the plugin version, the OpenClaw version, and the model files. `->` marks a part that `teaport update` installs; `==` marks one that matches; `!=` marks one that differs but isn't offered, such as an older release, or a part only a full `install.sh` run installs. The brain counts only when the release's package is newer than the installed one. The check ends with `up to date.` when nothing differs, or says when the channel names an older release than the device has. Otherwise it ends with a `changed:` list, then `teaport update installs:` with the parts the next section installs, and `needs a full install.sh run:` with the rest. It changes nothing on the device.

<!-- source: brain/teaport_brain/updates.py:57,243-299,450-479#7a492199 -->

## Apply an update

To install what the check found:

1. Run the command without `--check`:

   ```bash
   teaport update
   ```

2. The command prints the same comparison. When the engine, the models, or the brain changed, it asks `Install it now?`. Type `y`.

The update runs as the `teaport-update` service, and the command prints its output until it ends. The user that runs the brain can start that service without a password; anyone else is asked for an administrator's password. To install without the question, for example from a script, run `teaport update --yes`.

<!-- source: cli/teaport:580-633#cdc96203 -->

The service fetches the installer of the release, at the release tag that the manifest names. It never runs an installer from a branch, or one from before `teaport update` existed. When the engine or the models changed, it runs `install.sh --only engine`; when the brain changed, it then runs `install.sh --only brain`. Each run downloads first. An update never goes back: when the channel you pick names an older brain or an older engine than the device has, `teaport update` installs nothing from it, and `teaport update --check` says so. To go back on purpose, use `install.sh --rollback`. Before it swaps in the new engine or brain, it waits until nobody is talking to the device: no phone call, no Talk session, and no conversation with the device's microphone. After an hour of conversation, the run stops before the swap and keeps what it downloaded, so the next try is quick. Each run puts the previous engine or brain back by itself if the new one doesn't come back healthy, as the next sections describe.

On a device that an older installer set up, the first run of this version's installer rebuilds `/opt/teaport` and `/var/lib/teaport` as root's, whether that run is an update or a manual `install.sh`; [the installer reference](/reference/installer/#files-and-directories) says what that keeps. The kept engine rollback copies are not kept, and the next downloads start over.

If the engine's license is newer than the one you accepted, the engine update stops and asks for a terminal. Run `install.sh --only engine` from the one-line install in a terminal, accept the license, and then run `teaport update` again.

<!-- source: packaging/deb/update_apply.py:3-8,14-36,74#56a7a7da; install.sh:978-984#90143c15; packaging/deb/update_apply.py:335-338#032843e1; install.sh:1390-1405#530a8394; packaging/deb/update_apply.py:191-271,402#52468ac8; brain/teaport_brain/updates.py:250-258#76305232 -->

The device updates from its own channel, `TEAPORT_UPDATE_CHANNEL` in `/etc/teaport/brain.env`. A change to the plugin or to the OpenClaw version needs a full installer run, described in [Update everything](#update-everything).

To stop following the update, press Ctrl+C: the update goes on. To see how the last update went:

```bash
teaport update --status
```

The command prints `running`, `done`, `failed`, or `interrupted`, for a run that a power cut stopped, with the step and the reason. The full output is in `/var/lib/teaport-update/log` and in `journalctl -u teaport-update`.

<!-- source: cli/teaport:496-497,559-578#8393ae64; packaging/deb/update_apply.py:38-43#22b253f7 -->

### Update only the brain

The brain is a package, `teaport-brain`, that the project builds in CI. Nothing is built on the device. The package holds the brain's Python environment (`/opt/teaport/brain`), the `teaport` command (`/usr/bin/teaport`), and the helpers that the configuration page and Wi-Fi setup run as root.

<!-- source: install.sh:1313-1316#cee553a8; packaging/nfpm.yaml:61-107#65f36e8c; packaging/nfpm.yaml:61-95#eb12860b -->

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

<!-- source: install.sh:3,17-19#108120c1; install.sh:1362-1435#7f7db3bd -->

The run downloads the package and checks its sha256, keeps a copy in `/var/cache/teaport`, and installs it with `apt-get`. It renders the units that run brain code (`teaport-brain`, plus the audio bridge and the Wi-Fi setup when the device has them), restarts `teaport-brain`, and checks that the brain comes back. If the brain does not come back healthy, the run installs the package that was installed before and fails. If `apt-get` cannot install the package at all, for example because another package manager holds the lock for more than two minutes, the run stops with the device as it was and restarts nothing.

<!-- source: install.sh:17-23#03f10c8d; install.sh:1544-1623#309a5ec8; install.sh:1543-1622#599472ac -->

The `teaport-brain` unit waits for the brain to report that it is serving, so a restart fails when the new brain never gets ready. The swap restarts the local audio bridge with the brain when the `teaport-local-audio` service is active; the brain's health alone decides whether the swap stays.

<!-- source: systemd/teaport-brain.service.in:12-17#78e8f1b4; install.sh:1544-1623#309a5ec8; install.sh:1543-1622#599472ac -->

A Talk session in progress ends at the restart. A phone call stays connected at the gateway, and the brain rejoins it with a short notice that it lost the connection.

<!-- source: install.sh:1302#38ebb95b; brain/teaport_brain/sip_server.py:45-52#9213415f -->

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

<!-- source: install.sh:37-41,569-574,605,613-627,951-952#6e99dc07 -->

Then the run keeps a copy of the live engine for a rollback, moves the new files in, renders `teaport-engine.service`, and restarts the engine. The brain reconnects to it. The run checks that the engine listens on its port within three minutes, stays up, and loaded its pronunciation dictionary. If the engine does not come back healthy, the run puts back the previous engine with its models and its unit, removes the keys it added to `engine.env`, and fails.

<!-- source: install.sh:42-45,1039,1047,1058,1124-1142#671772e7 -->

If the run stops while it moves the new files in, it puts the previous engine back. If a power cut stops it there, the next run of the installer does that first. The installer runs one at a time: a second run started while one is running stops right away with `another install.sh is running on this box`.

<!-- source: install.sh:196-214,705-710,733-768#7f5b003a -->

The engine takes about a minute to load its models after a restart, and a Talk session or a phone call in progress loses the engine. When the engine's files, its unit, and `engine.env` are already current and the engine is running, the run checks the engine and doesn't restart it.

<!-- source: install.sh:49,1073,1116-1122#7a387a56; brain/teaport_brain/config_schema.toml:66#80379fdf -->

`/etc/teaport/engine.env` gains only the keys it lacks. A value that is already in the file stays, so a `KOKORO_RESERVE_FPT` that you tuned survives the update. A full run writes the installer's values over it.

<!-- source: install.sh:46-47,2617,2631,3017-3018#1d268b76 -->

The run asks you to accept the engine license only when its version differs from the one you accepted. It installs no system packages: it checks that `espeak-ng`, `libopenblas.so.0`, and `libcudnn.so.9` are present, and stops when one is missing. Nothing else changes: no brain build and no other unit.

<!-- source: install.sh:47-48,3893-3899#d2f3ce52 -->

If a drop-in changes the engine's unit, for example one that a manual deployment left in `/run`, the run names it and leaves it in place. Remove it if you don't need it, and then run `sudo systemctl daemon-reload`.

<!-- source: install.sh:1083-1092#c43a4851 -->

### Update everything

Run the installer with no component flag:

```bash
./install.sh
```

The full run repeats the installation and downloads only the artifacts that differ. It installs the brain package the way the brain-only run does, and after every unit and env file is written, it restarts the engine and the brain in one step and checks that they come back. A full run has no automatic rollback; roll the brain or the engine back by hand afterward.

<!-- source: install.sh:5,31-33#69faf65a; install.sh:50-52#c6d99e84 -->

## Roll back the brain

To put the previous brain package back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback brain
   ```

The run installs the newest other package kept in `/var/cache/teaport`, restarts the brain, and checks it comes back. The `teaport` command and the configuration helper come with the package, so they go back together with the brain. The package that the run replaces stays kept, so running the rollback again undoes it.

<!-- source: install.sh:3852-3872#a43d31fd -->

The installer accepts two components, `brain` and `engine`, and `--only` and `--rollback` do not combine in one run.

<!-- source: install.sh:134-137#ffc7d7a8 -->

The brain-only update rolls back on its own when the brain fails its health check. A full run does not; use the rollback by hand there.

<!-- source: install.sh:22-23,31-33#741a2838 -->

## Roll back the engine

To put the previous engine back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback engine
   ```

The run takes the newest engine kept in `engine.rollback/` in the install root and makes it the live engine again, with the unit it ran under and any model it kept. It keeps the live engine there first, moves the kept files back, restarts the engine, and checks that it comes back. To undo the rollback, run it again.

<!-- source: install.sh:13,50-51,775-795,3923-3962#c2118019 -->

The engine-only update rolls back on its own when the engine fails its check. A rollback doesn't: if the engine doesn't come back, the run stops and says so. A full run doesn't roll back either; use the rollback by hand there.

<!-- source: install.sh:45,51-52,956,3925-3926,3958#a9536167 -->

## Choose a release channel

A channel is a manifest. The repository ships two: `stable.json`, the default, and `beta.json`, which has the same shape and placeholder values until the first beta release is cut.

<!-- source: manifest/stable.json:2-4#1b522f83; manifest/beta.json:2-4#00ca757c -->

Both the installer and `teaport update --check` read the URL or local path in `TEAPORT_MANIFEST_URL`, `https://get.teaspoon.tech/manifest/stable.json` by default. To follow the beta channel, name the beta manifest:

```bash
TEAPORT_MANIFEST_URL=https://get.teaspoon.tech/manifest/beta.json ./install.sh
```

<!-- source: install.sh:56,95#0f9477e2; brain/teaport_brain/updates.py:103-109#e5101603 -->

The installer takes its channel from that variable alone; `teaport update` passes the device's channel to the installer it runs. `teaport update --check` also takes `--channel stable` or `--channel beta` for one run. The brain checks every day too: set `TEAPORT_UPDATE_CHANNEL=beta` in `/etc/teaport/brain.env` to make `beta` the channel it and the command look at, and `TEAPORT_UPDATE_CHECK_SECS=0` to turn the daily check off. The brain sees `TEAPORT_MANIFEST_URL` only when it is in `/etc/teaport/brain.env`; exported in a shell, it changes the command and not the daily check.

<!-- source: install.sh:120-130#ef93f814; brain/teaport_brain/updates.py:94-109,335-348#03e7c3dd -->

## Retained releases

The installer keeps the newest three brain packages in `/var/cache/teaport`, plus the installed one, and removes the rest after each install. A rollback goes back to the newest other package in that directory.

<!-- source: install.sh:1331-1332#5997041c; install.sh:1446-1455#4f210a86 -->

A run that changes the engine's files first keeps the engine they replace in `engine.rollback/` in the install root: its files, the unit it ran under, its entry from the installed manifest, and any model the run replaces. Each copy is numbered, so the newest copy is the one with the highest number, whatever the device's clock says. The installer keeps the newest two and removes the rest. A run that changes no engine file keeps nothing. A copy that holds a model takes that model's disk space until the installer removes the copy; the speech-to-text model is about 4.4 GB.

<!-- source: install.sh:575-582,589-595,603,665-676,807-811#e163601c -->

## What's next

- [Configure teaport](/guides/configuration/)
- [Troubleshooting](/guides/troubleshooting/)
- [Make phone calls](/guides/phone-calls/)

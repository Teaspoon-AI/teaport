---
title: Update teaport
description: Check a device against its release manifest, install the update with teaport update, update the brain or engine on its own, and roll either back.
---

The `teaport update --check` command compares the device with the release manifest and names the parts that differ; it changes nothing. `teaport update` installs a new brain, engine, or model by itself, through the installer's narrower paths: `install.sh --only brain` installs the brain package alone, and `install.sh --only engine` updates the engine alone. You can run those yourself too. A change to anything else takes a full installer run. `install.sh --rollback brain` and `install.sh --rollback engine` put the previous one back.

<!-- source: cli/teaport:490-500#1d13aff3; install.sh:9-13#3bf0e543 -->

## Before you begin

- An SSH connection to the device: every command on this page runs on it.
- A device with teaport installed. `teaport update --check` reads `/opt/teaport/manifest.json`, the manifest the installer wrote, and exits with an error if the file is missing.

<!-- source: brain/teaport_brain/updates.py:85-96,563-564#c715a888; install.sh:1420,4520#cb7eb1ce -->

## Check for updates

To see which parts of the installation differ from the release manifest:

1. Sign in to the device over SSH.
2. Run the check:

   ```bash
   teaport update --check
   ```

   The command fetches a fresh copy of the release manifest, compares it with the stored one, and prints a line for the version and for each tracked part: the engine release, the brain package, the plugin version, the OpenClaw version, and the model files. `->` marks a part that `teaport update` installs; `==` marks one that matches; `!=` marks one that differs but isn't offered, such as an older release, or a part only a full `install.sh` run installs. The brain counts only when the release's package is newer than the installed one. The check ends with `up to date.` when nothing differs, or says when the channel names an older release than the device has. Otherwise it ends with a `changed:` list, then `teaport update installs:` with the parts the next section installs, and `needs a full install.sh run:` with the rest. It changes nothing on the device.

<!-- source: brain/teaport_brain/updates.py:64,264-320,494-529#df65ae06 -->

## Apply an update

To install what the check found:

1. Run the command without `--check`:

   ```bash
   teaport update
   ```

2. The command prints the same comparison. When the engine, the models, or the brain changed, it asks `Install it now?`. Type `y`.

The update runs as the `teaport-update` service, and the command prints its output until it ends. The user that runs the brain can start that service without a password; anyone else is asked for an administrator's password. To install without the question, for example from a script, run `teaport update --yes`.

<!-- source: cli/teaport:585-638#cdc96203 -->

The service reads the release manifest of the device's channel, and its signature, from the download server. It installs nothing unless the signature verifies, as [The signed release manifest](#the-signed-release-manifest) describes. The service fetches the installer of the release, at the release tag that the manifest names, and only when that tag is still at the commit the manifest names. It never runs an installer from a branch, from a tag that was moved, or from before `teaport update` existed. When the engine or the models changed, it runs `install.sh --only engine`; when the brain changed, it then runs `install.sh --only brain`. Each run downloads first. An update never goes back: when the channel you pick names an older brain or an older engine than the device has, `teaport update` installs nothing from it, and `teaport update --check` says so. To go back on purpose, use `install.sh --rollback`. Before it swaps in the new engine or brain, it waits until nobody is talking to the device: no phone call, no Talk session, and no conversation with the device's microphone. After an hour of conversation, the run stops before the swap and keeps what it downloaded, so the next try is quick. Each run puts the previous engine or brain back by itself if the new one doesn't come back healthy, as the next sections describe.

On a device that an older installer set up, the first run of this version's installer rebuilds `/opt/teaport` and `/var/lib/teaport` as root's, whether that run is an update or a manual `install.sh`; [the installer reference](/reference/installer/#files-and-directories) says what that keeps. The kept engine rollback copies are not kept, and the next downloads start over.

If the engine's license is newer than the one you accepted, the engine update stops and asks for a terminal. Run `install.sh --only engine` from the one-line install in a terminal, accept the license, and then run `teaport update` again. The same happens on a device with no acceptance on record with Teaspoon AI, whatever license version it accepted: the installer asks at the terminal, and `teaport update` stops at the engine step.

<!-- source: packaging/deb/update_apply.py:3-8,14-43,85#cac4cbc8; install.sh:1434-1440#90143c15; packaging/deb/update_apply.py:427-430#032843e1; install.sh:1868-1883#530a8394; packaging/deb/update_apply.py:250-330,495#52468ac8; brain/teaport_brain/updates.py:271-279#76305232 -->

The device updates from its own channel, `TEAPORT_UPDATE_CHANNEL` in `/etc/teaport/brain.env`. A change to the plugin or to the OpenClaw version needs a full installer run, described in [Update everything](#update-everything).

To stop following the update, press Ctrl+C: the update goes on. To see how the last update went:

```bash
teaport update --status
```

The command prints `running`, `done`, `failed`, or `interrupted`, for a run that a power cut stopped, with the step and the reason. The full output is in `/var/lib/teaport-update/log` and in `journalctl -u teaport-update`.

<!-- source: cli/teaport:501-502,564-583#8393ae64; packaging/deb/update_apply.py:45-50#22b253f7 -->

### Update only the brain

The brain is a package, `teaport-brain`, that the project builds in CI. Nothing is built on the device. The package holds the brain's Python environment (`/opt/teaport/brain`), the `teaport` command (`/usr/bin/teaport`), and the helpers that the configuration page, Wi-Fi setup, and the owner's login run as root.

<!-- source: install.sh:1791-1794#cee553a8; packaging/nfpm.yaml:67-110#c48d03ff -->

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

<!-- source: install.sh:3,19-21#108120c1; install.sh:1840-1913#7f7db3bd -->

The run downloads the package and checks its sha256, keeps a copy in `/var/cache/teaport`, and installs it with `apt-get`. It renders the units that run brain code (`teaport-brain`, plus the audio bridge and the Wi-Fi setup when the device has them), restarts `teaport-brain`, and checks that the brain comes back. If the brain does not come back healthy, the run installs the package that was installed before and fails. If `apt-get` cannot install the package at all, for example because another package manager holds the lock for more than two minutes, the run stops with the device as it was and restarts nothing.

<!-- source: install.sh:19-25#03f10c8d; install.sh:2022-2101#309a5ec8; install.sh:2021-2100#599472ac -->

The `teaport-brain` unit waits for the brain to report that it is serving, so a restart fails when the new brain never gets ready. The swap restarts the local audio bridge with the brain when the `teaport-local-audio` service is active; the brain's health alone decides whether the swap stays.

<!-- source: systemd/teaport-brain.service.in:12-17#78e8f1b4; install.sh:2022-2101#309a5ec8; install.sh:2021-2100#599472ac -->

A Talk session in progress ends at the restart. A phone call stays connected at the gateway, and the brain rejoins it with a short notice that it lost the connection.

<!-- source: install.sh:1780#38ebb95b; brain/teaport_brain/sip_server.py:45-52#9213415f -->

Nothing else changes: no engine download, no license prompt, no system packages beyond the package's own dependencies, and no edit to `/etc/teaport`. A brain change that needs a new setting there ships in a full run instead.

<!-- source: install.sh:27-29#d7143d85 -->

On a device that still runs the retired `teaport-sip-brain` unit, the run stops that unit before the swap and removes it after; the SIP gateway keeps serving, and the new brain connects to it. The unit's drop-in tuning moves into `/etc/teaport/brain.env`.

<!-- source: install.sh:30-32#a64f5eaf -->

### Update only the engine

To install a new engine release without a full run, run the installer with `--only engine`. From a checkout of the teaport repository:

```bash
git pull && ./install.sh --only engine
```

On a device without a checkout, append the flag to the one-line install:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only engine
```

<!-- source: install.sh:3,12,39-40#5088e1dc -->

The engine comes from the release manifest, and the `teaport-engine` unit comes from the checkout. The run downloads only the files whose sha256 differs from the manifest, out of the engine's binary, its pronunciation dictionary, its part-of-speech tagger, its Mandarin dictionary and its AI watermark weights (both from engine 1.7), `THIRD_PARTY_NOTICES.txt`, and its two models. It downloads them beside the live files, so the engine keeps serving, and checks them as a full run does. If a download is interrupted, the next run continues it where it stopped.

<!-- source: install.sh:39-43,955-960,990-991,999-1015,1374-1375,1397-1398#fffb3923; install.sh:1354-1355#462dacb2 -->

Then the run keeps a copy of the live engine for a rollback, moves the new files in, renders `teaport-engine.service`, and restarts the engine. The brain reconnects to it. The run checks that the engine listens on its port within three minutes, stays up, loaded its pronunciation dictionary, and, from engine 1.7, loaded its AI watermark. If the engine does not come back healthy, the run puts back the previous engine with its models and its unit, removes the keys it added to `engine.env`, and fails.

<!-- source: install.sh:44-47,1496,1504,1519,1602-1620#671772e7; install.sh:1536-1538#c24c55ca -->

If the run stops while it moves the new files in, it puts the previous engine back. If a power cut stops it there, the next run of the installer does that first. The installer runs one at a time: a second run started while one is running stops right away with `another install.sh is running on this box`.

<!-- source: install.sh:236-254,1112-1117,1140-1175#7f5b003a -->

The engine takes about a minute to load its models after a restart, and a Talk session or a phone call in progress loses the engine. When the engine's files, its unit, and `engine.env` are already current and the engine is running, the run checks the engine and doesn't restart it.

<!-- source: install.sh:51,1551,1594-1600#7a387a56; brain/teaport_brain/config_schema.toml:66#80379fdf -->

`/etc/teaport/engine.env` gains only the keys it lacks. A value that is already in the file stays, so a `KOKORO_RESERVE_FPT` that you tuned survives the update. A full run writes the installer's values over it.

<!-- source: install.sh:48-49,3178,3192,3581-3582#1d268b76 -->

The run asks you to accept the engine license only when its version differs from the one you accepted. It installs no system packages: it checks that `espeak-ng`, `libopenblas.so.0`, and `libcudnn.so.9` are present, and stops when one is missing. Nothing else changes: no brain build and no other unit.

<!-- source: install.sh:49-50,4497-4503#d2f3ce52 -->

If a drop-in changes the engine's unit, for example one that a manual deployment left in `/run`, the run names it and leaves it in place. Remove it if you don't need it, and then run `sudo systemctl daemon-reload`.

<!-- source: install.sh:1561-1570#c43a4851 -->

### Update everything

Run the installer with no component flag:

```bash
./install.sh
```

The full run repeats the installation and downloads only the artifacts that differ. It installs the brain package the way the brain-only run does, and after every unit and env file is written, it restarts the engine and the brain in one step and checks that they come back. A full run has no automatic rollback; roll the brain or the engine back by hand afterward.

<!-- source: install.sh:5,33-35#69faf65a; install.sh:52-54#c6d99e84 -->

## Roll back the brain

To put the previous brain package back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback brain
   ```

The run installs the newest other package kept in `/var/cache/teaport`, restarts the brain, and checks it comes back. The `teaport` command and the root helpers come with the package, so they go back together with the brain. The package that the run replaces stays kept, so running the rollback again undoes it.

<!-- source: install.sh:4456-4476#a43d31fd -->

The installer accepts two components, `brain` and `engine`, and `--only` and `--rollback` do not combine in one run.

<!-- source: install.sh:174-177#ffc7d7a8 -->

The brain-only update rolls back on its own when the brain fails its health check. A full run does not; use the rollback by hand there.

<!-- source: install.sh:24-25,33-35#741a2838 -->

## Roll back the engine

To put the previous engine back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback engine
   ```

The run takes the newest engine kept in `engine.rollback/` in the install root and makes it the live engine again, with the unit it ran under and any model it kept. It keeps the live engine there first, moves the kept files back, restarts the engine, and checks that it comes back. To undo the rollback, run it again.

<!-- source: install.sh:13,52-53,1182-1202,4527-4566#c2118019 -->

The engine-only update rolls back on its own when the engine fails its check. A rollback doesn't: if the engine doesn't come back, the run stops and says so. A full run doesn't roll back either; use the rollback by hand there.

<!-- source: install.sh:47,53-54,1402,4529-4530,4562#a9536167 -->

## Choose a release channel

A channel is a manifest. The repository ships two: `stable.json`, the default, and `beta.json`, which has the same shape and placeholder values until the first beta release is cut.

<!-- source: manifest/stable.json:2-4#1b522f83; manifest/beta.json:2-4#00ca757c -->

Both the installer and `teaport update --check` read the URL or local path in `TEAPORT_MANIFEST_URL`, `https://get.teaspoon.tech/manifest/stable.json` by default. To follow the beta channel, name the beta manifest:

```bash
TEAPORT_MANIFEST_URL=https://get.teaspoon.tech/manifest/beta.json ./install.sh
```

<!-- source: install.sh:72-73,123#42e5c1e4; brain/teaport_brain/updates.py:113-116#315efc5e -->

The installer takes its channel from that variable alone; `teaport update` passes the device's channel to the installer it runs. `teaport update --check` also takes `--channel stable` or `--channel beta` for one run. The brain checks every day too: set `TEAPORT_UPDATE_CHANNEL=beta` in `/etc/teaport/brain.env` to make `beta` the channel it and the command look at, and `TEAPORT_UPDATE_CHECK_SECS=0` to turn the daily check off. The brain sees `TEAPORT_MANIFEST_URL` only when it is in `/etc/teaport/brain.env`; exported in a shell, it changes the command and not the daily check.

<!-- source: install.sh:150-173#e841289d; brain/teaport_brain/updates.py:104-116,371-384#86726fb1 -->

## The signed release manifest

Each release manifest is signed. The signature is a file next to the manifest, with the same name and `.sig` added: `stable.json.sig` next to `stable.json`. The keys that can sign a release are in `/usr/lib/teaport/allowed_signers`, which the brain package installs: a main key and a backup key. Before they read anything from a manifest, the installer, `teaport update`, and the brain's daily check verify its signature against those keys with `ssh-keygen`. A manifest named `stable.json` or `beta.json` must also name that channel inside it, so the manifest of one channel can't stand in for the other's. Only the `beta` channel names a release candidate, a tag such as `brain-v0.1.1-rc2` whose brain package is version `0.1.1~rc2`; a signed `stable.json` that names one is refused, and so is a manifest whose brain package isn't the version its release tag builds, or one that names a release under a tag that isn't a release tag. On a device that doesn't have the brain package yet, such as during a first install, the installer uses `manifest/allowed_signers` from its own checkout.

<!-- source: brain/teaport_brain/manifest_sig.py:4-12#4d2ee9ce; install.sh:298-334#947f532f; packaging/nfpm.yaml:112-127#c685f91b; brain/teaport_brain/updates.py:130-143#d522a480 -->

When the signature is missing or doesn't verify, nothing is installed:

- The installer stops with `the release manifest is refused, nothing was installed:` and the reason.
- `teaport update` ends as `failed`, and `teaport update --status` prints the reason.
- `teaport update --check` exits with `cannot read the release manifest:` and the reason, and the daily check doesn't offer the release.

<!-- source: install.sh:355-357#ca94faed; packaging/deb/update_apply.py:206-224#5d09851d; brain/teaport_brain/updates.py:568-572#5898d5d7 -->

A copy of a manifest on a local disk or on a mirror is checked the same way. Copy the `.sig` file with the manifest, and set `TEAPORT_MANIFEST_URL` to the copy's absolute path or URL. To install from a local manifest that has no signature, such as one you wrote for a development device, set `TEAPORT_MANIFEST_UNSIGNED=1` for that run of the installer. The variable doesn't apply to a URL, and `teaport update` never sets it. The brain's daily check sees it, as it sees `TEAPORT_MANIFEST_URL`, only when it is in `/etc/teaport/brain.env`.

<!-- source: install.sh:335-383#44445033; packaging/deb/update_apply.py:417-430#e8ec9da2; brain/teaport_brain/updates.py:130-143#d522a480 -->

## Retained releases

The installer keeps the newest three brain packages in `/var/cache/teaport`, plus the installed one, and removes the rest after each install. A rollback goes back to the newest other package in that directory.

<!-- source: install.sh:1809-1810#5997041c; install.sh:1924-1933#4f210a86 -->

A run that changes the engine's files first keeps the engine they replace in `engine.rollback/` in the install root: its files, the unit it ran under, its entry from the installed manifest, and any model the run replaces. Each copy is numbered, so the newest copy is the one with the highest number, whatever the device's clock says. The installer keeps the newest two and removes the rest. A run that changes no engine file keeps nothing. A copy that holds a model takes that model's disk space until the installer removes the copy; the speech-to-text model is about 4.4 GB.

<!-- source: install.sh:961-968,975-982,989,1072-1083,1214-1218#420fb4ad -->

## What's next

- [Configure teaport](/guides/configuration/)
- [Troubleshooting](/guides/troubleshooting/)
- [Make phone calls](/guides/phone-calls/)

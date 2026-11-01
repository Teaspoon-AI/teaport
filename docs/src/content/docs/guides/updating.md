---
title: Update teaport
description: Check an installed device against its release manifest, update the brain on its own, and roll the brain back.
---

The installer applies every update. The `teaport update` command compares the device with the release manifest and names the parts that differ; it changes nothing. The brain also has a narrower path: `install.sh --only brain` rebuilds the brain alone, and `install.sh --rollback brain` puts the previous brain back.

<!-- source: cli/teaport:281-309#60118f99; install.sh:9-11#ae7c9be6 -->

## Before you begin

- An SSH connection to the device: every command on this page runs on it.
- A device with teaport installed. `teaport update` reads `/opt/teaport/manifest.json`, the manifest the installer wrote, and exits with an error if the file is missing.

<!-- source: cli/teaport:17,283-284#81a89efd; install.sh:499#7b0e6e59 -->

## Check for updates

To see which parts of the installation differ from the release manifest:

1. Sign in to the device over SSH.
2. Run the check:

   ```bash
   teaport update --check
   ```

   The command fetches a fresh copy of the release manifest, compares it with the stored one, and prints a line for the version and for each tracked part: the engine release, the brain tag, and the plugin version. `->` marks a part that differs; `==` marks one that matches. The check ends with `up to date.` when nothing differs, or with a `changed:` list. It changes nothing on the device.

<!-- source: cli/teaport:281-304#c24b4811 -->

## Apply an update

Run the comparison without `--check` to see the parts that changed and the command that applies them:

```bash
teaport update
```

The command prints the same comparison and, when a part differs, names the run to make: `install.sh --only brain` for the brain, a full `install.sh` run for the rest. The installer does the update; the command does not.

<!-- source: cli/teaport:303,305-309#c47eb35a -->

### Update only the brain

From a checkout of the teaport repository, update the source and rebuild the brain from it:

```bash
git pull && ./install.sh --only brain
```

On a device without a checkout, append the flag to the one-line install:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --only brain
```

<!-- source: install.sh:3,15-17#774ff01d -->

The run builds a new Python environment from `brain/uv.lock` beside the live one, so the brain keeps serving while it builds. It checks the new environment, swaps it in together with the `teaport` command and the configuration helper built in it, restarts `teaport-brain`, and checks that the brain comes back. If the brain does not come back healthy, the run puts the previous environment back by itself.

<!-- source: install.sh:17-21#191a38f6 -->

The swap restarts the local audio bridge with the brain when the `teaport-local-audio` service is active; the brain's health alone decides whether the swap stays.

<!-- source: install.sh:817-819,835-837#ab020d70 -->

A Talk session in progress ends at the restart. A phone call stays connected at the gateway, and the brain rejoins it with a short notice that it lost the connection.

<!-- source: install.sh:895#38ebb95b; brain/teaport_brain/sip_server.py:45-52#9213415f -->

Nothing else changes: no engine download, no license prompt, no systemd unit or `/etc/teaport` edit. A brain change that needs a new unit or a new setting there ships in a full run instead.

<!-- source: install.sh:21-23#483a9f4f; install.sh:27-28#67547537 -->

On a device that still runs the retired `teaport-sip-brain` unit, the run stops that unit before the swap and removes it after; the SIP gateway keeps serving, and the new brain connects to it. The unit's drop-in tuning moves into `/etc/teaport/brain.env`.

<!-- source: install.sh:24-26#06ec7b3c -->

### Update everything

Run the installer with no component flag:

```bash
./install.sh
```

The full run repeats the installation and downloads only the artifacts that differ. It builds the brain the way the brain-only run does, and after every unit and env file is written, it points the `venv` link at the new release, restarts the engine and the brain in one step, and checks that they come back. A full run has no automatic rollback; roll the brain back by hand afterward.

<!-- source: install.sh:5,27-31#a295df7b; cli/teaport:309#9ea494d2 -->

## Roll back the brain

To put the previous environment back, run the rollback on its own:

1. On the device, run:

   ```bash
   ./install.sh --rollback brain
   ```

The run makes `venv.previous`, the environment the last brain update replaced, the live environment again: it points `venv` at that release, restarts the brain, and checks it comes back. The `teaport` command and the configuration helper come with it, because each environment carries the ones built in it.

<!-- source: install.sh:11,18-19,546-551,906-910#8bcd3541 -->

The rollback exists for the brain: the installer accepts no other component, and `--only` and `--rollback` do not combine in one run.

<!-- source: install.sh:99-102#13f0471b -->

The brain-only update rolls back on its own when the brain fails its health check. A full run does not; use the rollback by hand there.

<!-- source: install.sh:20-21,30-31#dcf52617 -->

## Choose a release channel

A channel is a manifest. The repository ships two: `stable.json`, the default, and `beta.json`, which has the same shape and placeholder values until the first beta release is cut.

<!-- source: manifest/stable.json:2-4#1b522f83; manifest/beta.json:2-4#00ca757c -->

Both the installer and `teaport update` read the URL or local path in `TEAPORT_MANIFEST_URL`, `https://get.teaspoon.tech/manifest/stable.json` by default. To follow the beta channel, name the beta manifest:

```bash
TEAPORT_MANIFEST_URL=https://get.teaspoon.tech/manifest/beta.json ./install.sh
```

<!-- source: install.sh:36,65#0f9477e2; cli/teaport:20,73#d418858c -->

No command takes a channel option: the installer parses `--dry-run`, `--accept-eula`, `--only`, `--rollback`, and `--help`, and `teaport update` parses `--check`. The variable is how you choose.

<!-- source: install.sh:85-95#d34c715a; cli/teaport:281-282#07b8ea70 -->

## Retained releases

Every brain build lands in a new environment under `venvs/` in the install root. The `venv` symlink names the live one; `venv.previous`, the one before it, is the rollback target. The installer keeps the newest three releases, plus the live release and the rollback target, and prunes the rest before each build.

<!-- source: install.sh:32-33,546-551,959-966#bb84da25 -->

## What's next

- [Configure teaport](/guides/configuration/)
- [Troubleshooting](/guides/troubleshooting/)
- [Make phone calls](/guides/phone-calls/)

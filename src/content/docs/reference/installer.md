---
title: Installer reference
description: Every flag, environment override, file, and systemd unit the teaport installer creates or manages.
---

The installer is the `install.sh` script at the root of the teaport repository. It installs the engine, the models, the brain, and the systemd units on a Jetson Orin running JetPack 7.2 with CUDA 13, as the release manifest describes. The OpenClaw plugin is optional. The installer is idempotent: rerunning it repairs the existing installation.

<!-- source: install.sh:3-7 @1e5604c -->

For a first install, follow [Install teaport](/getting-started/installation/). This page documents the installer itself.

## Two ways to run the installer

### The one-line installer

To install teaport without a checkout, run:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

That URL serves a bootstrap script. A piped script arrives as a single file, and the installer needs the whole repository, so the bootstrap makes a shallow clone of the teaport repository in a temporary directory, runs `install.sh` from the clone, and removes the clone when the installer exits. The bootstrap needs `git`; without it, it prints the `apt-get` command to run and stops.

<!-- source: install-bootstrap.sh:7-10,21-34 @1e5604c -->

Two environment variables apply only to the one-line form:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEAPORT_REPO` | `https://github.com/Teaspoon-AI/teaport.git` | The Git URL to clone. |
| `TEAPORT_REF` | `main` | The branch or tag to install. |

<!-- source: install-bootstrap.sh:12-14,18-19 @1e5604c -->

Every other argument and environment variable passes through to `install.sh` unchanged:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --dry-run
```

<!-- source: install-bootstrap.sh:15,34 @1e5604c -->

### From a checkout

Clone the repository and run the installer from the clone:

```bash
git clone https://github.com/Teaspoon-AI/teaport.git
cd teaport
./install.sh
```

<!-- source: install-bootstrap.sh:18 @1e5604c -->

The checkout is the source: the brain, the plugin, and the unit templates come from the tree the script runs in.

<!-- source: install.sh:74-75 @1e5604c -->

You do not need to be root. As a regular user, the installer runs each privileged command under `sudo`; as root, it runs the command directly.

<!-- source: install.sh:104-105 @1e5604c -->

## Command-line flags

The parser exits with status 2 on an unknown argument, a missing value, or `--only` together with `--rollback`. The two component flags also take an equals form, for example `--only=brain`, and the component must be `brain`.

<!-- source: install.sh:77-95 @1e5604c -->

| Flag | Argument | What it does |
| --- | --- | --- |
| (none) | None | Installs or repairs everything: the engine, the models, the brain, the systemd units, and, when the install includes it, the OpenClaw plugin. |
| `--only` | `brain` | Builds a new brain venv from `brain/uv.lock` beside the live one, swaps it in, restarts the brain unit, and rolls back on its own if the brain does not come back. No EULA prompt, no system packages, no engine download, no unit or env rewrite. |
| `--rollback` | `brain` | Puts the previous brain venv back, with the teaport CLI and config helper it was built with. |
| `--dry-run` | None | Prints every action, privileged ones included, instead of running it. |
| `--accept-eula` | None | Accepts the engine license without the prompt, the flag form of `TEAPORT_ACCEPT_EULA=1`. |
| `-h`, `--help` | None | Prints the usage text from the header comment and exits. |

<!-- source: install.sh:9-33,81,102-105,200 @1e5604c -->

A full install writes every unit and env file and restarts the engine and the brain in one step. It has no automatic rollback, but `--rollback brain` still works after it.

<!-- source: install.sh:27-31 @1e5604c -->

## Environment variables

The installer honors the overrides in this table; the default applies when the variable is unset. `TEAPORT_REPO` and `TEAPORT_REF` belong to the one-line form alone.

<!-- source: install.sh:35-54,58-70,241,1530,1901,1908,2356,2375-2376,2388,2403 @1e5604c -->

| Variable | Default | Controls |
| --- | --- | --- |
| `TEAPORT_MANIFEST_URL` | `https://get.teaspoon.tech/manifest/stable.json` | The release manifest URL, or a local path. |
| `TEAPORT_PREFIX` | `/opt/teaport` | The install root. |
| `TEAPORT_ETC` | `/etc/teaport` | The directory for the non-secret env files. |
| `TEAPORT_BRAIN_SRC` | None | Install the brain from this local directory instead of cloning. |
| `TEAPORT_PLUGIN_SRC` | None | Install the plugin from this local directory instead of npm. |
| `TEAPORT_ACCEPT_EULA` | `0` | `1` accepts the engine EULA without the prompt. |
| `TEAPORT_ALLOW_NON_JETSON` | `0` | `1` skips the Jetson and L4T gate, for development and dry runs. |
| `TEAPORT_ENABLE_BRIDGE` | `0` | `1` installs the Discord voice bridge and enables its unit. |
| `TEAPORT_BRIDGE_GUILD_ID`, `TEAPORT_BRIDGE_FOLLOW_USER_ID` | None | The bridge's Discord guild and followed user. |
| `TEAPORT_BRIDGE_SRC` | None | Install the bridge from this local directory instead of cloning. |
| `TEAPORT_ENABLE_LOCAL_AUDIO` | `0` | `1` enables the local microphone and speaker bridge, a USB mic array. |
| `TEAPORT_LOCAL_AUDIO_DEVICE` | `hw:CARD=Array,DEV=0`, the device in `.env` | The ALSA device for the local audio bridge. |
| `TEAPORT_ENABLE_OLED_AVATAR` | `0` | `1` installs the OLED face and its unit. The face repository is private, so the installer skips it where access is missing. |
| `TEAPORT_OLED_PORT`, `TEAPORT_OLED_ADDR` | None; the installer probes for `0x3c` or `0x3d` | The OLED panel's I2C bus and address. |
| `TEAPORT_OLED_SRC` | None | Install the avatar from a local checkout instead of cloning. |
| `TEAPORT_ALLOW_STALE_SOURCE` | `0` | `1` installs from a checkout that is behind its upstream. |
| `TEAPORT_OPENCLAW_HEAP_MIB` | `1536` | The host OpenClaw gateway's JS heap limit. |

## Files and directories

A full install creates the paths in this section. `TEAPORT_PREFIX`, `TEAPORT_ETC`, `TEAPORT_SECRETS_DIR`, and `TEAPORT_STATE` relocate the install root, the env directory, the secrets directory, and the state directory.

<!-- source: install.sh:59-62 @1e5604c -->

**`/opt/teaport/`**

The install root. It holds the `uv` binary, the model directories `models/voxtral/` and `models/kokoro/`, the `bridge/` tree, and the brain venvs.

<!-- source: install.sh:418,533,1940 @1e5604c -->

**`/opt/teaport/venv`, `venv.previous`, and `venvs/`**

Every install builds a new brain venv in `venvs/`, one per install. `venv` is a symlink to the live one, `venv.previous` is the one before it and the rollback target, and `venv.pre-uv` holds a pip-era venv kept for the same purpose. The newest three releases stay on the device, plus the live one and the rollback target.

<!-- source: install.sh:539-546,558-561,952-954,959 @1e5604c -->

**`/etc/teaport/`**

The non-secret configuration. The installer writes `engine.env` and `brain.env` here, group-readable with mode 640.

<!-- source: install.sh:60,1560,1726,1748 @1e5604c -->

**`~/.config/teaport/`**

The secrets directory, created with mode 700. The installer seeds `persona.md` when it is absent and never overwrites it, and keeps the `openclaw_token` and `discord_bot_token` files at mode 600.

<!-- source: install.sh:62,1416,1504,1761-1764,1925 @1e5604c -->

**`/var/lib/teaport/`**

Build and download space: the source checkouts, the `uv` download cache, and release archives. The installer creates it and hands it to the run user.

<!-- source: install.sh:61,528-529,657,726 @1e5604c -->

**`/usr/local/lib/teaport/`**

Root-owned helpers that run outside the brain venv, with one release directory per version and a symlink to the live one. The Wi-Fi setup helper and its captive DNS configuration are installed here.

<!-- source: install.sh:1026,1053,1101,2320 @1e5604c -->

Smaller system configuration lands outside these trees: a journald drop-in, a systemd-resolved drop-in, and the polkit rule for Wi-Fi setup.

<!-- source: install.sh:333,361,1165,2312-2317 @1e5604c -->

## systemd units

The installer renders the templates in `systemd/*.service.in` into `/etc/systemd/system/`.

<!-- source: install.sh:1538-1558 @1e5604c -->

**`teaport-engine.service`**

The speech engine: Voxtral for speech-to-text and Kokoro for text-to-speech. Rendered on every install and enabled.

<!-- source: install.sh:1775,1786;systemd/teaport-engine.service.in:5 @1e5604c -->

**`teaport-brain.service`**

The voice pipeline. The brain serves Talk and the phone line's front end. Rendered on every install and enabled.

<!-- source: install.sh:20,1776,1786;systemd/teaport-brain.service.in:6 @1e5604c -->

**`teaport-sandbox-recover.service`**

NemoClaw sandbox recovery at boot. Rendered and enabled only when the install has a NemoClaw sandbox.

<!-- source: install.sh:1777,1795;systemd/teaport-sandbox-recover.service.in:6 @1e5604c -->

**`teaport-discord-bridge.service`**

The Discord voice bridge. It runs on the host, owns the media leg, and pipes audio to the brain's `/talk` endpoint. The installer renders it when `TEAPORT_ENABLE_BRIDGE=1` or a bot token is already present, and enables it; the unit starts only after the guild, followed user, and token are configured.

<!-- source: install.sh:1896,1901,1949,1951-1959;systemd/teaport-discord-bridge.service.in:8 @1e5604c -->

**`teaport-sip.service`**

The SIP front end for phone calls. Rendered on every run, never enabled by the installer: the unit carries a `ConditionPathExists` on the SIP configuration, and `teaport sip configure` is what registers a trunk and turns it on. A repair re-renders the unit and leaves its enabled state alone. The gateway binary itself comes from the separate teaport-sip repository. While it renders the SIP unit, the installer also removes a leftover `teaport-sip-brain` unit from older installs.

<!-- source: install.sh:2041-2049,2255-2262,2267-2277;systemd/teaport-sip.service.in:13,17 @1e5604c -->

**`teaport-.service`**

The local microphone and speaker bridge for a USB mic array, piping audio to the brain's `/talk` endpoint. Rendered on every install but inert until `.env` exists. `TEAPORT_ENABLE_LOCAL_AUDIO=1` seeds that file and enables the unit; a repair without it leaves the enabled state alone, so a bridge the operator disabled stays off.

<!-- source: install.sh:2281-2285,2386,2405;systemd/teaport-.service.in:10,14 @1e5604c -->

**`teaport-wifi-setup.service`**

The Wi-Fi setup flow: a temporary setup network and a page where a phone enters the password. The brain starts it when the user asks for setup. Rendered when NetworkManager and iptables are present; never enabled by the installer.

<!-- source: install.sh:2286-2290,2305-2311;systemd/teaport-wifi-setup.service.in:25 @1e5604c -->

The OLED face is separate: with `TEAPORT_ENABLE_OLED_AVATAR=1`, the installer hands over to the avatar checkout's own `install.sh`, which installs `teaport-oled-avatar.service` and leaves it off when it finds no panel.

<!-- source: install.sh:2350-2354,2356 @1e5604c -->

The installer also enables `avahi-daemon` and `caddy`.

<!-- source: install.sh:1864 @1e5604c -->

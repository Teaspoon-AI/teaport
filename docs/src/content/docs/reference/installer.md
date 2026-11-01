---
title: Installer reference
description: Every flag, environment override, file, and systemd unit the teaport installer creates or manages.
---

The installer is the `install.sh` script at the root of the teaport repository. It installs the engine, the models, the brain, and the systemd units on a Jetson Orin running JetPack 7.2 with CUDA 13, as the release manifest describes. The OpenClaw plugin is optional. The installer is idempotent: rerunning it repairs the existing installation.

<!-- source: install.sh:3-7#50b8c301 -->

For a first install, follow [Install teaport](/getting-started/installation/). This page documents the installer itself.

## Two ways to run the installer

### The one-line installer

To install teaport without a checkout, run:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport)
```

That URL serves a bootstrap script. A piped script arrives as a single file, and the installer needs the whole repository, so the bootstrap makes a shallow clone of the teaport repository in a temporary directory, runs `install.sh` from the clone, and removes the clone when the installer exits. The bootstrap needs `git`; without it, it prints the `apt-get` command to run and stops.

<!-- source: install-bootstrap.sh:7-10,21-34#506e1330 -->

Two environment variables apply only to the one-line form:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEAPORT_REPO` | `https://github.com/Teaspoon-AI/teaport.git` | The Git URL to clone. |
| `TEAPORT_REF` | `main` | The branch or tag to install. |

<!-- source: install-bootstrap.sh:12-14,18-19#3a71b953 -->

Every other argument and environment variable passes through to `install.sh` unchanged:

```bash
bash <(curl -fsSL https://get.teaspoon.tech/teaport) --dry-run
```

<!-- source: install-bootstrap.sh:15,34#1517947f -->

### From a checkout

Clone the repository and run the installer from the clone:

```bash
git clone https://github.com/Teaspoon-AI/teaport.git
cd teaport
./install.sh
```

<!-- source: install-bootstrap.sh:18#cccc51ae -->

The checkout is the source: the brain, the plugin, and the unit templates come from the tree the script runs in.

<!-- source: install.sh:81-82#d3312094 -->

You do not need to be root. As a regular user, the installer runs each privileged command under `sudo`; as root, it runs the command directly.

<!-- source: install.sh:111-112#4a5756bf -->

## Command-line flags

The parser exits with status 2 on an unknown argument, a missing value, or `--only` together with `--rollback`. The two component flags also take an equals form, for example `--only=brain`, and the component must be `brain`.

<!-- source: install.sh:84-102#985ae33e -->

| Flag | Argument | What it does |
| --- | --- | --- |
| (none) | None | Installs or repairs everything: the engine, the models, the brain, the systemd units, and, when the install includes it, the OpenClaw plugin. |
| `--only` | `brain` | Builds a new brain venv from `brain/uv.lock` beside the live one, swaps it in, restarts the brain unit, and rolls back on its own if the brain does not come back. No EULA prompt, no system packages, no engine download, no unit or env rewrite. |
| `--rollback` | `brain` | Puts the previous brain venv back, with the teaport CLI and config helper it was built with. |
| `--dry-run` | None | Prints every action, privileged ones included, instead of running it. |
| `--accept-eula` | None | Accepts the engine license without the prompt, the flag form of `TEAPORT_ACCEPT_EULA=1`. |
| `-h`, `--help` | None | Prints the usage text from the header comment and exits. |

<!-- source: install.sh:9-33,88,109-112,207#f156f053 -->

A full install writes every unit and env file and restarts the engine and the brain in one step. It has no automatic rollback, but `--rollback brain` still works after it.

<!-- source: install.sh:27-31#b265cc25 -->

## Environment variables

The installer honors the overrides in this table; the default applies when the variable is unset. `TEAPORT_REPO` and `TEAPORT_REF` belong to the one-line form alone.

<!-- source: install.sh:35-54,65-77,248,1537,2122,2129,2577,2596-2597,2609,2624#d6d9b892 -->

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
| `TEAPORT_HOST` | `teaport.local`; `teaport-ab12.local` when another device already answers to that name | The front door's name. A re-run keeps the name an earlier run chose, unless another device answers to it. |
| `TEAPORT_TLS_CERT`, `TEAPORT_TLS_KEY` | The certificate an earlier run was given, else none | Your own certificate and key for the front door, in place of the device's own. Set both empty to go back to the device's own. See [Use your own certificate](#use-your-own-certificate). |
| `TEAPORT_REGENERATE_CA` | `0` | `1` replaces the device's own certificate authority, on every run where it is set. See [Replace the device's certificate](#replace-the-devices-certificate). |

<!-- source: install.sh:55-61,1712-1757#1a285422 -->

## The front door's certificate

The front door serves the browser page and the pairing links over HTTPS. By default
the certificate comes from the device's own certificate authority, which each phone
and laptop must trust one time. The authority is named for the device:
`Teaport ab12 Local Authority`, where `ab12` is the end of the device's Wi-Fi address.
A phone or laptop lists it with the year it was made, for example
`Teaport ab12 Local Authority - 2026 ECC Root`.

### Use your own certificate

If you have a certificate for the front door's name, from your own domain, a
reverse proxy, or Tailscale, the installer can serve it instead. Phones and laptops
then trust the front door without a step of their own, and the pairing pages leave
that step out.

1. Put the certificate, with its full chain, and its key in PEM files on the device.
   The `caddy` user must be able to read both.
2. Run the installer with the name and both paths:

   ```bash
   TEAPORT_HOST=teaport.example.com \
   TEAPORT_TLS_CERT=/etc/teaport/tls/fullchain.pem \
   TEAPORT_TLS_KEY=/etc/teaport/tls/key.pem \
   ./install.sh
   ```

The installer stops before it installs anything if a path is not absolute, the key
does not belong to the certificate or has a passphrase, or the certificate does not
cover the name. It stops later, after it installs Caddy, if the `caddy` user cannot read
the files. It warns when the certificate expires within 7 days. The pairing link must
fit the QR code on the device's screen: with a name longer than 36 characters, the
installer warns, and the device does not offer to pair a phone or laptop by voice.

A name that does not end in `.local` is not published over mDNS. Your DNS must resolve
it to the device.

A re-run keeps the certificate, so you set the variables one time. To go back to the
device's own certificate, run the installer with both set and empty:
`TEAPORT_TLS_CERT= TEAPORT_TLS_KEY= ./install.sh`.

Renewal is up to you. Replace both files, then reload Caddy:

```bash
sudo systemctl reload caddy.service
```

<!-- source: install.sh:55-61,1761-1786,1712-1757#9d8fc900 -->

### Replace the device's certificate

The device keeps its certificate authority across re-runs, so the phones and laptops
that trust it keep working. To make a new one, under the device's current name, run
the installer one time with `TEAPORT_REGENERATE_CA=1`; each run with it set makes
another new one. The installer keeps the old authority
and its certificates beside the new ones, with a `.bak-` and date suffix.

:::caution[Every phone and laptop must trust the new certificate]
After the replacement, no phone or laptop trusts the device until it installs and
trusts the new certificate. Remove the old entry from each one.
:::

<!-- source: install.sh:1792-1802#48dc5507 -->

## Files and directories

A full install creates the paths in this section. `TEAPORT_PREFIX`, `TEAPORT_ETC`, `TEAPORT_SECRETS_DIR`, and `TEAPORT_STATE` relocate the install root, the env directory, the secrets directory, and the state directory.

<!-- source: install.sh:66-69#6d18c51e -->

**`/opt/teaport/`**

The install root. It holds the `uv` binary, the model directories `models/voxtral/` and `models/kokoro/`, the `bridge/` tree, and the brain venvs.

<!-- source: install.sh:425,540,2161#8c7cbc17 -->

**`/opt/teaport/venv`, `venv.previous`, and `venvs/`**

Every install builds a new brain venv in `venvs/`, one per install. `venv` is a symlink to the live one, `venv.previous` is the one before it and the rollback target, and `venv.pre-uv` holds a pip-era venv kept for the same purpose. The newest three releases stay on the device, plus the live one and the rollback target.

<!-- source: install.sh:546-553,565-568,959-961,966#97116cdf -->

**`/etc/teaport/`**

The non-secret configuration. The installer writes `engine.env` and `brain.env` here, group-readable with mode 640.

<!-- source: install.sh:67,1580,1924,1952#2b2b7079 -->

**`~/.config/teaport/`**

The secrets directory, created with mode 700. The installer seeds `persona.md` when it is absent and never overwrites it, and keeps the `openclaw_token` and `discord_bot_token` files at mode 600.

<!-- source: install.sh:69,1423,1511,2144#c9741514; install.sh:1965-1970#a0504365 -->

**`/var/lib/teaport/`**

Build and download space: the source checkouts, the `uv` download cache, and release archives. The installer creates it and hands it to the run user.

<!-- source: install.sh:68,535-536,664,733#a52e2287 -->

**`/usr/local/lib/teaport/`**

Root-owned helpers that run outside the brain venv, with one release directory per version and a symlink to the live one. The Wi-Fi setup helper and its captive DNS configuration are installed here.

<!-- source: install.sh:1033,1060,1108,2541#3676d8bc -->

Smaller system configuration lands outside these trees: a journald drop-in, a systemd-resolved drop-in, and the polkit rule for Wi-Fi setup.

<!-- source: install.sh:340,368,1172,2533-2538#5c0253fa -->

## systemd units

The installer renders the templates in `systemd/*.service.in` into `/etc/systemd/system/`.

<!-- source: install.sh:1558-1578#026cfcf6 -->

**`teaport-engine.service`**

The speech engine: Voxtral for speech-to-text and Kokoro for text-to-speech. Rendered on every install and enabled.

<!-- source: install.sh:1981,1992#fc2fd203; systemd/teaport-engine.service.in:5#5e0b3478 -->

**`teaport-brain.service`**

The voice pipeline. The brain serves Talk and the phone line's front end. Rendered on every install and enabled.

<!-- source: install.sh:20,1982,1992#c8384c9b; systemd/teaport-brain.service.in:6#dd1fa334 -->

**`teaport-sandbox-recover.service`**

NemoClaw sandbox recovery at boot. Rendered and enabled only when the install has a NemoClaw sandbox.

<!-- source: install.sh:1983,2001#c4ac276c; systemd/teaport-sandbox-recover.service.in:6#32a2e52e -->

**`teaport-discord-bridge.service`**

The Discord voice bridge. It runs on the host, owns the media leg, and pipes audio to the brain's `/talk` endpoint. The installer renders it when `TEAPORT_ENABLE_BRIDGE=1` or a bot token is already present, and enables it; the unit starts only after the guild, followed user, and token are configured.

<!-- source: install.sh:2117,2122,2170,2172-2180#fb1ef353; systemd/teaport-discord-bridge.service.in:8#f85a03b3 -->

**`teaport-sip.service`**

The SIP front end for phone calls. Rendered on every run, never enabled by the installer: the unit carries a `ConditionPathExists` on the SIP configuration, and `teaport sip configure` is what registers a trunk and turns it on. A repair re-renders the unit and leaves its enabled state alone. The gateway binary itself comes from the separate teaport-sip repository. While it renders the SIP unit, the installer also removes a leftover `teaport-sip-brain` unit from older installs.

<!-- source: install.sh:2262-2270,2476-2483,2488-2498#e99260ed; systemd/teaport-sip.service.in:13,17#65131321 -->

**`teaport-local-audio.service`**

The local microphone and speaker bridge for a USB mic array, piping audio to the brain's `/talk` endpoint. Rendered on every install but inert until `.env` exists. `TEAPORT_ENABLE_LOCAL_AUDIO=1` seeds that file and enables the unit; a repair without it leaves the enabled state alone, so a bridge the operator disabled stays off.

<!-- source: install.sh:2502-2506,2607,2626#df1caeef; systemd/teaport-local-audio.service.in:10,14#e3a2bd5b -->

**`teaport-wifi-setup.service`**

The Wi-Fi setup flow: a temporary setup network and a page where a phone enters the password. The brain starts it when the user asks for setup. Rendered when NetworkManager and iptables are present; never enabled by the installer.

<!-- source: install.sh:2507-2511,2526-2532#141b4978; systemd/teaport-wifi-setup.service.in:25#d769a920 -->

The OLED face is separate: with `TEAPORT_ENABLE_OLED_AVATAR=1`, the installer hands over to the avatar checkout's own `install.sh`, which installs `teaport-oled-avatar.service` and leaves it off when it finds no panel.

<!-- source: install.sh:2571-2575,2577#ea4613d7 -->

The installer also enables `avahi-daemon` and `caddy`.

<!-- source: install.sh:2085#e72e75fe -->

---
title: teaport CLI reference
description: Reference for the teaport command and its status, doctor, update, licenses, key, setup-card, logs, and sip commands, with every option and exit behavior.
---

`teaport` is the operator command for a teaport device. It is a thin wrapper around systemd
and the release manifest, and it reads the layout the installer creates: `/opt/teaport`,
`/etc/teaport/*.env`, and `~/.config/teaport`.

<!-- source: cli/teaport:3-22#c8e492e7 -->

The `teaport-brain` package installs the command at `/usr/bin/teaport`. Run `teaport` with no arguments, or `teaport help`, to print the
command summary. An unknown command or option prints an error and exits 1.

<!-- source: install.sh:1177#9d1a0148; cli/teaport:41,1737-1751#c36cc6ea -->

The paths and ports in this reference are the defaults that the command reads at startup.
`TEAPORT_PREFIX`, `TEAPORT_ETC`, `TEAPORT_SECRETS_DIR`,
`TEAPORT_ENGINE_PORT`, and `TEAPORT_BRAIN_PORT` override them. The `TEAPORT_SIP_*` variables appear
in [teaport sip](#teaport-sip).

<!-- source: cli/teaport:25-37#1895e8cb -->

## teaport status

```text
teaport status
```

`teaport status` prints the state of the device at a glance. It shows the engine and brain
services, the `teaport-sandbox-recover` service when it is installed, what the brain's
recent requests for a reply met at the model provider, whether a newer release is out, the service ports, memory, and the
load average, and, on a device with OpenClaw, whether OpenClaw uses the brain's key. When the Session Initiation Protocol (SIP) gateway unit is enabled, it also shows the
`teaport-sip` service and the brain's SIP front-end. It takes no options.

<!-- source: cli/teaport:218-236#17a6a5ad -->

Example:

```bash
teaport status
```

The output is similar to the following:

```text
teaport — status
  teaport-engine               active
  teaport-brain                active
  answers (brain)              working
  port :8000 (engine)          listening
  port :7861 (brain /talk)     listening
  memory                       6.2Gi used, 1.1Gi avail / 7.4Gi
  load                         0.03 0.19 0.20
```

<!-- source: cli/teaport:230#a5805e19; cli/teaport:246-247#df9a8b07; cli/teaport:115-120#936558b1 -->

## teaport doctor

```text
teaport doctor [--fix]
```

`teaport doctor` runs the health sweep. It checks the install root, the engine binary and
models, the third-party notices file, the brain package against the checksums it shipped with (`dpkg -V`),
the `espeak-ng` dependency, the settings in `engine.env` and `brain.env`, the LLM API key,
both services, both service ports, the TTS pacing, and what the brain's recent requests for
a reply met. A key that the provider refused, or an account with no credit left, fails that
last check until a request goes through again. So does a brain with no endpoint or no key
at all, which says `not set up`. On the device's included trial credit, it also checks
what is left: a warning from half used on, and a failure after it has run out. Where OpenClaw shares the brain's key, it warns while OpenClaw waits for a restart to use a new key, and fails when a key change left OpenClaw and the brain on different keys. When telephony is on, it also checks
the SIP gateway binary and service, the brain's SIP front-end, the SIP source filter, and
the trunk registration. When it finds a NemoClaw sandbox record, it names the configured
sandbox. Each check prints one line: a check mark for pass, `!` for warning, and a cross
for failure.

<!-- source: cli/teaport:356-463#f0653a7f; cli/teaport:67-114,334-341#ce2e5130; cli/teaport:323-332#06ca79b5 -->

Option:

| Option | Description |
| --- | --- |
| `--fix` | Restarts what failed: a service that is not active, the SIP gateway when it is down or has not registered, and the brain when its SIP front-end is not connected. |

<!-- source: cli/teaport:394,450-455#b03ef28b -->

The final line is `doctor: all good` when every check passed, otherwise
`doctor: issues above`. The command exits 0 in the first case and 1 in the second; a warning
does not change the exit status.

<!-- source: cli/teaport:360-361,464-465#4c8069a5 -->

Example:

```bash
teaport doctor --fix
```

## teaport update

```text
teaport update --check [--json] [--channel {stable|beta}]
```

`teaport update --check` tells you whether a newer release is out. It reads the installed
manifest from `/opt/teaport/manifest.json`, downloads the manifest of the release channel,
and prints the installed value and the channel's value for the version, the engine
release, the brain tag, the plugin version, the OpenClaw version, and the model files. A
part the channel hasn't released yet (an empty value or `TODO`) isn't an update, and a new version name on its own isn't one either.

<!-- source: brain/teaport_brain/updates.py:50,139-141,144-171,296-309#99f3fd1c -->

| Option | Description |
| --- | --- |
| `--check` | Prints the comparison and stops. It changes nothing. |
| `--json` | Prints the result as one JSON object: the channel, the address, when it was read, and each part. |
| `--channel` | Looks at `stable` or `beta` for this run. |

<!-- source: cli/teaport:472-485#864e30aa; brain/teaport_brain/updates.py:310-346#ad4bdb28 -->

Without `--channel`, the command uses the `TEAPORT_UPDATE_CHANNEL` setting in
`/etc/teaport/brain.env`. When that is empty, it uses the channel the installed manifest
came from, and `stable` when that names none. `TEAPORT_MANIFEST_URL` replaces the address
for either channel. When nothing differs, the command prints `up to date.`. Otherwise it
prints `changed:` and the names of the parts. It exits 1 when the installed manifest is
missing or the channel's manifest can't be read.

<!-- source: brain/teaport_brain/updates.py:74-89,183-196,294-307,310-346#2249ccec -->

Without `--check`, the command doesn't apply the update: it prints the `install.sh`
command to run instead, `install.sh --only brain` for a brain-only change or
`install.sh --only engine` for an engine release, and exits 1.

<!-- source: cli/teaport:486-487#6b0371d2 -->

The brain also does this check by itself every day (the `TEAPORT_UPDATE_CHECK_SECS`
setting; `0` turns it off), and `teaport status` shows the result on a `release` line.

<!-- source: brain/teaport_brain/updates.py:199-220,269-291#f1d8d54b; cli/teaport:157-173#b622f9de -->

Example:

```bash
teaport update --check
```

## teaport licenses

```text
teaport licenses [PACKAGE | --notices] [--json]
```

`teaport licenses` shows the licenses of the software on the device. With no argument it
lists the Python packages in the brain with each one's version and license, and marks the
ones that are under the LGPL or bundle an LGPL library. With a package name it prints that package's license text, and
the name matches the way `pip` matches names, so case, `-`, `_`, and `.` do not matter.
`--notices` prints the notices instead: the third-party components and their licenses,
teaport's own license, and the engine's notices, for each of those that is on the device.
`--json` prints the list, or with `--notices` the documents, as JSON.

<!-- source: cli/teaport:6-9#df6fb355; brain/teaport_brain/licenses.py:155-214#a5fbe99f; brain/teaport_brain/licenses.py:110-152#a42773ed; brain/teaport_brain/licenses.py:33-36#09adb1b8 -->

The command runs under the brain's own Python environment, so it reads the same files as the
open-source licenses page that the configuration page links to (see [Open-source licenses](/reference/licenses/)). A package's text is the
license files in its `.dist-info` directory. A file longer than 256 KB is cut.

<!-- source: cli/teaport:490-498#3e591f85; brain/teaport_brain/licenses.py:29,38-45#8b2f47ba -->

The command exits with 0 when it shows something. It exits with 1, and says why, when the
brain has no package of that name, or when `--notices` finds no document, which is the case
for a brain that was not installed from the package. A package name together with `--notices`
is a usage error and exits with 2.

<!-- source: brain/teaport_brain/licenses.py:177-185,193-198#f6fff889 -->

## teaport key set

```text
teaport key set [--starter] [--new-key] [--json]
```

`teaport key set` gives the device an AI key from a terminal. It reads the key on standard
input, or asks for it without showing it when standard input is a terminal, so the key never
appears in a process listing. It checks the key with the model provider and saves it only if
the provider accepts it, the same way the setup page saves one: in the brain's key file and,
on a device with OpenClaw and an OpenRouter endpoint, in OpenClaw too. If OpenClaw doesn't
take the key, the brain keeps the key it had. In the rare case where OpenClaw might have taken
the key before it failed, `teaport status` says that the two might be on different keys until
the next save.

<!-- source: cli/teaport:518-539#7ceed65b; brain/teaport_brain/provision.py:145-194#4e21552e -->

The command runs with the brain's environment: the settings in `/etc/teaport/brain.env`,
your user's home directory from the system's user database, and systemd's default `PATH`.
It ignores the settings and `PATH` in your shell. If `brain.env` sets `LLM_API_KEY`, the brain
reads that copy first, so the command saves nothing and tells you to remove the line. Run the
command as the user the brain runs as, not as root. A running brain uses the new key from
the next conversation, without a restart.

<!-- source: brain/teaport_brain/provision.py:73-78,100-113#37151763; cli/teaport:500-516#21fa185b -->

| Option | Description |
| --- | --- |
| `--starter` | Marks the key as the device's included trial key, after the key is saved. The device then watches the key's credit and says when it runs low, until the owner connects an account of their own. Only for an OpenRouter endpoint. |
| `--new-key` | The provider made the key a moment ago. A refusal is asked again for a few seconds, because a new OpenRouter key can be refused briefly. |
| `--json` | Prints the result as one JSON object, with the credit left on the key when the provider says. The key is never printed. |

<!-- source: brain/teaport_brain/provision.py:145-194#4e21552e -->

The command exits with 0 when it saved the key, and with 3 when the provider refused it. A
key whose own credit limit is used up counts as refused. A key that works on an OpenRouter
account with no credit is saved, and the result's note says so. It exits with 2 when
it saved nothing for another reason: no answer from the provider, OpenClaw didn't take the
key, an empty line or text that isn't a key, `LLM_API_KEY` in `brain.env`, or a usage error.
It exits with 1 when it can't run (as root, as another user than the brain's, or with no
brain installed) or meets an error it doesn't expect.

A stop signal (`SIGTERM` or `SIGHUP`, for example when the SSH session drops) also exits with
2, and the message says whether the key was saved. A stop during the check with the provider
takes effect when the check ends, after at most about 10 seconds, or 16 with `--new-key`.
With `--starter`, exit 2 can also mean that the key was saved but couldn't be marked as the
trial key. In each of these cases, running the command again finishes the job.

<!-- source: brain/teaport_brain/provision.py:48-53,82-86,239-298#0574ef8b; cli/teaport:41,507-516#9aebeb27 -->

Example, with the key in a file:

```bash
teaport key set --starter < trial-key.txt
```

## teaport setup-card

```text
teaport setup-card [--new-password] [--json]
```

`teaport setup-card` prints the name of the device's setup network, its password, and the
text of its QR code, exactly as setup mode shows them, so you can print them on a card that
ships with the device. Without options, the password is the one fixed by
`WIFI_SETUP_PASSWORD` in `/etc/teaport/wifi-setup.env`. When no password is fixed, each setup
makes a new one, so there is nothing to print. The command exits with 0 when it prints a
card, and with 1 when it prints none: no password is fixed, setup mode doesn't accept the
fixed one, the device has no Wi-Fi, or `--new-password` couldn't install the new
file. Stopped by a signal, it prints no card and exits with another nonzero status.

<!-- source: cli/teaport:541-587#ca104852; brain/teaport_brain/provision.py:212-234#2bf0bf62; brain/teaport_brain/wifi_setup.py:177-192#73a41f3c -->

| Option | Description |
| --- | --- |
| `--new-password` | Makes a new password, in the same format as the ones setup mode makes, and fixes it in `wifi-setup.env`. The command keeps the file's other setup settings and comments, installs the file owned by root and readable by the group of the user who runs the command (mode 640, like the installer's other settings files), and prints the card only after the file is in place. It uses `sudo`. If the file holds a line that isn't a setup setting (`WIFI_SETUP_...`) or a comment, or a carriage return, the command changes nothing and exits 1; fix the file by hand. |
| `--json` | Prints the name, the password, and the QR code text as one JSON object. |

<!-- source: brain/teaport_brain/provision.py:199-209#f749e8ed; brain/teaport_brain/wifi_setup.py:201-213#67817dfa; cli/teaport:541-587#ca104852 -->

Example:

```bash
teaport setup-card --new-password
```

The output is similar to the following:

```text
Network   teaport-9e35
Password  k7mq-x4tn-p9wr
QR code   WIFI:T:WPA;S:teaport-9e35;P:k7mq-x4tn-p9wr;;
```

## teaport logs

```text
teaport logs [{engine|brain|sip|sip-brain}] [JOURNALCTL_OPTIONS]
```

`teaport logs` shows the systemd journal of one service. The first argument selects the
unit: `engine`, `brain` (the default), `sip`, or `sip-brain`, which also selects the brain
because the SIP front-end runs inside it. Every further argument is passed to `journalctl`
unchanged, so `-f`, `-n LINES`, and `--since` work directly. With no further arguments it
passes `-b`, so a bare `teaport logs` shows this boot's entries instead of the whole
journal.

<!-- source: cli/teaport:590-604#ec2c88d8 -->

The command exits with the exit status of `journalctl`.

<!-- source: cli/teaport:604#2240fe1f -->

Example:

```bash
teaport logs engine -f
```

## teaport sip

`teaport sip` controls the opt-in telephony: phone calls over SIP. Telephony is off on a
fresh install; `teaport sip configure` turns it on and `teaport sip disable` turns it off.

<!-- source: cli/teaport:1718-1731#41b599bf -->

The wizard writes the gateway configuration to `~/.config/teaport/teaport-sip.conf`. The
gateway binary is `~/teaport-sip/build/teaport-sip`, and the gateway and the brain's SIP
front-end meet on the `/run/teaport/teaport-sip.sock` socket.
`TEAPORT_SIP_CONF`, `TEAPORT_SIP_GATEWAY`, and `TEAPORT_SIP_UDS` override those paths.

<!-- source: cli/teaport:35-37#8d57683f -->

The subcommands are `configure`, `status`, `aec`, `restart`, and `disable`. For a walk
through of the setup, see [Set up phone calls](/guides/phone-calls/).

<!-- source: cli/teaport:1711-1734#f2927b40 -->

### teaport sip configure

```text
teaport sip configure [--host H --domain D --user U --password P] [--yes|-y] [--fresh]
teaport sip configure --conf FILE
```

The wizard that turns telephony on. It asks for the registrar or session border controller
(SBC) host, the SIP domain, the username, and the password, and the domain defaults to the
host. A re-run keeps the settings of the installed configuration that it does not set;
`--fresh` keeps nothing and writes the template. It test-registers before it writes
anything: if the registration fails, the configuration is not written and the units are
left as they were.

<!-- source: cli/teaport:1474-1541#135300db -->

On success it writes the configuration with mode 600, enables and restarts the gateway, and
prints `SIP enabled — telephony is on. Check: teaport sip status`. It then waits up to
20 seconds for the first registration; a missing registration does not fail the command.

<!-- source: cli/teaport:1468-1471,1540#30407607 -->

With `--conf FILE` it adopts an existing gateway configuration instead of asking. The file
must be readable and must set `id_uri`, `registrar_uri`, `username`, and `password`. The
`sip_port`, `uds_path`, `register`, and `auto_answer` keys are forced to the values the
services need, each change prints a note, and the result is test-registered exactly like
the wizard.

<!-- source: cli/teaport:1556-1566,1588-1608#db00ef74 -->

| Option | Description |
| --- | --- |
| `--host H` | The registrar or SBC host. Prompts for it when missing; required with `--yes`. |
| `--domain D` | The SIP domain. Defaults to the host. |
| `--user U` | The SIP username. Prompts for it when missing; required with `--yes`. |
| `--password P` | The SIP password. Prompts, without echo, when missing; required with `--yes`. |
| `--yes`, `-y` | Headless mode: no prompts. Requires `--host`, `--user`, and `--password`. |
| `--fresh` | Wizard only: keep none of the installed configuration and write the template. |
| `--conf FILE` | Adopt this gateway configuration instead of running the wizard. |
| `-h`, `--help` | Print the usage. |

<!-- source: cli/teaport:1475-1494,1502-1519#a33788a9 -->

The command exits 1 and leaves the installed configuration, if any, unchanged when a flag
is unknown, an argument is missing, a check refuses the install, the gateway can't finish
its check of the configuration, the test registration fails, or the configuration file
changes while the test runs.

<!-- source: cli/teaport:1462-1467,1489,1510-1536#b5fa4053 -->

Example:

```bash
teaport sip configure --host SIP_HOST --user SIP_USER --password SIP_PASSWORD --yes
```

`SIP_HOST` is the registrar or SBC host, and `SIP_USER` and `SIP_PASSWORD` are the trunk
credentials. The domain defaults to the host; add `--domain` to set it.

### teaport sip status

```text
teaport sip status
```

Prints the telephony picture: the `teaport-sip` service state, the brain's SIP front-end
state, and, when the configuration exists, its path, the registrar and username it names,
the `aec` and `aec_tail_ms` values, the source filter state, the `auto_answer` value, and
this run's registration outcome. Without a configuration it prints `config` as `absent`
with the configure command, and it still reports the gateway's registration attempt from
the journal. It takes no options.

<!-- source: cli/teaport:1668-1695#e661c53b -->

Example:

```bash
teaport sip status
```

### teaport sip aec

```text
teaport sip aec [on|off]
```

Shows or flips the gateway's echo canceller. With no argument it prints the current `aec`
and `aec_tail_ms` values and exits. `on` (or `true`, or `1`) and `off` (or `false`, or `0`)
rewrite the `aec` key in place, print the old and new value, and restart the gateway, which
reads the setting at startup. The restart preconditions are checked before the file is
touched.

<!-- source: cli/teaport:1641-1665#2fb69cec -->

The command exits 1 when the argument is none of the accepted values, the telephony
preconditions fail, or the rewrite fails; a failed rewrite leaves `aec` as it was.

<!-- source: cli/teaport:1628-1632,1656,1660-1662#8a3e62a5 -->

Example:

```bash
teaport sip aec off
```

### teaport sip restart

```text
teaport sip restart
```

Restarts the gateway. It requires the configuration, the installed units, and telephony
on, and exits with an error that names the missing piece otherwise. The brain's SIP
front-end reconnects to the gateway on its own; the brain service, and any Talk session,
is not touched. The command then waits up to 20 seconds for a fresh registration, and a
missing one does not fail it. It takes no options.

<!-- source: cli/teaport:1622-1639,1729#3bec60db -->

Example:

```bash
teaport sip restart
```

### teaport sip disable

```text
teaport sip disable [--purge]
```

Stops and disables the `teaport-sip` service and prints
`SIP gateway disabled + stopped`; the brain's SIP front-end then waits, idle. The
configuration is kept and the command says where. Afterwards, `teaport status` and
`teaport doctor` report telephony as off by choice.

<!-- source: cli/teaport:414-415,1698-1707#2f9df694 -->

| Option | Description |
| --- | --- |
| `--purge` | Also deletes the configuration file and prints its path. |

<!-- source: cli/teaport:1699,1702-1703#15f03923 -->

A failing `systemctl` call does not fail the command.

<!-- source: cli/teaport:1700#adf95939 -->

Example:

```bash
teaport sip disable --purge
```

---
title: teaport CLI reference
description: Reference for the teaport command and its status, doctor, update, licenses, logs, and sip commands, with every option and exit behavior.
---

`teaport` is the operator command for a teaport device. It is a thin wrapper around systemd
and the release manifest, and it reads the layout the installer creates: `/opt/teaport`,
`/etc/teaport/*.env`, and `~/.config/teaport`.

<!-- source: cli/teaport:3-17#f31d2622 -->

The `teaport-brain` package installs the command at `/usr/bin/teaport`. Run `teaport` with no arguments, or `teaport help`, to print the
command summary. An unknown command or option prints an error and exits 1.

<!-- source: install.sh:1177#9d1a0148; cli/teaport:36,1643-1655#ac17e130 -->

The paths and ports in this reference are the defaults that the command reads at startup.
`TEAPORT_PREFIX`, `TEAPORT_ETC`, `TEAPORT_SECRETS_DIR`,
`TEAPORT_ENGINE_PORT`, and `TEAPORT_BRAIN_PORT` override them. The `TEAPORT_SIP_*` variables appear
in [teaport sip](#teaport-sip).

<!-- source: cli/teaport:20-32#1895e8cb -->

## teaport status

```text
teaport status
```

`teaport status` prints the state of the device at a glance. It shows the engine and brain
services, the `teaport-sandbox-recover` service when it is installed, what the brain's
recent requests for a reply met at the model provider, whether a newer release is out, the service ports, memory, and the
load average, and, on a device with OpenClaw, whether OpenClaw uses the brain's key. When the Session Initiation Protocol (SIP) gateway unit is enabled, it also shows the
`teaport-sip` service and the brain's SIP front-end. It takes no options.

<!-- source: cli/teaport:213-231#17a6a5ad -->

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

<!-- source: cli/teaport:225#a5805e19; cli/teaport:241-242#df9a8b07; cli/teaport:110-115#936558b1 -->

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

<!-- source: cli/teaport:351-458#f0653a7f; cli/teaport:62-109,329-336#ce2e5130; cli/teaport:318-327#06ca79b5 -->

Option:

| Option | Description |
| --- | --- |
| `--fix` | Restarts what failed: a service that is not active, the SIP gateway when it is down or has not registered, and the brain when its SIP front-end is not connected. |

<!-- source: cli/teaport:389,445-450#b03ef28b -->

The final line is `doctor: all good` when every check passed, otherwise
`doctor: issues above`. The command exits 0 in the first case and 1 in the second; a warning
does not change the exit status.

<!-- source: cli/teaport:355-356,459-460#4c8069a5 -->

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

<!-- source: cli/teaport:467-480#864e30aa; brain/teaport_brain/updates.py:310-346#ad4bdb28 -->

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

<!-- source: cli/teaport:481-482#6b0371d2 -->

The brain also does this check by itself every day (the `TEAPORT_UPDATE_CHECK_SECS`
setting; `0` turns it off), and `teaport status` shows the result on a `release` line.

<!-- source: brain/teaport_brain/updates.py:199-220,269-291#f1d8d54b; cli/teaport:152-168#b622f9de -->

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
two that are under the LGPL. With a package name it prints that package's license text, and
the name matches the way `pip` matches names, so case, `-`, `_`, and `.` do not matter.
`--notices` prints the notices instead: the third-party components and their licenses,
teaport's own license, and the engine's notices, for each of those that is on the device.
`--json` prints the list, or with `--notices` the documents, as JSON.

<!-- source: cli/teaport:6-9#df6fb355; brain/teaport_brain/licenses.py:125-184#51ed793b; brain/teaport_brain/licenses.py:96-122#1e0fcef5; brain/teaport_brain/licenses.py:33-36#09adb1b8 -->

The command runs under the brain's own Python environment, so it reads the same files as the
open-source licenses page that the configuration page links to (see [Open-source licenses](/reference/licenses/)). A package's text is the
license files in its `.dist-info` directory. A file longer than 256 KB is cut.

<!-- source: cli/teaport:485-493#3e591f85; brain/teaport_brain/licenses.py:29,38-45#8b2f47ba -->

The command exits with 0 when it shows something. It exits with 1, and says why, when the
brain has no package of that name, or when `--notices` finds no document, which is the case
for a brain that was not installed from the package. A package name together with `--notices`
is a usage error and exits with 2.

<!-- source: brain/teaport_brain/licenses.py:147-157,163-168#ed74749c -->

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

<!-- source: cli/teaport:496-510#ec2c88d8 -->

The command exits with the exit status of `journalctl`.

<!-- source: cli/teaport:510#2240fe1f -->

Example:

```bash
teaport logs engine -f
```

## teaport sip

`teaport sip` controls the opt-in telephony: phone calls over SIP. Telephony is off on a
fresh install; `teaport sip configure` turns it on and `teaport sip disable` turns it off.

<!-- source: cli/teaport:1624-1637#41b599bf -->

The wizard writes the gateway configuration to `~/.config/teaport/teaport-sip.conf`. The
gateway binary is `~/teaport-sip/build/teaport-sip`, and the gateway and the brain's SIP
front-end meet on the `/run/teaport/teaport-sip.sock` socket.
`TEAPORT_SIP_CONF`, `TEAPORT_SIP_GATEWAY`, and `TEAPORT_SIP_UDS` override those paths.

<!-- source: cli/teaport:30-32#8d57683f -->

The subcommands are `configure`, `status`, `aec`, `restart`, and `disable`. For a walk
through of the setup, see [Set up phone calls](/guides/phone-calls/).

<!-- source: cli/teaport:1617-1640#f2927b40 -->

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

<!-- source: cli/teaport:1380-1447#135300db -->

On success it writes the configuration with mode 600, enables and restarts the gateway, and
prints `SIP enabled — telephony is on. Check: teaport sip status`. It then waits up to
20 seconds for the first registration; a missing registration does not fail the command.

<!-- source: cli/teaport:1374-1377,1446#30407607 -->

With `--conf FILE` it adopts an existing gateway configuration instead of asking. The file
must be readable and must set `id_uri`, `registrar_uri`, `username`, and `password`. The
`sip_port`, `uds_path`, `register`, and `auto_answer` keys are forced to the values the
services need, each change prints a note, and the result is test-registered exactly like
the wizard.

<!-- source: cli/teaport:1462-1472,1494-1514#db00ef74 -->

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

<!-- source: cli/teaport:1381-1400,1408-1425#a33788a9 -->

The command exits 1 and leaves the installed configuration, if any, unchanged when a flag
is unknown, an argument is missing, a check refuses the install, the gateway can't finish
its check of the configuration, the test registration fails, or the configuration file
changes while the test runs.

<!-- source: cli/teaport:1368-1373,1395,1416-1442#b5fa4053 -->

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

<!-- source: cli/teaport:1574-1601#e661c53b -->

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

<!-- source: cli/teaport:1547-1571#2fb69cec -->

The command exits 1 when the argument is none of the accepted values, the telephony
preconditions fail, or the rewrite fails; a failed rewrite leaves `aec` as it was.

<!-- source: cli/teaport:1534-1538,1562,1566-1568#8a3e62a5 -->

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

<!-- source: cli/teaport:1528-1545,1635#3bec60db -->

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

<!-- source: cli/teaport:409-410,1604-1613#2f9df694 -->

| Option | Description |
| --- | --- |
| `--purge` | Also deletes the configuration file and prints its path. |

<!-- source: cli/teaport:1605,1608-1609#15f03923 -->

A failing `systemctl` call does not fail the command.

<!-- source: cli/teaport:1606#adf95939 -->

Example:

```bash
teaport sip disable --purge
```

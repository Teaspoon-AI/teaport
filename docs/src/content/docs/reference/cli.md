---
title: teaport CLI reference
description: Reference for the teaport command and its status, doctor, update, logs, and sip commands, with every option and exit behavior.
---

`teaport` is the operator command for a teaport device. It is a thin wrapper around systemd
and the release manifest, and it reads the layout the installer creates: `/opt/teaport`,
`/etc/teaport/*.env`, and `~/.config/teaport`.

<!-- source: cli/teaport:3-13#43660e14 -->

The installer puts the command at `/usr/local/bin/teaport`. Run `teaport` with no arguments, or `teaport help`, to print the
command summary. An unknown command or option prints an error and exits 1.

<!-- source: install.sh:1018,1027#738c080b; cli/teaport:33,1383-1393#0144faa8 -->

The paths and ports in this reference are the defaults that the command reads at startup.
`TEAPORT_PREFIX`, `TEAPORT_ETC`, `TEAPORT_SECRETS_DIR`, `TEAPORT_MANIFEST_URL`,
`TEAPORT_ENGINE_PORT`, and `TEAPORT_BRAIN_PORT` override them. The `TEAPORT_SIP_*` variables appear
in [teaport sip](#teaport-sip).

<!-- source: cli/teaport:17-29#d2007d79 -->

## teaport status

```text
teaport status
```

`teaport status` prints the state of the device at a glance. It shows the engine and brain
services, the `teaport-sandbox-recover` service when it is installed, the service ports,
memory, and the load average. When the Session Initiation Protocol (SIP) gateway unit is enabled, it also shows the
`teaport-sip` service and the brain's SIP front-end. It takes no options.

<!-- source: cli/teaport:84-97#d8be9058 -->

Example:

```bash
teaport status
```

The output is similar to the following:

```text
teaport — status
  teaport-engine               active
  teaport-brain                active
  port :8000 (engine)          listening
  port :7861 (brain /talk)     listening
  memory                       6.2Gi used, 1.1Gi avail / 7.4Gi
  load                         0.03 0.19 0.20
```

<!-- source: cli/teaport:85#a5805e19; cli/teaport:95-96#df9a8b07 -->

## teaport doctor

```text
teaport doctor [--fix]
```

`teaport doctor` runs the health sweep. It checks the install root, the engine binary and
models, the third-party notices file, the brain venv against its recorded lock file build,
the `espeak-ng` dependency, the settings in `engine.env` and `brain.env`, the LLM API key,
both services, both service ports, and the TTS pacing. When telephony is on, it also checks
the SIP gateway binary and service, the brain's SIP front-end, the SIP source filter, and
the trunk registration. When it finds a NemoClaw sandbox record, it names the configured
sandbox. Each check prints one line: a check mark for pass, `!` for warning, and a cross
for failure.

<!-- source: cli/teaport:176-277#79e1b43b -->

Option:

| Option | Description |
| --- | --- |
| `--fix` | Restarts what failed: a service that is not active, the SIP gateway when it is down or has not registered, and the brain when its SIP front-end is not connected. |

<!-- source: cli/teaport:209,262-267#b03ef28b -->

The final line is `doctor: all good` when every check passed, otherwise
`doctor: issues above`. The command exits 0 in the first case and 1 in the second; a warning
does not change the exit status.

<!-- source: cli/teaport:180-181,276-277#4c8069a5 -->

Example:

```bash
teaport doctor --fix
```

## teaport update

```text
teaport update [--check]
```

`teaport update` compares the installed release with the release manifest. It reads the
installed manifest from `/opt/teaport/manifest.json` and downloads the release manifest
from `TEAPORT_MANIFEST_URL`. For the engine release, the brain tag, and the plugin version,
it prints the installed value and the remote value.

<!-- source: cli/teaport:281-301#a6e768ff -->

| Option | Description |
| --- | --- |
| `--check` | Prints the comparison and stops. It changes nothing. |

<!-- source: cli/teaport:282,304#c717993d -->

When nothing differs, it prints `up to date.` and exits 0. With differences, `--check` also
prints `(--check) not applying.` and exits 0. Without `--check`, the command does not apply
the update: it prints the `install.sh` command to run instead, `install.sh --only brain`
for a brain-only change, and exits 1. It also exits 1 when the installed manifest is
missing, the download fails, or the downloaded manifest is not valid JSON.

<!-- source: cli/teaport:76-78,284,302-309#0eba0883 -->

Example:

```bash
teaport update --check
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

<!-- source: cli/teaport:313-327#ec2c88d8 -->

The command exits with the exit status of `journalctl`.

<!-- source: cli/teaport:327#2240fe1f -->

Example:

```bash
teaport logs engine -f
```

## teaport sip

`teaport sip` controls the opt-in telephony: phone calls over SIP. Telephony is off on a
fresh install; `teaport sip configure` turns it on and `teaport sip disable` turns it off.

<!-- source: cli/teaport:1364-1377#41b599bf -->

The wizard writes the gateway configuration to `~/.config/teaport/teaport-sip.conf`. The
gateway binary is `~/teaport-sip/build/teaport-sip`, and the gateway and the brain's SIP
front-end meet on the `/run/teaport/teaport-sip.sock` socket.
`TEAPORT_SIP_CONF`, `TEAPORT_SIP_GATEWAY`, and `TEAPORT_SIP_UDS` override those paths.

<!-- source: cli/teaport:27-29#8d57683f -->

The subcommands are `configure`, `status`, `aec`, `restart`, and `disable`. For a walk
through of the setup, see [Set up phone calls](/guides/phone-calls/).

<!-- source: cli/teaport:1357-1380#f2927b40 -->

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

<!-- source: cli/teaport:1120-1187#135300db -->

On success it writes the configuration with mode 600, enables and restarts the gateway, and
prints `SIP enabled — telephony is on. Check: teaport sip status`. It then waits up to
20 seconds for the first registration; a missing registration does not fail the command.

<!-- source: cli/teaport:1114-1117,1186#30407607 -->

With `--conf FILE` it adopts an existing gateway configuration instead of asking. The file
must be readable and must set `id_uri`, `registrar_uri`, `username`, and `password`. The
`sip_port`, `uds_path`, `register`, and `auto_answer` keys are forced to the values the
services need, each change prints a note, and the result is test-registered exactly like
the wizard.

<!-- source: cli/teaport:1202-1212,1234-1254#db00ef74 -->

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

<!-- source: cli/teaport:1121-1140,1148-1165#a33788a9 -->

The command exits 1 and leaves the installed configuration, if any, unchanged when a flag
is unknown, an argument is missing, a check refuses the install, the test registration
fails, or the configuration file changes while the test runs.

<!-- source: cli/teaport:1109-1113,1135,1156-1182#0e91cf1b -->

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

<!-- source: cli/teaport:1314-1341#e661c53b -->

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

<!-- source: cli/teaport:1287-1311#2fb69cec -->

The command exits 1 when the argument is none of the accepted values, the telephony
preconditions fail, or the rewrite fails; a failed rewrite leaves `aec` as it was.

<!-- source: cli/teaport:1274-1278,1302,1306-1308#8a3e62a5 -->

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

<!-- source: cli/teaport:1268-1285,1375#3bec60db -->

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

<!-- source: cli/teaport:226-227,1344-1353#2f9df694 -->

| Option | Description |
| --- | --- |
| `--purge` | Also deletes the configuration file and prints its path. |

<!-- source: cli/teaport:1345,1348-1349#15f03923 -->

A failing `systemctl` call does not fail the command.

<!-- source: cli/teaport:1346#adf95939 -->

Example:

```bash
teaport sip disable --purge
```

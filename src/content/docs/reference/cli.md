---
title: teaport CLI
description: The teaport command — status, doctor, update, and logs.
---

The installer puts the `teaport` command in `/usr/local/bin/teaport`. It is a small program
that reads the layout the installer made. It does not need the internet, except for
`teaport update`.

<!-- source: teaport repo cli/teaport -->

## `teaport status`

Shows the services, the ports, the memory, and the load.

```
teaport — status
  teaport-engine               active
  teaport-brain                active
  port :8000 (engine)          listening
  port :7861 (brain /talk)     listening
  memory                       6.2Gi used, 1.1Gi avail / 7.4Gi total
  load                         0.03 0.19 0.20
```

Use this command first. It answers one question: are the parts running?

## `teaport doctor`

Runs a health check of 14 tests. Use it at any time, not only after an installation.

```
teaport — doctor
  ✓ install root /opt/teaport
  ✓ engine binary
  ✓ STT model
  ✓ Kokoro TTS model
  ✓ brain (teaport-brain)
  ✓ espeak-ng (engine G2P dep)
  ✓ KOKORO_RESERVE_FPT=6
  ✓ LLM_BASE_URL set
  ✓ GATEWAY_TOKEN set
  ✓ LLM API key present
  ✓ teaport-engine active
  ✓ teaport-brain active
  ✓ engine :8000 listening
  ✓ brain :7861 listening

doctor: all good
```

| Option | Effect |
|---|---|
| `--fix` | Restarts any service that is not running |

:::caution[`doctor` does not check speech quality]
`doctor` checks that files exist and that services run. It does not check that the engine
chose the fast kernel or that it loaded the pronunciation dictionary. A device can pass
every test and still be slow or sound wrong. See
[Troubleshooting](/guides/troubleshooting/).
:::

## `teaport update`

Compares the installed files against the release manifest and installs what changed.

| Option | Effect |
|---|---|
| `--check` | Shows the difference. Changes nothing. |

## `teaport logs`

Shows the log of a service. It is a small program around `journalctl`.

```bash
teaport logs            # the brain (the default)
teaport logs engine     # the engine
teaport logs brain -f   # follow the brain's log
teaport logs engine -n 200
```

Options after the service name go to `journalctl`. Use `-f` to follow and `-n` to set the
number of lines.

## Where the files are

| Path | Contents |
|---|---|
| `/opt/teaport/bin/` | the engine and the pronunciation dictionary |
| `/opt/teaport/models/` | the speech recognition and speech synthesis models |
| `/opt/teaport/venv/` | the brain's Python environment |
| `/opt/teaport/manifest.json` | the release that is installed |
| `/etc/teaport/engine.env` | engine settings (not secret) |
| `/etc/teaport/brain.env` | brain settings (not secret) |
| `~/.config/teaport/` | your secrets: `llm_key`, `openclaw_token`, `persona.md` |
| `/var/lib/teaport/` | the record of your license acceptance |

Secrets are never written into the service files or into the images.

## Environment variables

These change where the `teaport` command looks. They are for development and for tests. You
do not need them for a normal installation.

| Variable | Default |
|---|---|
| `TEAPORT_PREFIX` | `/opt/teaport` |
| `TEAPORT_ETC` | `/etc/teaport` |
| `TEAPORT_SECRETS_DIR` | `~/.config/teaport` |
| `TEAPORT_MANIFEST_URL` | `https://get.teaspoon.tech/manifest/stable.json` |
| `TEAPORT_ENGINE_PORT` | `8000` |
| `TEAPORT_BRAIN_PORT` | `7861` |

## Related

- [Troubleshooting](/guides/troubleshooting/)
- [Configuration](/guides/configuration/)
- [Architecture](/reference/architecture/)

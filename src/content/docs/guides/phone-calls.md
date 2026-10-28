---
title: Set up phone calls
description: Answer phone calls on your teaport device over a SIP trunk, through the teaport-sip gateway and the brain's SIP front-end.
---

With a Session Initiation Protocol (SIP) trunk or a session border controller
(SBC) account, the device answers real phone calls. The brain's SIP front-end
answers the call, taking it from a separate gateway service, `teaport-sip`.

<!-- source: docs/CONFIG.md:370-375 @1e5604c -->

## Before you begin

You need:

- A device with teaport installed. See [Install teaport](/getting-started/installation/).
- A SIP trunk or SBC account, with its host, domain,
  username, and password.
- The gateway program. The installer creates the `teaport-sip` service, but it
  does not build or install the gateway: that is a separate program in its own
  repository, and the service runs `~/teaport-sip/build/teaport-sip`.
- Access to the device as the user the teaport services run as.

<!-- source: install.sh:2255-2256; cli/teaport:27-29 @1e5604c -->

## Turn on telephony

Telephony ships off. The installer lays down the `teaport-sip` service, gated on
a configuration file that does not exist, so nothing starts and no SIP port is
bound. The `teaport sip configure` wizard turns it on.

<!-- source: systemd/teaport-sip.service.in:4,17 @1e5604c -->

To configure the line, follow these steps:

1. On the device, start the wizard:

   ```bash
   teaport sip configure
   ```

2. Answer its prompts: the registrar or SBC host, the domain, the username, and
   the password.

3. Wait for the test registration. The wizard registers from a throwaway port,
   never port 5060, so it leaves a running gateway untouched. When the
   trunk answers `200 OK`, the wizard writes
   `~/.config/teaport/teaport-sip.conf` with mode `600`, which holds the SIP
   password, and enables the service. If the registration fails, the wizard
   writes nothing and telephony stays off.

<!-- source: docs/CONFIG.md:385-389; cli/teaport:330-338 @1e5604c -->

Run the wizard as the user the services run as. As root, add `sudo -E`: a plain
`sudo` resets `HOME`, so the wizard writes a configuration the gateway never
reads, and it refuses to.

<!-- source: docs/CONFIG.md:389-391 @1e5604c -->

### Turn it on without prompts

To pass the answers on the command line, for example from a script:

```bash
teaport sip configure --host SBC_HOST --domain SIP_DOMAIN --user SIP_USER \
    --password SIP_PASSWORD --yes
```

Replace the following:

- `SBC_HOST`: the registrar or SBC host.
- `SIP_DOMAIN`: the SIP domain of the trunk.
- `SIP_USER`: the username the trunk assigns you.
- `SIP_PASSWORD`: the trunk password.

<!-- source: cli/teaport:1124-1131; docs/CONFIG.md:381-383 @1e5604c -->

### Use an existing gateway configuration

To adopt a gateway `.conf` that a device ran by hand, or one carried over from
another device:

```bash
teaport sip configure --conf CONF_PATH
```

Replace `CONF_PATH` with the file to adopt. The wizard test-registers through
the same path, then installs the file as it is, except for four keys the
services own: `sip_port` becomes 5060, `uds_path` becomes the socket the brain
listens on, `register` becomes true, and `auto_answer` becomes false, because
the brain answers the call, after a couple of rings. If a gateway or SIP
front-end launched by hand is still running, the command refuses and names it:
the service it is about to enable needs port 5060 and the socket.

<!-- source: docs/CONFIG.md:420-426 @1e5604c -->

### Change the trunk later

A re-run, for a new password or a new registrar, rewrites only the keys the
wizard owns: the trunk's `id_uri`, `registrar_uri`, `username`, `password`, and
`realm`, and the four service keys. Every other line of the installed
configuration survives: the source filter, `sip_source_filter` with its
`sip_source_allow` list, `nameserver`, `aec`, comments, and keys the wizard
does not know. Before the test registration, the wizard lists by name the
settings it keeps. To keep nothing and write a fresh template, run
`teaport sip configure --fresh`.

<!-- source: docs/CONFIG.md:395-411; cli/teaport:347-350 @1e5604c -->

## Check the line

`teaport sip status` reports the gateway service, the brain's SIP front-end, the
configuration, including whether the running gateway confirms the source filter
in it, and the registration from this run:

```bash
teaport sip status
```

<!-- source: docs/CONFIG.md:390-393 @1e5604c -->

To follow the gateway's journal:

```bash
teaport logs sip -f
```

The call's own lines are in the brain's journal, `teaport logs brain`.

<!-- source: docs/CONFIG.md:441 @1e5604c -->

## Keep the line running

The line survives reboots and crashes on its own. The service is enabled and
restarts with a backoff that does not hammer the registrar, and the brain
reconnects to the gateway whenever the gateway comes back. The two restart
independently: a brain restart, from an update or a configuration change, leaves
the call at the gateway, which hands it to the restarted brain, and the caller
hears a short "sorry, I lost you for a moment". The brain picks up a restarted
gateway within a couple of seconds.

<!-- source: docs/CONFIG.md:428-434; systemd/teaport-sip.service.in:38,47,49 @1e5604c -->

To restart the gateway, for example after editing its configuration by hand:

```bash
teaport sip restart
```

The command waits for the trunk's `200 OK`; the brain reconnects on its own.

<!-- source: docs/CONFIG.md:437 @1e5604c -->

The gateway cancels echo for the caller. `teaport sip aec` prints the current
acoustic echo cancellation (AEC) setting:

```bash
teaport sip aec
```

The flags `teaport sip aec off` and `teaport sip aec on` flip `aec=` in the
configuration and restart the gateway:

```bash
teaport sip aec off
teaport sip aec on
```

<!-- source: docs/CONFIG.md:438-440 @1e5604c -->

To turn telephony off:

```bash
teaport sip disable
```

The command stops and disables the gateway. The configuration stays at
`~/.config/teaport/teaport-sip.conf`; add `--purge` to remove it too. Run the
wizard again to turn telephony back on.

<!-- source: cli/teaport:1344-1351,1376 @1e5604c -->

The configuration is the gateway's own `key=value` format: `registrar_uri`,
`id_uri`, `username`, `password`, `uds_path`, `aec`, `auto_answer`, and more.
Editing it by hand is fine; run `teaport sip restart` afterwards. Nothing about
the line lives in `/tmp`: the configuration is in `~/.config/teaport`, the
socket in `/run/teaport`, and the logs in journald.

<!-- source: docs/CONFIG.md:445-449 @1e5604c -->

## A call during a conversation

The brain runs every front-end in one process, and the engine serves one
conversation at a time; the session arbiter decides which one. A phone call and
a Talk session are peers there. See [Architecture](/reference/architecture/).

<!-- source: docs/CONFIG.md:602-604; brain/teaport_brain/sip_server.py:5-6 @1e5604c -->

The brain answers the call, not the gateway. The delay is a setting,
`SIP_ANSWER_AFTER_SECS`, a couple of rings, and the brain builds the call's
pipeline and words its greeting during the ring, so the greeting plays as
soon as the caller is connected. See [the settings reference](/reference/settings/#sip-front-end).

<!-- source: docs/CONFIG.md:618-623 @1e5604c -->

While the call rings, the agent asks the live conversation, in the
conversation's language: *"Someone's calling me — +1 346 234 8500. Should I
step away for a moment?"* When the call carries no name or number, the agent
asks *"Someone's calling me. Should I step away…"*.

The question has three outcomes:

- **Yes**: the agent says *"I'll take the call — back in a moment."* The
  conversation goes on hold: its connection and everything said in it are kept,
  and it hears nothing while the call is up. After the call, the conversation
  resumes with *"Sorry about that — where were we?"*
- **No**: the call is never answered. The caller hears it ring until they give
  up.
- **No answer**: after `TEAPORT_CALL_PROMPT_SECS`, 12 seconds by default, the
  call is taken by default (`TEAPORT_CALL_PROMPT_DEFAULT`); it can instead keep
  ringing.

<!-- source: docs/CONFIG.md:255,599; brain/teaport_brain/config_schema.toml:1728-1736 @1e5604c -->

With `TEAPORT_TOOL_ANSWER_PHONE_CALL=0`, nobody is asked. A Talk session hears
*"Sorry, a phone call is coming in and I have to take it…"* and closes; a
conversation at the device keeps it, and the caller hears the busy line.

<!-- source: docs/CONFIG.md:599 @1e5604c -->

A call that is already connected is not asked about, because the caller waits on
a silent line: a configuration with `auto_answer=true`, or a call that was up
when the brain restarted. The brain takes the call and holds the conversation.

<!-- source: docs/CONFIG.md:599 @1e5604c -->

While a call is live, the brain refuses a Talk client with
*"Sorry, I'm on a phone call right now…"*, and the gateway turns away a
second call with `486 Busy Here`; the caller's phone handles that, as voicemail
or a busy tone.

<!-- source: docs/CONFIG.md:599-601 @1e5604c -->

To dedicate a device to the phone, turn off the other front-ends, not the
brain, which is what answers calls: the local audio bridge, the Discord bridge,
and Talk in OpenClaw.

<!-- source: docs/CONFIG.md:607-612 @1e5604c -->

## Test the phone path without a line

`brain/tests/fake_sip_gateway.py` stands in for the gateway on the brain's side
of the socket. It binds the socket, rings and answers a call from a caller ID
you choose, plays a WAV as the caller in real time, records what the caller
hears, and hangs up when the bot has answered, partway through the answer, or
after a number of seconds. A brain that drops mid-call is handed the call again
when it reconnects, as the real gateway does. Nothing registers and no SIP port
is bound, so it works on a device whose trunk account is registered somewhere
else. It needs only the system `python3`, run from a checkout that contains it.

<!-- source: docs/CONFIG.md:453-460 @1e5604c -->

The brain needs nothing: its SIP front-end looks for the socket every 2 seconds,
`TEAPORT_SIP_SOCKET`, `/run/teaport/teaport-sip.sock` by default, and takes
calls from whatever serves it.

<!-- source: docs/CONFIG.md:462-465; systemd/teaport-brain.service.in:17 @1e5604c -->

To test the phone path, follow these steps:

1. From a checkout that contains `tools/sip-line-guard` and
   `brain/tests/fake_sip_gateway.py`, guard the real gateway for the session; it
   must not start and register while the fake is up:

   ```bash
   sudo tools/sip-line-guard on
   ```

   The guard is a runtime drop-in whose condition cannot hold, so the service
   skips every start of it, and the drop-in is gone at the next boot. The
   command ends with `GUARDED: teaport-sip.service cannot start`.

   <!-- source: docs/CONFIG.md:467-472,482; tools/sip-line-guard:11-19 @1e5604c -->

2. Prove that the guard holds, without starting anything. The check exits 0
   when the drop-in is loaded, the guard condition fails, and the gateway is not
   running:

   ```bash
   tools/sip-line-guard status
   ```

   <!-- source: docs/CONFIG.md:474-478 @1e5604c -->

3. Create the socket directory and a directory for the recordings. The socket
   directory is the gateway's runtime directory, absent while the service is
   off:

   ```bash
   sudo install -d -o teaspoon -g teaspoon -m 0750 /run/teaport
   install -d -m 0700 ~/fake-sip
   ```

   <!-- source: docs/CONFIG.md:484-485 @1e5604c -->

4. Start the fake gateway. It waits up to 120 seconds for the brain, which
   connects within about 2 seconds:

   ```bash
   python3 brain/tests/fake_sip_gateway.py --socket /run/teaport/teaport-sip.sock \
       --allow-live-path --from +15551234567 --wav brain/test/question.wav \
       --record ~/fake-sip/bot.wav --record-stereo ~/fake-sip/both.wav \
       --summary-json ~/fake-sip/summary.json
   ```

   The call rings in from `+15551234567`; replace it with any caller ID. The
   caller's audio, `question.wav`, asks: "What is the capital of France, and is
   it a large city?"

   <!-- source: docs/CONFIG.md:487-490,516-518 @1e5604c -->

5. Follow the call from the brain's side, in a second shell:

   ```bash
   journalctl -u teaport-brain -f
   ```

   The front-end logs `SIP front-end: connected to the gateway at ...` when it
   connects.

   <!-- source: docs/CONFIG.md:492-493 @1e5604c -->

6. End the fake; it removes the socket when it exits. Then clean up:

   ```bash
   sudo rmdir /run/teaport
   sudo tools/sip-line-guard off
   ```

   <!-- source: docs/CONFIG.md:495-497 @1e5604c -->

The recordings and the result land in `~/fake-sip`:

- `bot.wav`: what the caller heard.
- `both.wav`: the caller on the left channel and the bot on the right.
- `summary.json`: whether the call was answered, when the greeting and the
  answer started and ended, the answer's latency measured from the end of the
  WAV, who hung up, and the control messages the brain sent.

<!-- source: docs/CONFIG.md:488-490,506-509,516 @1e5604c -->

The fake exits 1 when the call was not answered, there was no greeting, there
was no answer to the WAV, or the protocol was violated: an audio frame that is
not exactly 640 bytes, a datagram over the gateway's 2048-byte read, or
unparseable control.

<!-- source: docs/CONFIG.md:509-514 @1e5604c -->

If `brain.env` sets `TEAPORT_SIP_SOCKET` to another path, serve that path
instead; it must not be empty, because empty turns the SIP front-end off. The
fake refuses to bind a path that something already listens on, compares real
paths, so a symlink such as `/var/run` does not pass, and without
`--allow-live-path` it refuses the default path as well.

<!-- source: docs/CONFIG.md:500-504 @1e5604c -->

Any 16-bit WAV works as the caller's audio; other sample rates and stereo are
converted to the 16 kHz mono the gateway speaks. For a call that exercises a
tool, `question_host_status.wav` asks for the host status.

<!-- source: docs/CONFIG.md:519-523 @1e5604c -->

## What's next

These pages cover the rest:

- [CLI reference](/reference/cli/), for every `teaport` command and option.
- [Settings reference](/reference/settings/), for the settings that tune a
  call, such as `SIP_ANSWER_AFTER_SECS`.
- [Architecture](/reference/architecture/), for the session arbiter.

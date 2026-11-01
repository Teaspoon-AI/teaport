---
title: Set up phone calls
description: Answer phone calls on your teaport device over a SIP trunk, through the teaport-sip gateway and the brain's SIP front-end.
---

With a Session Initiation Protocol (SIP) trunk or a session border controller
(SBC) account, the device answers real phone calls. The brain's SIP front-end
answers the call, taking it from a separate gateway service, `teaport-sip`.

<!-- source: systemd/teaport-sip.service.in:1-7,18-19#4ef18c92; brain/teaport_brain/sip_server.py:3-5#e39af618 -->

## Before you begin

You need:

- A device with teaport installed. See [Install teaport](/getting-started/installation/).
- A SIP trunk or SBC account, with its host, domain,
  username, and password.
- The gateway program. The installer creates the `teaport-sip` service, but it
  does not build or install the gateway: that is a separate program in its own
  repository, and the service runs `~/teaport-sip/build/teaport-sip`.
- Access to the device as the user the teaport services run as.

<!-- source: install.sh:2444-2445#439739d4; cli/teaport:27-29#8d57683f -->

## Turn on telephony

Telephony ships off. The installer lays down the `teaport-sip` service, gated on
a configuration file that does not exist, so nothing starts and no SIP port is
bound. The `teaport sip configure` wizard turns it on.

<!-- source: systemd/teaport-sip.service.in:4,17#5cf62ce5 -->

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

<!-- source: cli/teaport:330-338#27174433; cli/teaport:19,27#80224d4c; cli/teaport:1167-1176#938e530e; cli/teaport:1367-1368#fed8b523 -->

Run the wizard as the user the services run as. As root, add `sudo -E`: a plain
`sudo` resets `HOME`, so the wizard writes a configuration the gateway never
reads, and it refuses to.

<!-- source: cli/teaport:693-699,714#f979126a -->

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

<!-- source: cli/teaport:1124-1131#3a2472e8 -->

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

<!-- source: cli/teaport:1000-1003#bafef1f1; cli/teaport:1202-1212#f6ff2e6c; cli/teaport:1234-1237#e22022c1 -->

### Change the trunk later

A re-run, for a new password or a new registrar, rewrites only the keys the
wizard owns: the trunk's `id_uri`, `registrar_uri`, `username`, `password`, and
`realm`, and the four service keys. Every other line of the installed
configuration survives: the source filter, `sip_source_filter` with its
`sip_source_allow` list, `nameserver`, `aec`, comments, and keys the wizard
does not know. Before the test registration, the wizard lists by name the
settings it keeps. To keep nothing and write a fresh template, run
`teaport sip configure --fresh`.

<!-- source: cli/teaport:347-350#c26fec26; cli/teaport:351-368#e3a362d0; cli/teaport:478-484#b5bd2dd5 -->

## Check the line

`teaport sip status` reports the gateway service, the brain's SIP front-end, the
configuration, including whether the running gateway confirms the source filter
in it, and the registration from this run:

```bash
teaport sip status
```

<!-- source: cli/teaport:1314-1341#e661c53b; cli/teaport:543-549#787a371a -->

To follow the gateway's journal:

```bash
teaport logs sip -f
```

The call's own lines are in the brain's journal, `teaport logs brain`.

<!-- source: cli/teaport:8,316-320#0ac45871; brain/teaport_brain/sip_server.py:483#f51c16f8 -->

## Keep the line running

The line survives reboots and crashes on its own. The service is enabled and
restarts with a backoff that does not hammer the registrar, and the brain
reconnects to the gateway whenever the gateway comes back. The two restart
independently: a brain restart, from an update or a configuration change, leaves
the call at the gateway, which hands it to the restarted brain, and the caller
hears a short "sorry, I lost you for a moment". The brain picks up a restarted
gateway within a couple of seconds.

<!-- source: systemd/teaport-sip.service.in:38,47,49#9bb6f61f; systemd/teaport-sip.service.in:18-24#ae006ccc; brain/teaport_brain/sip_server.py:16-18,45-52#a8cd7d23; brain/teaport_brain/config_schema.toml:1813-1822#52b15746 -->

To restart the gateway, for example after editing its configuration by hand:

```bash
teaport sip restart
```

The command waits for the trunk's `200 OK`; the brain reconnects on its own.

<!-- source: cli/teaport:1279-1285,1375#a48b0483 -->

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

<!-- source: cli/teaport:1287-1302,1310-1311,1374#6acaab27 -->

To turn telephony off:

```bash
teaport sip disable
```

The command stops and disables the gateway. The configuration stays at
`~/.config/teaport/teaport-sip.conf`; add `--purge` to remove it too. Run the
wizard again to turn telephony back on.

<!-- source: cli/teaport:1344-1351,1376#4454f6b2 -->

The configuration is the gateway's own `key=value` format: `registrar_uri`,
`id_uri`, `username`, `password`, `uds_path`, `aec`, `auto_answer`, and more.
Editing it by hand is fine; run `teaport sip restart` afterwards. Nothing about
the line lives in `/tmp`: the configuration is in `~/.config/teaport`, the
socket in `/run/teaport`, and the logs in journald.

<!-- source: cli/teaport:27,29#1bfec8de; cli/teaport:395-414#50069afa; systemd/teaport-sip.service.in:9-11,30-36#29a2dda2; cli/teaport:347-350#c26fec26 -->

## A call during a conversation

The brain runs every front-end in one process, and the engine serves one
conversation at a time; the session arbiter decides which one. A phone call and
a Talk session are peers there. See [Architecture](/reference/architecture/).

<!-- source: brain/teaport_brain/sip_server.py:5-6#c9cb1fdf; brain/teaport_brain/session_arbiter.py:5-7,9-14#0b291a8e -->

The brain answers the call, not the gateway. The delay is a setting,
`SIP_ANSWER_AFTER_SECS`, a couple of rings, and the brain builds the call's
pipeline and words its greeting during the ring, so the greeting plays as
soon as the caller is connected. See [the settings reference](/reference/settings/#sip-front-end).

<!-- source: brain/teaport_brain/sip_server.py:27-34#aa05fcee; brain/teaport_brain/config_schema.toml:1775-1785#a986d676; cli/teaport:1208-1210#ba0dcf82 -->

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

<!-- source: brain/teaport_brain/session_arbiter.py:25-31,599-613#f6a0b0f1; brain/teaport_brain/config_schema.toml:1788-1810#e70d77f4 -->

With `TEAPORT_TOOL_ANSWER_PHONE_CALL=0`, nobody is asked. A Talk session hears
*"Sorry, a phone call is coming in and I have to take it…"* and closes; a
conversation at the device keeps it, and the caller hears the busy line.

<!-- source: brain/teaport_brain/config_schema.toml:1640-1647#20ccb9f2; brain/teaport_brain/session_arbiter.py:40-43,550-553#56213afb -->

A call that is already connected is not asked about, because the caller waits on
a silent line: a configuration with `auto_answer=true`, or a call that was up
when the brain restarted. The brain takes the call and holds the conversation.

<!-- source: brain/teaport_brain/session_arbiter.py:359-364#6c992cf1; brain/teaport_brain/sip_server.py:39-43,45-49#510dd7f2 -->

While a call is live, the brain refuses a Talk client with
*"Sorry, I'm on a phone call right now…"*, and the gateway turns away a
second call with `486 Busy Here`; the caller's phone handles that, as voicemail
or a busy tone.

<!-- source: brain/teaport_brain/session_arbiter.py:32-34,502-506#f32279bb; brain/tests/fake_sip_gateway.py:19-20#704657fc -->

To dedicate a device to the phone, turn off the other front-ends, not the
brain, which is what answers calls: the local audio bridge, the Discord bridge,
and Talk in OpenClaw.

<!-- source: brain/teaport_brain/session_arbiter.py:9-14#90d2cb37; systemd/teaport-sip.service.in:18-19#4470ffc5 -->

## Test the phone path without a line

`brain/tests/fake_sip_gateway.py` stands in for the gateway on the brain's side
of the socket. It binds the socket, rings and answers a call from a caller ID
you choose, plays a WAV as the caller in real time, records what the caller
hears, and hangs up when the bot has answered, partway through the answer, or
after a number of seconds. A brain that drops mid-call is handed the call again
when it reconnects, as the real gateway does. Nothing registers and no SIP port
is bound, so it works on a device whose trunk account is registered somewhere
else. It needs only the system `python3`, run from a checkout that contains it.

<!-- source: brain/tests/fake_sip_gateway.py:3-10,13-33,39-41#d1043fc3; brain/tests/fake_sip_gateway.py:808-818,845#464b24cc -->

The brain needs nothing: its SIP front-end looks for the socket every 2 seconds,
`TEAPORT_SIP_SOCKET`, `/run/teaport/teaport-sip.sock` by default, and takes
calls from whatever serves it.

<!-- source: systemd/teaport-brain.service.in:17#3f95ac55; brain/teaport_brain/sip_server.py:5-7,247#de2d5741; brain/teaport_brain/config_schema.toml:1813-1822#52b15746 -->

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

   <!-- source: tools/sip-line-guard:11-19#f808a444; tools/sip-line-guard:58,66-69#540d6738 -->

2. Prove that the guard holds, without starting anything. The check exits 0
   when the drop-in is loaded, the guard condition fails, and the gateway is not
   running:

   ```bash
   tools/sip-line-guard status
   ```

   <!-- source: tools/sip-line-guard:8-9,36-59#35d88e94 -->

3. Create the socket directory and a directory for the recordings. The socket
   directory is the gateway's runtime directory, absent while the service is
   off:

   ```bash
   sudo install -d -o teaspoon -g teaspoon -m 0750 /run/teaport
   install -d -m 0700 ~/fake-sip
   ```

   <!-- source: systemd/teaport-sip.service.in:30-36#fd95366a; brain/tests/fake_sip_gateway.py:99-100#38682664 -->

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

   <!-- source: brain/tests/fake_sip_gateway.py:808-818,841-842,845#afda3533; brain/test/make_question_wav.py:9-15,105#0a163423 -->

5. Follow the call from the brain's side, in a second shell:

   ```bash
   journalctl -u teaport-brain -f
   ```

   The front-end logs `SIP front-end: connected to the gateway at ...` when it
   connects.

   <!-- source: brain/teaport_brain/sip_server.py:483#f51c16f8 -->

6. End the fake; it removes the socket when it exits. Then clean up:

   ```bash
   sudo rmdir /run/teaport
   sudo tools/sip-line-guard off
   ```

   <!-- source: tools/sip-line-guard:10,70-75#305b0032; brain/tests/fake_sip_gateway.py:458-462#0d9312d7 -->

The recordings and the result land in `~/fake-sip`:

- `bot.wav`: what the caller heard.
- `both.wav`: the caller on the left channel and the bot on the right.
- `summary.json`: whether the call was answered, when the greeting and the
  answer started and ended, the answer's latency measured from the end of the
  WAV, who hung up, and the control messages the brain sent.

<!-- source: brain/tests/fake_sip_gateway.py:747-798#a62558d7; brain/tests/fake_sip_gateway.py:816-818,845#2f32c326 -->

The fake exits 1 when the call was not answered, there was no greeting, there
was no answer to the WAV, or the protocol was violated: an audio frame that is
not exactly 640 bytes, a datagram over the gateway's 2048-byte read, or
unparseable control.

<!-- source: brain/tests/fake_sip_gateway.py:627-628,639-643,655#27da9a25; brain/tests/fake_sip_gateway.py:883-893,902#572fbd98 -->

If `brain.env` sets `TEAPORT_SIP_SOCKET` to another path, serve that path
instead; it must not be empty, because empty turns the SIP front-end off. The
fake refuses to bind a path that something already listens on, compares real
paths, so a symlink such as `/var/run` does not pass, and without
`--allow-live-path` it refuses the default path as well.

<!-- source: brain/teaport_brain/config_schema.toml:1813-1822#52b15746; brain/tests/fake_sip_gateway.py:186-189,417-428,810-811#63009a0e -->

Any 16-bit WAV works as the caller's audio; other sample rates and stereo are
converted to the 16 kHz mono the gateway speaks. For a call that exercises a
tool, `question_host_status.wav` asks for the host status.

<!-- source: brain/tests/fake_sip_gateway.py:122-125,129-130#006a271e; brain/test/run_sip_tools_test.sh:4,14#f5fbaa7a -->

## What's next

These pages cover the rest:

- [CLI reference](/reference/cli/), for every `teaport` command and option.
- [Settings reference](/reference/settings/), for the settings that tune a
  call, such as `SIP_ANSWER_AFTER_SECS`.
- [Architecture](/reference/architecture/), for the session arbiter.

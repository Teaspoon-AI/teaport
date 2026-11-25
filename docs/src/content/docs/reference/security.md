---
title: Security model
description: Which accounts teaport runs as, what each root helper may do, and how the owner's login is created once.
---

teaport's services run as an ordinary user, the run user, not as root. The agent runs as
that user too. So anything the run user can do, a compromised agent can do as well. This
page describes the few things the run user can ask root to do, and how the device's
administrator login comes to exist.

## The run user and root

The brain, the engine, Wi-Fi setup, and the agent run as the run user. That user can't
change system files or other users by itself. For the few changes it needs, it calls a
root helper through `sudo`, one rule per helper. Each helper is a small script, owned by
root, in `/usr/lib/teaport/`. It runs on the system Python, never the brain's own
environment. The `teaport-brain` package installs the helpers, and
`/usr/lib/teaport/sudoers-sync` writes their rules into `/etc/sudoers.d/teaport-config`.

<!-- source: packaging/deb/sudoers-sync:5-11#e895c837; packaging/deb/sudoers-sync:82-105#e1a1abd8; packaging/nfpm.yaml:88,97,106#54fe789f -->

| Helper | What the run user can ask it to do |
| --- | --- |
| `config_apply.py` | Write one of the settings files in `/etc/teaport/`, and restart or stop a teaport service. |
| `wifi_apply.py` | Change the Wi-Fi: the setup network, joining a Wi-Fi network, and putting the old one back. It takes Wi-Fi requests only. |
| `login_apply.py` | Create the owner's login, one time only, on a device that ships ready to set up. |

Each helper refuses any request outside that list. A helper that takes a password reads
it on standard input, never on a command line, where any user could read it.

<!-- source: brain/teaport_brain/login_apply.py:43-45#5955915f; brain/teaport_brain/wifi_apply.py:28-30#b0c60a6e -->

The settings in `/etc/teaport/` are written on the run user's behalf, so no command
that runs as root takes its environment from them.

<!-- source: systemd/teaport-wifi-setup-net.service.in:9-13#e760f30b; brain/teaport_brain/config_apply.py:37-41#cd855526 -->

The run user's only other rights come through polkit: it can scan for Wi-Fi networks,
and it can start `teaport-update.service`, the root unit that `teaport update` starts.
That unit takes no arguments and reads from the release manifest what to install, so
the run user decides when an update runs, never what it installs. An update doesn't
need the run user to have `sudo`.

<!-- source: packaging/wifi-setup/50-teaport-wifi-setup.rules.in:9-19#a6b65275; packaging/deb/sudoers-sync:12-17#2bf6c0cf; packaging/deb/sudoers-sync:59-74#dd65fc86 -->

## The owner's login

The run user must never be able to make itself an administrator. So a device that
ships ready to set up has no login at all, and the owner creates it during setup: an
account named `teaport` with a home folder, a Bash shell, and `sudo` rights. It is
never the run user's own account.

<!-- source: brain/teaport_brain/login_apply.py:92-93#1c1c90ab; brain/teaport_brain/login_apply.py:300-301#5f08f622; brain/teaport_brain/login_apply.py:238-242#57a7a7ce -->

`login_apply.py` creates it one time only, and only on a device that asked for it:

- The step that prepares a device for shipping writes a pending marker,
  `/var/lib/teaport-owner/pending`, in a folder only root can open. The marker means
  that the device has no owner login yet. It is outside every folder the run user can
  write, including `/etc/teaport` and teaport's state folder. Neither the installer nor
  the package writes it.
- Without the marker, the helper creates nothing. A device that you install yourself
  never has one, so it never offers the step.
- A marker that the run user could have written, in a folder it can write or owned by
  another user, counts as no marker.
- Without the marker, an existing `teaport` account is the owner's, and the helper
  refuses. It removes the marker when it creates the login.
- If a step fails, the helper undoes the steps before it. While the marker is there, a
  `teaport` account that is plainly what a create left behind, for example after a
  power cut between its steps, is finished by the next create: no password was ever set
  on it, its full name is `Teaport owner`, and its home folder is missing or is its own
  folder with no `.ssh` in it. The next create makes the home folder if it has none (as
  a new account gets one, from `/etc/skel`, with the mode that `/etc/login.defs` sets),
  gives the account its `sudo` rights, sets the password last, and removes the marker.
- Any other `teaport` account, marker or not, is someone's login: the helper refuses,
  and the owner sees that the device already has a login. An account that someone
  prepared, with a password or SSH keys of their own, is never handed to the owner.
- The helper also refuses when a group named `teaport`, or a `/home/teaport` folder,
  exists without the account: the new account can't get a group of its own name, and a
  folder it didn't make isn't its to take over. The device then doesn't offer the step.

<!-- source: brain/teaport_brain/login_apply.py:16-28#47ab3ecd; brain/teaport_brain/login_apply.py:163-184#84310eeb; brain/teaport_brain/login_apply.py:206-233#5edb85ef; brain/teaport_brain/login_apply.py:288-343#f0dd833c -->

The helper finishes what it starts. It blocks every signal it can, and it runs the
create in a process of its own session, apart from the one that `sudo` started. `sudo`
can end its command, for example when the run user sends `sudo` an alarm signal, and
then stops the command's whole process group with `SIGKILL`; the create goes on
regardless, through to removing the marker. The create still belongs to the service
that asked for it, though, and takes about a second: stopping that service (after its
stop timeout), the out-of-memory killer, a restart of the device, or a power cut can
stop it partway. The next create usually finishes what it left, when that is plainly a
leftover; see [The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login)
for when it doesn't. The helper leaves no core dump that could hold the password.

<!-- source: brain/teaport_brain/login_apply.py:51-63#e5e125ca; brain/teaport_brain/login_apply.py:395-430#57ad4d30; brain/teaport_brain/login_apply.py:369-374#e61abdcf; brain/teaport_brain/login_apply.py:441-444#701bd93e -->

The password goes from the setup page or the settings to the helper, and from the
helper to `chpasswd` on standard input. It is never written to a log, the journal,
the Wi-Fi setup status file, an error message, or a command line. Where `sudo` can log what its commands read
(classic `sudo`, with input logging on), the helper's `sudo` rule says `NOLOG_INPUT`, so
`sudo` doesn't keep the password. `sudo-rs`, which newer Ubuntu releases use, has no such
logging and doesn't accept the tag, so the rule leaves it out there. On its way it is held in a wrapper that hides it from an error report
that prints variables. A password is at
least 12 characters and at most 256 bytes, without control or invisible characters.

<!-- source: brain/teaport_brain/login_apply.py:43-49#5f5b4325; brain/teaport_brain/owner_login.py:6-13#c2bc1e2d; brain/teaport_brain/root_helper.py:16-38#8e6425a9; packaging/deb/sudoers-sync:89-101#b51d41a8; brain/teaport_brain/login_apply.py:93-97#831554ec; brain/teaport_brain/login_apply.py:137-148#020cf4bc -->

The setup page's form can create the administrator account, so the page answers only
under its own names: the setup network's address and its `.local` name. A request for
any other name, such as a captive-portal check (`captive.apple.com`) or a name that a
site has pointed at the device, is sent to the setup address instead, which is the
usual captive-portal redirect. The page accepts the form only under those names, and
only from its own pages: it refuses a post that a page from another site sends, for
example from a tab that the phone loaded over its mobile data. A post that names no
origin is accepted. One whose origin is `null`, which some captive browsers send but so
does a sandboxed frame on any site, is accepted only when the browser also says the post
comes from the page itself.

<!-- source: brain/teaport_brain/wifi_setup.py:686-701#362b2378; brain/teaport_brain/wifi_setup.py:768-781#74bd2512 -->

The setup page answers only devices on the setup network itself, whose addresses are in
its subnet (`10.42.0.0/24`). Listening on the setup address alone isn't enough: a device
on your home network could route that address to the device and reach the page
over the home network, for example while the device is online over Ethernet and in setup
mode. The page drops any connection from outside the subnet before it reads anything.
The rule that sends port 80 to the page doesn't name the setup network's interface,
because the code picks the Wi-Fi device when setup runs.

<!-- source: brain/teaport_brain/wifi_setup.py:13-21#1be8f012; brain/teaport_brain/wifi_setup.py:539-558#30298a1c -->

Teaspoon AI's provisioning, which runs outside this repository when it prepares a
Teaspoon AI device for shipping, writes the marker, refuses a device that already has a
`teaport` account, group, or home folder, removes the run user from the `sudo` group and sets aside
its other `sudo` rules (only the helpers' rules in `/etc/sudoers.d/teaport-config`
stay), locks the run user's password, removes its SSH keys, and makes sure that SSH
accepts passwords. The paper insert carries no login. So on such a device, until the
owner creates the login, nobody can sign in. This repository's code doesn't enforce
these steps; the helper only relies on the marker.

## The known window

The window lasts until the owner creates the login, not only until the device is set
up. For as long as the marker is there, software that runs as the run user, such as a
compromised agent, could create the login first. The owner then sees that the device
already has a login, in place of the step. Because the setup page and the settings run
as the run user, software that was compromised before the owner typed the password
could also read the password as it is typed.

Anyone who can reach the setup page or the settings can create the login first, too:

- On the setup network: anyone who knows its password. That password is on the paper
  insert, on the device's screen during setup, and spoken aloud by the device when it
  has no screen or you say "repeat".
- The setup page is plain HTTP over the setup network's WPA2 password. Someone who knows
  that password and recorded the phone joining the network can read what the phone
  sends, including the login password.
- On a device without a gateway token (`GATEWAY_TOKEN`), the configuration page on the
  device itself, `http://127.0.0.1:7861/config`, needs no token, so any user or program
  on the device can create the login there.

<!-- source: brain/teaport_brain/wifi_setup.py:7-13#5369985c; brain/teaport_brain/wifi_setup.py:686-701#362b2378; brain/teaport_brain/config_ui.py:102-118#6e4abba8; brain/teaport_brain/config_ui.py:138-140#7c7d40df -->

The setup page makes the step required, so on a device set up through that page the
window ends at setup. The exception is a create that fails because of the device, not
the password: the owner can then connect without a login, and the window stays open
until they create it in the settings. The same holds when the page can't ask the helper
at the start of setup and shows no step. A device that went online another way offers the
step in the settings until the owner creates the login. The marker limits the window to a device
whose owner has no login yet; it doesn't prevent it. See
[The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login).

<!-- source: brain/teaport_brain/login_apply.py:34-41#01fb9b96; brain/teaport_brain/wifi_setup.py:37-51#de9e3f82 -->

## Related

- [Create your login](/guides/wifi-setup/#create-your-login)
- [Create your login in the settings](/guides/configuration/#create-your-login-in-the-settings)
- [Installer reference](/reference/installer/)

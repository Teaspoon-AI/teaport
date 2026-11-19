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

<!-- source: packaging/deb/sudoers-sync:5-10#cfea6970; packaging/deb/sudoers-sync:47-56#56986163; packaging/nfpm.yaml:78,87,96#54fe789f -->

| Helper | What the run user can ask it to do |
| --- | --- |
| `config_apply.py` | Write one of the settings files in `/etc/teaport/`, and restart or stop a teaport service. |
| `wifi_apply.py` | Change the Wi-Fi: the setup network, joining a Wi-Fi network, and putting the old one back. It takes Wi-Fi requests only. |
| `login_apply.py` | Create the owner's login, one time only, on a device that ships ready to set up. |

Each helper refuses any request outside that list. A helper that takes a password reads
it on standard input, never on a command line, where any user could read it.

<!-- source: brain/teaport_brain/login_apply.py:36-38#5955915f; brain/teaport_brain/wifi_apply.py:28-30#b0c60a6e -->

The settings in `/etc/teaport/` are written on the run user's behalf, so no command
that runs as root takes its environment from them.

<!-- source: systemd/teaport-wifi-setup-net.service.in:9-13#e760f30b; brain/teaport_brain/config_apply.py:37-41#cd855526 -->

The only other right the run user has is to scan for Wi-Fi networks, through a polkit
rule.

<!-- source: packaging/wifi-setup/50-teaport-wifi-setup.rules.in:9-19#a6b65275 -->

## The owner's login

The run user must never be able to make itself an administrator. So a device that
ships ready to set up has no login at all, and the owner creates it during setup: an
account named `teaport` with a home folder, a Bash shell, and `sudo` rights. It is
never the run user's own account.

<!-- source: brain/teaport_brain/login_apply.py:76-77#1c1c90ab; brain/teaport_brain/login_apply.py:211-212#5f08f622; brain/teaport_brain/login_apply.py:152-156#15abae22 -->

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
  `teaport` account counts as one that a create started and didn't finish, for example
  after a power cut between its steps: the device still offers the step, and the next
  create sets the password, gives the account its `sudo` rights, and removes the marker.
  The step that prepares a device for shipping refuses a device that already has a
  `teaport` account, so no other account can be taken over this way.

<!-- source: brain/teaport_brain/login_apply.py:6-25#19999030; brain/teaport_brain/login_apply.py:72-74#2cb3dff3; brain/teaport_brain/login_apply.py:178-247#5169fbc0 -->

The helper finishes what it starts. It blocks every signal that the run user could send
it through `sudo`, so it can't be stopped between its steps, except by `SIGKILL` or a
power cut, which the next create recovers from. It leaves no core dump that could hold
the password.

<!-- source: brain/teaport_brain/login_apply.py:41-49#928cedc8; brain/teaport_brain/login_apply.py:269-274#e61abdcf; brain/teaport_brain/login_apply.py:291-294#701bd93e -->

The password goes from the setup page or the settings to the helper, and from the
helper to `chpasswd` on standard input. It is never written to a log, the journal,
the Wi-Fi setup status file, an error message, or a command line. A password is at
least 12 characters and at most 256 bytes, without control or invisible characters.

<!-- source: brain/teaport_brain/login_apply.py:36-42#5f5b4325; brain/teaport_brain/owner_login.py:6-11#4b5bc913; brain/teaport_brain/login_apply.py:77-81#831554ec; brain/teaport_brain/login_apply.py:112-123#020cf4bc -->

The setup page's form can create the administrator account, so the page accepts it
only from its own pages: it refuses a post that a page from another site sends, for
example from a tab that the phone loaded over its mobile data.

<!-- source: brain/teaport_brain/wifi_setup.py:641-658#88dc47e7 -->

On a Teaspoon AI device, the step that prepares it for shipping also writes the marker,
removes the run user from the `sudo` group and sets aside its other `sudo` rules (only
the helpers' rules in `/etc/sudoers.d/teaport-config` stay), locks the run user's
password, removes its SSH keys, and makes sure that SSH accepts passwords. The paper
insert carries no login. Until the owner creates the login, nobody can sign in to the
device.

## The known window

The window lasts until the owner creates the login, not only until the device is set
up. For as long as the marker is there, software that runs as the run user, such as a
compromised agent, could create the login first. The owner then sees that the device
already has a login, in place of the step. Because the setup page and the settings run
as the run user, software that was compromised before the owner typed the password
could also read the password as it is typed.

The setup page makes the step required, so on a device set up through that page the
window ends at setup. A device that went online another way offers the step in the
settings until the owner creates the login. The marker limits the window to a device
whose owner has no login yet; it doesn't prevent it. See
[The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login).

<!-- source: brain/teaport_brain/login_apply.py:27-34#01fb9b96; brain/teaport_brain/wifi_setup.py:31-41#f51e8da8 -->

## Related

- [Create your login](/guides/wifi-setup/#create-your-login)
- [Create your login in the settings](/guides/configuration/#create-your-login-in-the-settings)
- [Installer reference](/reference/installer/)

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

<!-- source: brain/teaport_brain/login_apply.py:27-29#5955915f; brain/teaport_brain/wifi_apply.py:28-30#b0c60a6e -->

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

<!-- source: brain/teaport_brain/login_apply.py:64-65#1c1c90ab; brain/teaport_brain/login_apply.py:183-184#5f08f622 -->

`login_apply.py` creates it one time only, and only on a device that asked for it:

- The step that prepares a device for shipping writes a pending marker,
  `/var/lib/teaport-owner/pending`, in a folder only root can open. The marker is
  outside every folder the run user can write, including `/etc/teaport` and teaport's
  state folder. Neither the installer nor the package writes it.
- Without the marker, the helper creates nothing. A device that you install yourself
  never has one, so it never offers the step.
- A marker that the run user could have written, in a folder it can write or owned by
  another user, counts as no marker.
- The helper refuses when a `teaport` account already exists, and removes the marker
  when it creates one. If a step fails, it undoes the steps before it.

<!-- source: brain/teaport_brain/login_apply.py:6-20#22c10f6d; brain/teaport_brain/login_apply.py:60-62#2cb3dff3; brain/teaport_brain/login_apply.py:151-169#1b5d5b28 -->

The password goes from the setup page or the settings to the helper, and from the
helper to `chpasswd` on standard input. It is never written to a log, the journal,
the Wi-Fi setup status file, an error message, or a command line.

<!-- source: brain/teaport_brain/login_apply.py:27-38#155cfb4b; brain/teaport_brain/owner_login.py:6-11#4b5bc913 -->

On a device that ships ready to set up, the step that prepares it for shipping also
takes away the run user's password, its SSH keys, and its general `sudo` rights, and
the paper insert carries no login. Until the owner creates the login, nobody can sign
in to the device.

## The known window

Between the time the device is prepared for shipping and the time its owner sets it up,
the marker is there. During that time, software that runs as the run user, such as a
compromised agent, could create the login first. The owner then sees that the device
already has a login, in place of the step. The marker limits this to a device that
nobody has set up yet; it doesn't prevent it. See
[The device already has a login](/guides/troubleshooting/#the-device-already-has-a-login).

<!-- source: brain/teaport_brain/login_apply.py:22-25#74f3c370 -->

## Related

- [Create your login](/guides/wifi-setup/#create-your-login)
- [Create your login in the settings](/guides/configuration/#create-your-login-in-the-settings)
- [Installer reference](/reference/installer/)

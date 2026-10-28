# The TouchMix hardware and its Linux system

What the mixer is underneath the control protocol. Most of this project was
worked out over the network (see [PROTOCOL.md](PROTOCOL.md) and
[RESEARCH.md](RESEARCH.md)); the notes here were confirmed directly on a unit
during an owner recovery (the OS card was read on a PC, with QSC's guidance,
after the device stopped booting).

> **Privacy and safety.** This file documents the *device*, not how to get into
> one. The root password hash, the exact password-reset procedure, and the
> unit's MAC address are deliberately left out: they are a device credential and
> an authentication-bypass recipe, and publishing them would let anyone with
> physical access get into any TouchMix, not just their own. Keep real captures,
> `/etc/passwd`, and the config XML (which holds the Wi-Fi and mixer passwords
> in plain text) out of the repo too.

## Hardware and OS

* **Platform:** an Arago Linux build (Arago 2011.09, the TI "m16" target), on a
  TI-class embedded board. The app library's own paths call the platform
  **ds2i** and the project **Sonora** (RESEARCH.md).
* **The OS lives on a removable microSD card.** Three partitions:

  | Partition | Rough size | Filesystem | Holds |
  |-----------|-----------|------------|-------|
  | 1 | ~70 MB | vfat | boot (u-boot, `uImage`, `MLO`) |
  | 2 | ~780 MB | ext3 | a backup / secondary root filesystem |
  | 3 | ~2.9 GB | ext3 | the **active root filesystem** |

  So the whole system is imageable and restorable with `dd` by the owner, which
  is the supported recovery path if a unit stops booting.

## Network services

A port scan found only these open (PROTOCOL.md), and a later scan of the common
audio/video streaming and clock ports (RTSP, RTP, RTMP, Dante, PTP) found
**none** — the mixer does not stream audio over the network:

| Port | Service | Notes |
|-----:|---------|-------|
| 22 | **Dropbear SSH 0.51** | SSH and SFTP |
| 23 | Telnet | a login shell |
| 12865 | netperf | network benchmarking, not audio |
| dynamic | the control protocol | the port the app uses; changes on reboot, found over mDNS |

SFTP is served by `/usr/libexec/sftp-server` behind Dropbear; the host key is at
`/etc/dropbear/dropbear_rsa_host_key`, and the service is advertised as
`_sftp-ssh` over mDNS/Avahi. That is the file-transfer service the official app
never uses.

### Connecting over SSH / SFTP

Dropbear 0.51 is old enough that a current OpenSSH client refuses its ciphers by
default. You must re-enable the legacy algorithms (this only affects which
crypto is offered; it is **not** a way past the login — you still need a valid
account on the device):

```sh
ssh -p 22 \
  -o KexAlgorithms=+diffie-hellman-group1-sha1 \
  -o HostKeyAlgorithms=+ssh-rsa \
  -o Ciphers=+aes128-cbc,3des-cbc \
  -o MACs=hmac-sha1,hmac-sha1-96,hmac-md5 \
  <user>@<mixer-ip>
```

`sftp` and `scp` take the same `-o` options. A login the device accepts is the
owner's to obtain (from QSC, or by owner recovery of their own unit); this repo
does not carry one.

## Filesystem map (active root)

This lines up with everything reverse-engineered from the app, and it is where a
network collector will look:

* **`/usr/share/ds2i/`** — the mixer itself
  * `MixClient`, `MixServer` — the mixer binaries (the app and the mixer share a
    code base; see RESEARCH.md, "Inside the official app")
  * `config.xml` / `config.xml.backup` — the full mixer state (the same file
    the mixer sends clients on connect) — **contains passwords in plain text**
  * `user/scenes/` — saved scenes (`.scene.dqx` + `.scene.dqxhdr`)
  * `user/settings/` — the per-client access profiles (PROTOCOL.md,
    "Per-client access")
  * `user/channels/`, `user/effects/`, `user/routing/` — saved presets
  * `html/` — the web UI the app loads over a JavaScript bridge
* **`/home/`** — small home directories; no mixer data of interest

## Getting recordings off

* **USB (works today):** the mixer records to a USB drive; `touchmix collect`
  copies a session off with one WAV per channel (README).
* **Over the network (in progress):** with a valid SFTP login, the recorded
  sessions can be pulled straight off the device's filesystem. Their on-device
  location is still being confirmed; this section will be updated once the
  network collector is built and tested.

## Firmware updates

QSC's published firmware (`*.zip` → `V<ver>.tar.gz` → `upgrade-file` +
`checksum.txt`) ships the payload **encrypted**: `upgrade-file` is base64 of an
OpenSSL `enc` blob (the `Salted__` header). The device decrypts it on update
with a key QSC holds and does not publish, so the update package can't be
inspected without that key. QSC has confirmed they do not release the key.

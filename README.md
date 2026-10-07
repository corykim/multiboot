# multiboot

A tiny cross-platform CLI that triggers a **one-shot reboot into the other OS** on a Windows/Linux UEFI dual-boot machine. Run it from Windows to boot into Linux, or from Linux to boot into Windows — the machine reverts to its normal default on the following boot.

## How it works

`multiboot` runs on both OSes and uses a different mechanism in each direction, because what's reachable differs by OS:

- **Windows → Linux** drives the UEFI firmware boot menu via `bcdedit`. It arms a one-shot boot of the GRUB (`ubuntu`) firmware entry with `bcdedit /set {fwbootmgr} bootsequence {GUID}`. The firmware boots GRUB once; GRUB then boots its default.
- **Linux → Windows** uses `grub-reboot` to set GRUB's one-shot `next_entry` to the Windows menu entry (falling back to `grub-editenv`).

It deliberately does **not** try to read/write grubenv from Windows: on a standard Ubuntu+GRUB UEFI install the EFI System Partition holds only a stub `grub.cfg` and no grubenv — the real files live on the ext4 `/boot` partition, which Windows can't read.

## Requirements

- A UEFI dual-boot setup with **GRUB** as the Linux bootloader.
- **Windows side:** must run as **Administrator** (to read/set firmware entries via `bcdedit`).
- **Linux side:** `grub-reboot` available and `GRUB_DEFAULT=saved` in `/etc/default/grub` (see [GRUB configuration](#grub-configuration)). The command re-execs itself under `sudo`.
- [uv](https://docs.astral.sh/uv/) to run or install it.

## Install

```bash
uv tool install --editable ".[tray]"
```

Exposes `multiboot` (the CLI) and `multiboot-tray` (an optional Windows system-tray icon). Drop `[tray]` if you don't want the tray front-end.

## Usage

```bash
multiboot list              # show boot entries (works on both OSes)

# From Windows (Administrator):
multiboot to-linux          # arm a one-shot boot to Linux, then reboot
multiboot to-linux --entry ubuntu

# From Linux:
multiboot to-windows        # arm a one-shot boot to Windows, then reboot
multiboot to-windows --entry "Windows"
```

Add `--dry-run` to arm the next boot **without** rebooting. Note it still writes the boot state (firmware `bootsequence` / grubenv `next_entry`); it only skips the reboot itself. Clear a stray arming with `bcdedit /deletevalue {fwbootmgr} bootsequence` (Windows) or `grub-editenv - unset next_entry` (Linux).

`--entry` is auto-detected when omitted. On Windows it accepts a `{GUID}`, a list index, or a firmware-description substring; on Linux it accepts a GRUB menu entry title or index.

## GRUB configuration

For both directions to work reliably, GRUB's persistent default must point at **Linux**, and GRUB must honor one-shot overrides:

```bash
# /etc/default/grub
GRUB_DEFAULT=saved        # required: grub-reboot's one-shot next_entry only works with "saved"
GRUB_SAVEDEFAULT=false    # don't drift the default to the last-booted OS
```

Then pin the saved default to Linux and regenerate the config:

```bash
sudo grub-set-default 0   # or the exact Ubuntu menuentry title
sudo update-grub
```

With this, Windows→Linux lands on the Linux default, and Linux→Windows does a one-shot to Windows and reverts. Using `GRUB_SAVEDEFAULT=true` ("remember last selection") breaks Windows→Linux: the firmware can only get you *into* GRUB — it can't tell GRUB which entry to pick — so a remembered Windows default sends you straight back. Do **not** set `GRUB_DEFAULT=0`; that disables the `next_entry` machinery and breaks `to-windows`.

## Project layout

- `multiboot/cli.py` — all logic; behavior branches on `IS_WINDOWS` / `IS_LINUX`.
- `multiboot/tray.py` — optional Windows tray front-end that shells out to the CLI, elevating via `ShellExecuteW … runas`.

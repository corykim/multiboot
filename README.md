# multiboot

A tiny cross-platform CLI that triggers a **one-shot reboot into any boot entry** on a UEFI multi-boot machine — from either Windows or Linux. Pick any OS/entry; the machine reverts to its normal default on the following boot.

## How it works

`multiboot list` shows every boot **target** reachable from the OS you're on, and `multiboot <entry>` arms a one-shot boot of it, then reboots. Targets span **two layers**:

- **UEFI firmware entries** — one per bootloader (e.g. `Windows Boot Manager`, `ubuntu`/GRUB). This is the cross-OS layer: the same NVRAM list is visible and settable from either OS, via `bcdedit /set {fwbootmgr} bootsequence {GUID}` on Windows or `efibootmgr --bootnext` on Linux.
- **Inside a bootloader** — individual OSes that share one bootloader:
  - On Windows, separate installs under one Windows Boot Manager (e.g. Win11 vs Win10), armed via `bcdedit /bootsequence`.
  - On Linux, GRUB menu entries (os-prober nests other OSes here), armed via `grub-reboot`.

A target inside a bootloader also arms the firmware layer so the right bootloader runs first. The shorthands `multiboot linux` / `multiboot windows` auto-detect the obvious target for that OS and boot it.

It deliberately does **not** try to read/write grubenv from Windows: on a standard Ubuntu+GRUB UEFI install the EFI System Partition holds only a stub `grub.cfg` and no grubenv — the real files live on the ext4 `/boot` partition, which Windows can't read. From Windows you reach Linux by booting GRUB (a firmware entry); GRUB then picks the entry.

## Requirements

- A UEFI multi-boot setup. GRUB is the Linux bootloader for the Linux-side GRUB-menu targets.
- **Windows side:** must run as **Administrator** (to read/set entries via `bcdedit`).
- **Linux side:** `efibootmgr` (firmware targets) and/or `grub-reboot` with `GRUB_DEFAULT=saved` in `/etc/default/grub` for GRUB-menu targets (see [GRUB configuration](#grub-configuration)). Booting a target re-execs under `sudo`.
- [uv](https://docs.astral.sh/uv/) to run or install it.

## Install

From the repo root, pick one:

```bash
# CLI only (no extra dependencies) -- recommended for Linux and most setups
uv tool install --editable .

# CLI + Windows system-tray icon (pulls in pystray + pillow)
uv tool install --editable ".[tray]"
```

Both put `multiboot` (the CLI) on your PATH. The tray is an optional Windows-only front-end that shells out to the CLI; you don't need it to use `multiboot`. The `multiboot-tray` command is installed either way, but without `[tray]` it just shows a "missing dependencies" error when launched.

To add the tray later, re-run the `[tray]` command (add `--force` if uv says it's already installed). To uninstall: `uv tool uninstall multiboot`.

You can also skip installing entirely and run from the checkout with `uv run multiboot ...`.

## Usage

```bash
multiboot                      # list all boot targets arm-able from this OS (default)
multiboot 2                    # one-shot boot target #2 (from list), then reboot
multiboot ubuntu               # ...by name substring
multiboot "{d7b25d8b-...}"     # ...by firmware GUID

multiboot linux                # convenience: auto-detect + boot Linux
multiboot windows              # convenience: auto-detect + boot Windows

multiboot 2 --dry-run          # arm it but DON'T reboot (for testing)
```

The `TARGET` argument is an entry from `multiboot list`: its **index number**, a **name substring**, or a **{GUID}**. (A bare number is always the list index, not a hex boot number.) The words `list` (the default), `linux`, and `windows` are reserved; select an entry literally named one of those by index or GUID.

Add `--dry-run` to arm the next boot **without** rebooting. It still writes the boot state — firmware `bootsequence`/`BootNext`, Windows BCD `bootsequence`, or grubenv `next_entry` — it only skips the reboot itself. Clear a stray arming with `bcdedit /deletevalue {fwbootmgr} bootsequence` (plus `{bootmgr}` for a Windows install target) on Windows, or `efibootmgr --delete-bootnext` / `grub-editenv - unset next_entry` on Linux.

> **Note on nested GRUB entries:** for a GRUB menu entry inside a submenu, prefer the entry **title** over the list index — GRUB numbers submenu entries as `submenu>entry`, which won't match the flat index shown by `list`.

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

With this, Windows→Linux lands on the Linux default, and Linux→Windows does a one-shot to Windows and reverts. Using `GRUB_SAVEDEFAULT=true` ("remember last selection") breaks Windows→Linux: the firmware can only get you *into* GRUB — it can't tell GRUB which entry to pick — so a remembered Windows default sends you straight back. Do **not** set `GRUB_DEFAULT=0`; that disables the `next_entry` machinery and breaks booting GRUB-menu targets.

## Status & testing

Developed and verified on a two-target machine (one firmware `Ubuntu` entry + one `Windows 11` install), so the multi-OS paths still need testing on richer setups:

| Path | Mechanism | Status |
|------|-----------|--------|
| Windows → Linux (firmware) | `bcdedit {fwbootmgr} bootsequence` | ✅ verified — full round trip via both `multiboot linux` and `multiboot 0` |
| Windows → a Windows install | `bcdedit /bootsequence` + firmware→`{bootmgr}` | ⚠️ arming verified via `--dry-run`; not boot-tested (needs 2 Windows installs, e.g. Win11/Win10) |
| Linux → firmware entry | `efibootmgr --bootnext` | ❔ untested (developed on Windows) |
| Linux → GRUB menu entry | `grub-reboot` (+ best-effort `--bootnext`) | ❔ untested; requires `GRUB_DEFAULT=saved` |
| Multi-distro (e.g. Ubuntu → Rocky) | firmware entry *or* GRUB entry | ❔ untested; mind the submenu-index caveat above |

**To test a path safely:** run `multiboot <idx> --dry-run`, confirm the printed "Pending one-shot selection" shows the expected `BootNext`/`bootsequence`/`next_entry`, clear it (see Usage), then try a real boot.

## Project layout

- `multiboot/cli.py` — all logic; behavior branches on `IS_WINDOWS` / `IS_LINUX`. Targets flow through `list_targets` → `resolve_target`/`auto_detect_target` → `arm_target`.
- `multiboot/tray.py` — optional Windows tray front-end that shells out to the CLI, elevating via `ShellExecuteW … runas`.

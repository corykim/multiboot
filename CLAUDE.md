# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`multiboot` is a cross-platform CLI that triggers a **one-shot reboot into the other OS** of a Windows/Linux dual-boot machine. It runs on *both* OSes, and nearly all behavior branches on which OS it is currently running on (`IS_WINDOWS` / `IS_LINUX` in `multiboot/cli.py`). When reasoning about any function, first ask which OS path it belongs to — the two paths use completely different mechanisms.

## Commands

The project uses [uv](https://docs.astral.sh/uv/). There is no `python` on PATH in this environment; use `uv run`.

```bash
uv run multiboot list            # show boot entries (works on both OSes)
uv run multiboot to-linux        # (Windows, Admin) arm next boot to Linux + reboot
uv run multiboot to-windows      # (Linux)          arm next boot to Windows + reboot
uv run multiboot to-linux --dry-run   # arm the next boot but SKIP the reboot — use this to test
```

- Both reboot commands take `--dry-run`; it arms the next boot but skips the reboot. **It still mutates real boot state** (sets the firmware `bootsequence` on Windows / grubenv `next_entry` on Linux) — it only skips the actual reboot. Clear a stray arming with `bcdedit /deletevalue {fwbootmgr} bootsequence` (Windows) or `grub-editenv - unset next_entry` (Linux).
- `to-linux`/`list` on Windows **require Administrator**; the code checks and exits otherwise.
- Install the tray app: `uv tool install --editable ".[tray]"` (exposes `multiboot-tray`).

There are no tests, linter, or formatter configured. To sanity-check changes without a reboot, prefer `--dry-run`, or syntax/logic-check pure functions directly, e.g.:

```bash
uv run python -c "import ast; ast.parse(open('multiboot/cli.py').read()); print('ok')"
```

## Architecture

Two files under `multiboot/`: `cli.py` (all logic) and `tray.py` (optional Windows system-tray front-end that just shells out to the `multiboot` CLI, elevating via `ShellExecuteW ... runas`).

The two reboot directions deliberately use **different boot mechanisms**, because what's reachable differs by OS:

- **Windows → Linux** (`cmd_to_linux`, `cmd_list_windows`): drives the **UEFI firmware boot menu** via `bcdedit`. `bcdedit /enum firmware` lists entries; `bcdedit /set {fwbootmgr} bootsequence {GUID}` arms a one-shot boot of the GRUB ("ubuntu") entry. The firmware boots GRUB once, GRUB boots its default (Linux), then reverts to Windows on the next boot.

- **Linux → Windows** (`cmd_to_windows`): arms GRUB's one-shot `next_entry` via `grub-reboot <title>`, auto-detecting the Windows menuentry **title** from `grub.cfg` (not a fragile index). Falls back to `grub-editenv <grubenv> set next_entry=…`, then to a manual `build_grubenv` write. Re-execs under `sudo` up front, warns via `warn_if_not_savedefault()` if `/etc/default/grub` lacks `GRUB_DEFAULT=saved`, and prints `grub-editenv list` afterward to verify. The real `grubenv` lives under `/boot/grub/`.

**Critical gotcha — do not reintroduce grubenv parsing on the Windows side.** On a standard Ubuntu+GRUB UEFI install the EFI System Partition holds only a *stub* `EFI\ubuntu\grub.cfg` (no `menuentry` lines) and **no `grubenv`**; the real `grub.cfg`/`grubenv` live on the **ext4** `/boot` partition, which Windows cannot read. An earlier version tried to read/write grubenv on the ESP from Windows and silently failed to detect GRUB — that's why the Windows path moved to `bcdedit`. The grubenv helpers (`parse_grubenv`, `build_grubenv`, `get_grubenv_path`, `parse_menu_entries`) are now **Linux-only**. `ensure_efi_mounted`/`_dump_efi` remain on Windows purely as a diagnostic when no Linux firmware entry is detected.

### Entry resolution

On Windows `--entry` accepts a `{GUID}`, a list index (as shown by `list`), or a case-insensitive firmware description substring. On Linux it accepts a GRUB menu entry title or index, passed straight to `grub-reboot` (default fallback: the title `"Windows"`). Auto-detection when omitted:
- `_SKIP_RE` filters out non-Linux firmware entries (Windows, firmware setup, memtest, EFI shell) when looking for Linux; `_WIN_RE` matches the Windows entry (by firmware description on Windows, by `grub.cfg` menuentry title on Linux).
- `_DISTRO_PRIORITY` + `_best_match` prefer a known distro name (e.g. `ubuntu`) over the first arbitrary candidate.

## GRUB configuration (required for reliable round-trips)

`grub-reboot`'s one-shot `next_entry` is **only honored when `GRUB_DEFAULT=saved`**, and the persistent default must resolve to **Linux** — otherwise Windows→Linux loops back to Windows (the firmware can only get you *into* GRUB; it can't pick the GRUB entry). Correct config:

```
# /etc/default/grub
GRUB_DEFAULT=saved        # keep — required by grub-reboot
GRUB_SAVEDEFAULT=false    # do NOT "remember last selection"; it drifts the default to Windows
```
then `sudo grub-set-default 0` (pin the saved default to Linux) and `sudo update-grub`. **Do not set `GRUB_DEFAULT=0`** — that drops the `next_entry` machinery and breaks `to-windows`.

## Current status (as of 2026-10-07)

- **Windows → Linux (`to-linux`) is verified** on the maintainer's machine — confirmed one-shot boot into GRUB/Linux and automatic revert (full round trip works).
- **Linux → Windows (`cmd_to_windows`) is freshly rewritten** to the `grub-reboot`-by-title design above and **not yet tested on Linux** — that's the next task. On Ubuntu: run `multiboot to-windows --dry-run`, confirm the printed `grub-editenv list` shows `next_entry` = the Windows entry, then test for real.
- The maintainer SSHes into the Windows host as an admin user; that SSH session already holds a full admin token, so `multiboot to-linux` runs directly there — no `sudo`/UAC/scheduled-task workaround needed.

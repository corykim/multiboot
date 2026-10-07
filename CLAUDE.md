# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`multiboot` is a cross-platform CLI that triggers a **one-shot reboot into any boot entry** of a multi-boot machine, from either Windows or Linux. It runs on *both* OSes, and nearly all behavior branches on which OS it is currently running on (`IS_WINDOWS` / `IS_LINUX` in `multiboot/cli.py`). When reasoning about any function, first ask which OS path it belongs to — the mechanisms differ completely.

`to-linux`/`to-windows` are thin convenience wrappers that auto-detect a target and call the same machinery as the general `boot <entry>` command.

## Commands

The project uses [uv](https://docs.astral.sh/uv/). There is no `python` on PATH in this environment; use `uv run`.

```bash
uv run multiboot list            # show all boot targets arm-able from this OS
uv run multiboot boot 2          # one-shot boot target #2 (from list), then reboot
uv run multiboot boot ubuntu     # ...by name substring; also accepts a {GUID}
uv run multiboot to-linux        # convenience: auto-detect + boot Linux
uv run multiboot to-windows      # convenience: auto-detect + boot Windows
uv run multiboot boot 2 --dry-run   # arm the next boot but SKIP the reboot — use when testing
```

- `boot`/`to-linux`/`to-windows` take `--dry-run`; it arms the next boot but skips the reboot. **It still mutates real boot state** (firmware `bootsequence` / BCD `bootsequence` on Windows, `BootNext` / grubenv `next_entry` on Linux) — it only skips the actual reboot. Clear a stray arming with `bcdedit /deletevalue {fwbootmgr} bootsequence` (and `{bootmgr}` for a BCD target) on Windows, or `efibootmgr --delete-bootnext` / `grub-editenv - unset next_entry` on Linux.
- On Windows everything **requires Administrator** (bcdedit); on Linux `boot`/`to-*` re-exec under `sudo`. The code checks/elevates and exits otherwise.
- Output: `main()` reconfigures stdout/stderr to UTF-8 (Windows consoles default to cp1252). Still, **keep printed strings ASCII** — the rewrite dropped the `→`/`◄`/`…` glyphs that crashed the cp1252 console.
- Install the tray app: `uv tool install --editable ".[tray]"` (exposes `multiboot-tray`).

There are no tests, linter, or formatter configured. To sanity-check changes without a reboot, prefer `--dry-run`, or syntax/logic-check pure functions directly, e.g.:

```bash
uv run python -c "import ast; ast.parse(open('multiboot/cli.py').read()); print('ok')"
```

## Architecture

Two files under `multiboot/`: `cli.py` (all logic) and `tray.py` (optional Windows system-tray front-end that just shells out to the `multiboot` CLI, elevating via `ShellExecuteW ... runas`).

### The unified target model

Everything flows through a list of **targets** — a dict `{idx, kind, id, name}` — built by `list_targets()` for the current OS. `resolve_target()` maps a user query (list index / `{GUID}` / name substring) to one; `auto_detect_target()` picks the obvious Linux/Windows one (preferring firmware entries, then `_best_match` by distro name); `arm_target()` arms it; `_arm_and_reboot()` arms + verifies (`show_pending_state()`) + reboots. `cmd_boot` and the two `cmd_to_*` wrappers are thin shells over these.

A target lives at one of **two layers**, and `arm_target()` dispatches on `kind`:

- **`kind="firmware"`** — a UEFI NVRAM boot entry (one per *bootloader*). Enumerated by `bcdedit /enum firmware` (Windows) or `efibootmgr` (Linux, `get_efi_bootnum_entries`). Armed one-shot by `bcdedit /set {fwbootmgr} bootsequence {GUID}` (Windows, `set_firmware_bootnext`) or `efibootmgr --bootnext <hexnum>` (Linux). This is the cross-OS layer — the same NVRAM list is visible/settable from either side.
- **`kind="bcd"`** (Windows only) — a Windows install under one Windows Boot Manager, from `bcdedit /enum osloader` (`get_bcd_osloaders`, skips recovery/ramdisk entries). Armed by `bcdedit /bootsequence {GUID}` (one-time OS-loader order) **plus** `set_firmware_bootnext("{bootmgr}")` so the firmware runs Windows Boot Manager next.
- **`kind="grub"`** (Linux only) — a GRUB menuentry from `grub.cfg` (`parse_menu_entries`; os-prober can nest other OSes here). Armed by `grub-reboot <title>` (`set_next_entry_linux`, with `grub-editenv`/manual `build_grubenv` fallbacks) **plus** best-effort `_maybe_arm_firmware_for_grub()` to point `BootNext` at the GRUB firmware entry. Requires `GRUB_DEFAULT=saved` (see below); `warn_if_not_savedefault()` flags it.

The Layer-2 kinds (`bcd`, `grub`) also arm Layer-1 so the right bootloader runs first — that's the key subtlety when the firmware default isn't the bootloader that owns the target.

**Critical gotcha — do not reintroduce grubenv parsing on the Windows side.** On a standard Ubuntu+GRUB UEFI install the EFI System Partition holds only a *stub* `EFI\ubuntu\grub.cfg` (no `menuentry` lines) and **no `grubenv`**; the real `grub.cfg`/`grubenv` live on the **ext4** `/boot` partition, which Windows cannot read. An earlier version tried to read/write grubenv on the ESP from Windows and silently failed to detect GRUB — that's why Windows never uses the `grub`/`grubenv` paths. The grubenv helpers (`parse_grubenv`, `build_grubenv`, `get_grubenv_path`, `parse_menu_entries`, `set_next_entry_linux`) are **Linux-only**. `ensure_efi_mounted`/`_dump_efi` remain on Windows purely as a diagnostic when `list` finds no non-Windows firmware entry.

### Entry resolution & detection

`resolve_target()` accepts a `{GUID}`, a **list index** (the `Idx` column — a bare number always means the list index, never a hex boot num), or a case-insensitive name substring. Auto-detection (`auto_detect_target`):
- `_SKIP_RE` filters out non-Linux entries (Windows, firmware setup, memtest, EFI shell, USB/network, recovery) when looking for Linux; `_WIN_RE` matches Windows.
- Firmware-kind targets are preferred over Layer-2 ones; then `_DISTRO_PRIORITY` + `_best_match` prefer a known distro name (`ubuntu`, `fedora`, `rocky`, …) over the first arbitrary candidate.

## GRUB configuration (required for reliable round-trips)

`grub-reboot`'s one-shot `next_entry` is **only honored when `GRUB_DEFAULT=saved`**, and the persistent default must resolve to **Linux** — otherwise Windows→Linux loops back to Windows (the firmware can only get you *into* GRUB; it can't pick the GRUB entry). Correct config:

```
# /etc/default/grub
GRUB_DEFAULT=saved        # keep — required by grub-reboot
GRUB_SAVEDEFAULT=false    # do NOT "remember last selection"; it drifts the default to Windows
```
then `sudo grub-set-default 0` (pin the saved default to Linux) and `sudo update-grub`. **Do not set `GRUB_DEFAULT=0`** — that drops the `next_entry` machinery and breaks `to-windows`.

## Current status & testing plan (as of 2026-10-07)

The maintainer's machine has **only two boot targets** — one firmware entry (`Ubuntu`) and one BCD osloader (`Windows 11`) — so the multi-OS paths cannot be fully exercised here. The maintainer SSHes into the Windows host as an admin user; that SSH session already holds a full admin token, so `multiboot` runs directly there — no `sudo`/UAC/scheduled-task workaround needed.

**Verified (Windows side):**
- `list` enumerates both layers (firmware `Ubuntu` + BCD `Windows 11`).
- Windows → Linux round trip (firmware `bootsequence` → GRUB → Ubuntu, then revert). Confirmed by the maintainer.
- `boot "Windows 11"` / `to-windows` arm the BCD `/bootsequence` + firmware→`{bootmgr}` (verified via `--dry-run` state readback; **not** boot-tested — only one Windows install exists here).

**Not yet tested — needs specific hardware:**
- **Windows → a *second* Windows** (the `bcd` path actually booting the chosen install): needs two Windows installs under one Windows Boot Manager (e.g. Win11/Win10).
- **Entire Linux side** (we developed it on Windows): `list` via `efibootmgr` + `grub.cfg`; `firmware` targets via `efibootmgr --bootnext`; `grub` targets via `grub-reboot` + `_maybe_arm_firmware_for_grub()`; `to-linux`/`to-windows` auto-detect. Requires `GRUB_DEFAULT=saved`.
- **Multi-distro** (e.g. Ubuntu → Rocky): works via firmware entries if each distro has its own UEFI entry; via `grub` entries if nested under one GRUB. **Submenu caveat:** `list`'s flat index may not match `grub-reboot`'s `submenu>entry` numbering — prefer the title for nested entries.

**How to test on Linux (safe):** run as root; for each target class do `multiboot boot <idx> --dry-run`, confirm the printed "Pending one-shot selection" shows the expected `BootNext` (firmware) or `next_entry` (grub), then clear with `efibootmgr --delete-bootnext` / `grub-editenv - unset next_entry` before trying a real boot.

**Known unrelated bug:** `tray.py`'s `on_list` references undefined `PYTHON`/`SCRIPT` and passes `list --dry-run` (which `list` no longer accepts); the tray's "List" item is broken. Out of scope so far.

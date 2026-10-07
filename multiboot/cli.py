#!/usr/bin/env python3
"""multiboot — reboot between Windows and Linux from either OS.

Windows (run as Administrator):
  python -m multiboot list
  python -m multiboot to-linux
  python -m multiboot to-linux --entry ubuntu

Linux (run with sudo, or it re-execs itself with sudo):
  python -m multiboot list
  python -m multiboot to-windows
  python -m multiboot to-windows --entry 2
"""

import argparse
import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

GRUBENV_SIZE = 1024
GRUBENV_HEADER = "# GRUB Environment Block\n"
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
EFI_LETTER = "Z"  # drive letter used when we mount the ESP ourselves

# ── grubenv read/write ─────────────────────────────────────────────────────

def parse_grubenv(data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    if not text.startswith(GRUBENV_HEADER):
        raise ValueError("Not a valid grubenv file (bad header).")
    result = {}
    for line in text[len(GRUBENV_HEADER):].splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip()
    return result

def build_grubenv(entries: dict) -> bytes:
    body = GRUBENV_HEADER + "".join(f"{k}={v}\n" for k, v in entries.items())
    raw = body.encode("utf-8")
    pad = GRUBENV_SIZE - len(raw)
    if pad < 0:
        raise ValueError("grubenv content exceeds 1024 bytes.")
    return raw + b"#" * pad

# ── Windows EFI partition ──────────────────────────────────────────────────

_efi_letter: Optional[str] = None
_we_mounted_efi: bool = False

def windows_is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def ensure_efi_mounted() -> str:
    global _efi_letter, _we_mounted_efi
    if _efi_letter:
        return _efi_letter
    # Check if EFI is already accessible under any drive letter
    for ltr in "ZYXWVUTSRQPONMLKJIHGFEDCBA":
        if (Path(f"{ltr}:\\") / "EFI").exists():
            _efi_letter, _we_mounted_efi = f"{ltr}:", False
            return _efi_letter
    # Mount it
    r = subprocess.run(["mountvol", f"{EFI_LETTER}:", "/s"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PermissionError(
            "Cannot mount EFI partition — re-run as Administrator.\n"
            f"  mountvol said: {r.stderr.strip()}"
        )
    _efi_letter, _we_mounted_efi = f"{EFI_LETTER}:", True
    return _efi_letter

def release_efi():
    global _efi_letter, _we_mounted_efi
    if _we_mounted_efi and _efi_letter:
        subprocess.run(["mountvol", _efi_letter, "/d"], capture_output=True)
    _efi_letter, _we_mounted_efi = None, False

# ── Windows UEFI firmware boot entries (bcdedit) ────────────────────────────
#
# On a normal Ubuntu+GRUB UEFI install the ESP only holds a stub grub.cfg and
# no grubenv — the real grub.cfg/grubenv live on the ext4 /boot partition that
# Windows can't read. So from Windows we drive the firmware boot menu instead:
# `bcdedit /enum firmware` lists the UEFI entries (including "ubuntu"), and
# `bcdedit /set {fwbootmgr} bootsequence {guid}` arms a one-shot boot of GRUB.

FWBOOTMGR = "{fwbootmgr}"

def _run_bcdedit(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["bcdedit"] + args, capture_output=True, text=True)

def parse_firmware_entries(text: str) -> list:
    """Parse `bcdedit /enum firmware` into [{id, description}] blocks."""
    entries, cur = [], {}
    def flush():
        if cur.get("id"):
            entries.append(dict(cur))
    for raw in text.splitlines():
        if not raw.strip():
            flush(); cur.clear(); continue
        m = re.match(r"(\S+)\s+(.*)", raw)   # key at col 0, value after it
        if not m:
            continue                          # dashed rules / continuation lines
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "identifier":
            cur["id"] = val
        elif key == "description":
            cur["description"] = val
    flush()
    return entries

def get_firmware_entries() -> list:
    r = _run_bcdedit(["/enum", "firmware"])
    if r.returncode != 0:
        raise PermissionError(
            "bcdedit /enum firmware failed — re-run as Administrator.\n"
            f"  bcdedit said: {(r.stderr or r.stdout).strip()}"
        )
    return parse_firmware_entries(r.stdout)

def selectable_firmware_entries(entries: list) -> list:
    """Firmware entries with a human description, minus the boot managers."""
    out = []
    for e in entries:
        desc = e.get("description")
        if not desc:
            continue
        if e["id"].lower() in ("{fwbootmgr}", "{bootmgr}"):
            continue
        out.append(e)
    return out

def auto_detect_firmware(entries: list, want_linux: bool) -> Optional[dict]:
    named = selectable_firmware_entries(entries)
    if want_linux:
        cands = [e for e in named if not _SKIP_RE.search(e["description"])]
        return _best_match(cands, lambda e: e["description"])
    cands = [e for e in named if _WIN_RE.search(e["description"])]
    return cands[0] if cands else None

def resolve_firmware_entry(entries: list, entry: str) -> Optional[dict]:
    """Resolve a user --entry (GUID, index, or description substring)."""
    named = selectable_firmware_entries(entries)
    if entry.startswith("{") and entry.endswith("}"):
        for e in entries:
            if e["id"].lower() == entry.lower():
                return e
        return None
    if entry.lstrip("-").isdigit():
        i = int(entry)
        return named[i] if 0 <= i < len(named) else None
    for e in named:                           # case-insensitive substring
        if entry.lower() in e["description"].lower():
            return e
    return None

def set_firmware_bootnext(guid: str):
    r = _run_bcdedit(["/set", FWBOOTMGR, "bootsequence", guid])
    if r.returncode != 0:
        raise RuntimeError(
            "bcdedit failed to set one-shot boot entry.\n"
            f"  bcdedit said: {(r.stderr or r.stdout).strip()}"
        )

# ── locate grubenv + grub.cfg ──────────────────────────────────────────────

_DISTRO_PRIORITY = ("ubuntu", "fedora", "arch", "manjaro", "debian",
                    "opensuse", "grub", "grub2")

def _best_match(candidates: list, key) -> Optional[Path]:
    if not candidates:
        return None
    for name in _DISTRO_PRIORITY:
        for c in candidates:
            if key(c).lower() == name:
                return c
    return candidates[0]

def get_grubenv_path() -> Path:
    if IS_WINDOWS:
        letter = ensure_efi_mounted()
        root = Path(letter + "\\")
        found = sorted(root.glob("EFI/*/grubenv"))
        p = _best_match(found, lambda c: c.parent.name)
        if p is None:
            raise FileNotFoundError(
                f"No grubenv under {root}EFI\\*\\\n"
                "Is GRUB your bootloader?"
            )
        return p
    else:
        for p in ("/boot/grub/grubenv", "/boot/grub2/grubenv",
                  "/boot/efi/EFI/grub/grubenv"):
            if Path(p).exists():
                return Path(p)
        raise FileNotFoundError("No grubenv found — is GRUB installed?")

def get_grubcfg_path() -> Optional[Path]:
    if IS_WINDOWS:
        letter = ensure_efi_mounted()
        root = Path(letter + "\\")
        found = sorted(root.glob("EFI/*/grub.cfg"))
        return _best_match(found, lambda c: c.parent.name)
    else:
        for p in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"):
            if Path(p).exists():
                return Path(p)
        return None

# ── parse grub.cfg ─────────────────────────────────────────────────────────

def parse_menu_entries(grub_cfg: Path) -> list:
    entries, depth = [], 0
    for line in grub_cfg.read_text(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("menuentry "):
            m = re.match(r'menuentry\s+[\'"]([^\'"]+)[\'"]', s)
            if m:
                entries.append({"name": m.group(1),
                                "index": len(entries),
                                "depth": depth})
        elif s.startswith("submenu "):
            depth += 1
        elif s == "}":
            depth = max(0, depth - 1)
    return entries

_SKIP_RE = re.compile(
    r"windows|uefi firmware|memtest|diagnostics|efi shell", re.I
)
_WIN_RE = re.compile(r"windows", re.I)

def auto_detect_entry(entries: list, want_linux: bool) -> Optional[dict]:
    if want_linux:
        candidates = [e for e in entries if not _SKIP_RE.search(e["name"])]
    else:
        candidates = [e for e in entries if _WIN_RE.search(e["name"])]
    return candidates[0] if candidates else None

# ── reboot ─────────────────────────────────────────────────────────────────

def do_reboot(dry_run: bool):
    if dry_run:
        print("  [dry-run] skipping reboot")
        return
    print("Rebooting…")
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/r", "/t", "0"])
    else:
        os.execvp("sudo", ["sudo", "reboot"])

# ── commands ───────────────────────────────────────────────────────────────

def _dump_efi(letter: str):
    """Print the EFI partition tree to help diagnose missing grubenv."""
    root = Path(letter + "\\") / "EFI"
    print(f"\nEFI partition contents ({letter}\\EFI\\):")
    if not root.exists():
        print("  (EFI directory not found — wrong partition?)")
        return
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root.parent)
        indent = "  " + "  " * (len(rel.parts) - 1)
        print(f"{indent}{p.name}{'/' if p.is_dir() else ''}")


def cmd_list_windows(args):
    if not windows_is_admin():
        sys.exit("Re-run as Administrator to read the firmware boot entries.")
    entries = get_firmware_entries()
    named   = selectable_firmware_entries(entries)
    linux   = auto_detect_firmware(entries, want_linux=True)

    print("UEFI firmware boot entries (bcdedit /enum firmware):")
    print(f"  {'Idx':<4}  {'Description':<32}  Identifier")
    print("  " + "-" * 70)
    if not named:
        print("  (no named firmware entries found)")
    for i, e in enumerate(named):
        mark = " ◄ Linux" if linux and e["id"] == linux["id"] else ""
        print(f"  {i:<4}  {e['description']:<32}  {e['id']}{mark}")

    if not any(not _SKIP_RE.search(e["description"]) for e in named):
        letter = ensure_efi_mounted()
        try:
            _dump_efi(letter)
        finally:
            release_efi()
        print("\nNo Linux/GRUB firmware entry detected — is Ubuntu's UEFI "
              "entry present? (See ESP contents above.)")


def cmd_list(args):
    if IS_WINDOWS:
        return cmd_list_windows(args)

    try:
        genv = get_grubenv_path()
    except FileNotFoundError as exc:
        sys.exit(f"\n{exc}")

    env  = parse_grubenv(genv.read_bytes())
    next_e  = env.get("next_entry", "(default)")
    saved_e = env.get("saved_entry")

    print(f"grubenv    : {genv}")
    print(f"next_entry : {next_e}")
    if saved_e:
        print(f"saved_entry: {saved_e}")

    cfg = get_grubcfg_path()
    if cfg:
        entries = parse_menu_entries(cfg)
        print(f"\ngrub.cfg: {cfg}")
        print(f"  {'Idx':<4}  Entry")
        print("  " + "-" * 56)
        for e in entries:
            active = " ◄" if (str(e["index"]) == str(next_e)
                              or e["name"] == next_e) else ""
            indent = "  " * e["depth"]
            print(f"  {e['index']:<4}  {indent}{e['name']}{active}")
    else:
        print("\n(grub.cfg not found — cannot list entries)")


def cmd_to_linux(args):
    if not IS_WINDOWS:
        sys.exit("'to-linux' runs on Windows. On Linux use 'to-windows'.")
    if not windows_is_admin():
        sys.exit("Re-run as Administrator to set the firmware boot entry.")

    entries = get_firmware_entries()

    if args.entry is not None:
        target = resolve_firmware_entry(entries, args.entry)
        if target is None:
            sys.exit(f"No firmware entry matched --entry {args.entry!r}. "
                     "Run 'multiboot list' to see available entries.")
    else:
        target = auto_detect_firmware(entries, want_linux=True)
        if target is None:
            try:
                letter = ensure_efi_mounted()
                _dump_efi(letter)
            finally:
                release_efi()
            sys.exit("\nCould not auto-detect a Linux/GRUB firmware entry.\n"
                     "Run 'multiboot list' and pass one with --entry.")
        print(f"Auto-detected Linux entry: {target['description']} "
              f"({target['id']})")

    set_firmware_bootnext(target["id"])
    print(f"Armed one-shot boot: {target['description']} ({target['id']})")

    do_reboot(args.dry_run)


def _grub_editenv_bin() -> Optional[str]:
    return shutil.which("grub-editenv") or shutil.which("grub2-editenv")

def warn_if_not_savedefault():
    """grub-reboot's next_entry is only honored when GRUB_DEFAULT=saved."""
    cfg = Path("/etc/default/grub")
    try:
        text = cfg.read_text(errors="replace")
    except OSError:
        return
    val = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") or not s.startswith("GRUB_DEFAULT="):
            continue
        val = s.split("=", 1)[1].strip().strip('"').strip("'")
    if val != "saved":
        shown = repr(val) if val is not None else "unset (defaults to 0)"
        print(f"WARNING: {cfg} has GRUB_DEFAULT={shown}, not 'saved'.\n"
              "  grub-reboot sets next_entry, which GRUB only honors when "
              "GRUB_DEFAULT=saved —\n  the next boot may ignore it. "
              "Fix: set GRUB_DEFAULT=saved and run 'sudo update-grub'.")

def detect_windows_title(fallback: str) -> str:
    """Resolve the Windows menuentry title from grub.cfg (robust vs. index)."""
    cfg = get_grubcfg_path()
    if cfg:
        e = auto_detect_entry(parse_menu_entries(cfg), want_linux=False)
        if e:
            print(f"Auto-detected Windows entry: {e['name']!r}")
            return e["name"]
    print(f"No Windows entry auto-detected; using {fallback!r}")
    return fallback

def print_grubenv():
    """Echo current grubenv for verification (mirrors `grub-editenv list`)."""
    editenv = _grub_editenv_bin()
    if editenv:
        subprocess.run([editenv, "list"])
        return
    try:
        genv = get_grubenv_path()
        print(f"grubenv ({genv}):")
        for k, v in parse_grubenv(genv.read_bytes()).items():
            print(f"  {k}={v}")
    except FileNotFoundError:
        pass

def set_next_entry_linux(target: str):
    """Arm a one-shot boot of `target` via grub-reboot, with fallbacks."""
    grub_reboot = shutil.which("grub-reboot") or shutil.which("grub2-reboot")
    if grub_reboot:
        r = subprocess.run([grub_reboot, target])
        if r.returncode == 0:
            return
        print(f"{grub_reboot} failed (rc={r.returncode}); "
              "falling back to grubenv write…")

    genv = get_grubenv_path()
    editenv = _grub_editenv_bin()
    if editenv:
        r = subprocess.run([editenv, str(genv), "set", f"next_entry={target}"])
        if r.returncode != 0:
            sys.exit(f"grub-editenv failed to set next_entry (rc={r.returncode}).")
        return
    env = parse_grubenv(genv.read_bytes())          # last-resort manual write
    env["next_entry"] = target
    genv.write_bytes(build_grubenv(env))

def cmd_to_windows(args):
    if IS_WINDOWS:
        sys.exit("'to-windows' runs on Linux. On Windows use 'to-linux'.")

    # grub-reboot, reading grub.cfg, and writing grubenv all need root.
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

    warn_if_not_savedefault()

    target = args.entry if args.entry is not None \
        else detect_windows_title(args.default_entry)

    set_next_entry_linux(target)
    print(f"Armed one-shot boot: next_entry = {target!r}")
    print_grubenv()                                 # verify, like your manual step

    do_reboot(args.dry_run)

# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="multiboot",
        description="One-shot reboot between Windows and Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  (Windows, Admin)  multiboot list\n"
            "  (Windows, Admin)  multiboot to-linux\n"
            "  (Windows, Admin)  multiboot to-linux --entry 0\n"
            "  (Linux)           multiboot list\n"
            "  (Linux)           multiboot to-windows\n"
            "  (Linux)           multiboot to-windows --entry 2\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # Shared --dry-run so it works *after* the subcommand (to-linux --dry-run).
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true",
                     help="Arm the next boot but skip the actual reboot")

    sub.add_parser("list", help="Show GRUB menu entries and grubenv state")

    p_lin = sub.add_parser("to-linux", parents=[dry],
                            help="(Windows) Set next boot to Linux, then reboot")
    p_lin.add_argument("--entry", metavar="GUID_IDX_OR_NAME",
                       help="Firmware entry: {GUID}, list index, or a "
                            "description substring (auto-detected if omitted)")

    p_win = sub.add_parser("to-windows", parents=[dry],
                            help="(Linux) Set next boot to Windows, then reboot")
    p_win.add_argument("--entry", metavar="TITLE_OR_IDX",
                       help="GRUB menu entry title or index, passed to "
                            "grub-reboot (auto-detected if omitted)")
    p_win.add_argument("--default-entry", metavar="TITLE", default="Windows",
                       help="Fallback title when auto-detect fails "
                            "(default: 'Windows')")

    args = p.parse_args()
    {"list": cmd_list, "to-linux": cmd_to_linux, "to-windows": cmd_to_windows}[args.cmd](args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""multiboot — one-shot reboot into any boot entry, from Windows or Linux.

  multiboot                   # list every boot target this OS can arm
  multiboot <idx|name|{GUID}> # one-shot boot that target, then reboot
  multiboot linux|windows     # convenience: auto-detect that OS and boot it

Targets span two layers: UEFI firmware entries (one per bootloader, settable
from either OS) and, within a bootloader, Windows BCD OS loaders (Windows side)
or GRUB menu entries (Linux side). On Windows run as Administrator; on Linux it
re-execs itself under sudo.
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
from typing import Optional

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

# ── Windows UEFI firmware + BCD entries (bcdedit) ───────────────────────────
#
# On a normal Ubuntu+GRUB UEFI install the ESP only holds a stub grub.cfg and
# no grubenv — the real grub.cfg/grubenv live on the ext4 /boot partition that
# Windows can't read. So from Windows we drive the firmware boot menu via
# `bcdedit /enum firmware` + `bcdedit /set {fwbootmgr} bootsequence {guid}`,
# and individual Windows installs via `bcdedit /enum osloader` + /bootsequence.

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

def get_bcd_osloaders() -> list:
    """Real Windows installs under the Windows Boot Manager (skip recovery)."""
    r = _run_bcdedit(["/enum", "osloader"])
    if r.returncode != 0:
        return []
    out, cur = [], {}
    def flush():
        desc, dev = cur.get("description"), cur.get("device", "")
        if cur.get("id") and desc and "ramdisk" not in dev.lower() \
           and "recovery" not in desc.lower():
            out.append({"id": cur["id"], "description": desc})
    for raw in r.stdout.splitlines():
        if not raw.strip():
            flush(); cur.clear(); continue
        m = re.match(r"(\S+)\s+(.*)", raw)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "identifier":
            cur["id"] = val
        elif key == "description":
            cur["description"] = val
        elif key == "device":
            cur["device"] = val
    flush()
    return out

def set_firmware_bootnext(guid: str):
    r = _run_bcdedit(["/set", FWBOOTMGR, "bootsequence", guid])
    if r.returncode != 0:
        raise RuntimeError(
            "bcdedit failed to set one-shot firmware boot entry.\n"
            f"  bcdedit said: {(r.stderr or r.stdout).strip()}"
        )

# ── locate grubenv + grub.cfg ──────────────────────────────────────────────

_DISTRO_PRIORITY = ("ubuntu", "fedora", "rocky", "almalinux", "arch",
                    "manjaro", "debian", "opensuse", "grub", "grub2")

def _best_match(candidates: list, key) -> Optional[object]:
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

# ── Linux UEFI firmware entries (efibootmgr) ────────────────────────────────

def get_efi_bootnum_entries() -> list:
    """UEFI NVRAM boot entries via efibootmgr → [{num, name}] (Linux)."""
    efi = shutil.which("efibootmgr")
    if not efi:
        return []
    r = subprocess.run([efi], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"Boot([0-9A-Fa-f]{4})\*?\s+(.+)", line)
        if m:
            out.append({"num": m.group(1),
                        "name": m.group(2).split("\t")[0].strip()})
    return out

_SKIP_RE = re.compile(
    r"windows|uefi firmware|firmware setup|memtest|diagnostics|efi shell|"
    r"usb device|network|recovery", re.I
)
_WIN_RE = re.compile(r"windows", re.I)

# ── reboot ─────────────────────────────────────────────────────────────────

def do_reboot(dry_run: bool):
    if dry_run:
        print("  [dry-run] skipping reboot")
        return
    print("Rebooting...")
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/r", "/t", "0"])
    else:
        os.execvp("sudo", ["sudo", "reboot"])

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

# ── boot targets (unified across layers) ─────────────────────────────────────
#
# A "target" is anything we can arm a one-shot boot of, independent of layer:
#   kind="firmware" — a UEFI NVRAM boot entry (one per bootloader). Settable
#                     from either OS. id = {GUID} (Windows) / hex num (Linux).
#   kind="bcd"      — a Windows BCD OS loader under one Windows Boot Manager
#                     (e.g. Win11 vs Win10). Windows only. id = {GUID}.
#   kind="grub"     — a GRUB menu entry (os-prober can nest other OSes here).
#                     Linux only. id = menuentry title.

def list_targets() -> list:
    """Every boot target arm-able from the *current* OS, with a unified index."""
    targets = []
    if IS_WINDOWS:
        for e in selectable_firmware_entries(get_firmware_entries()):
            targets.append({"kind": "firmware", "id": e["id"],
                            "name": e["description"]})
        for e in get_bcd_osloaders():
            targets.append({"kind": "bcd", "id": e["id"],
                            "name": e["description"]})
    else:
        for e in get_efi_bootnum_entries():
            targets.append({"kind": "firmware", "id": e["num"],
                            "name": e["name"]})
        cfg = get_grubcfg_path()
        if cfg:
            try:
                menu = parse_menu_entries(cfg)
            except OSError:
                menu = []
            for e in menu:
                targets.append({"kind": "grub", "id": e["name"],
                                "name": e["name"], "depth": e["depth"]})
    for i, t in enumerate(targets):
        t["idx"] = i
    return targets

def resolve_target(targets: list, query: str) -> Optional[dict]:
    """Resolve a query to a target: {GUID}, a `list` index, or name substring."""
    if query.startswith("{") and query.endswith("}"):
        for t in targets:
            if t["id"].lower() == query.lower():
                return t
        return None
    if query.lstrip("-").isdigit():          # a bare number = index from `list`
        for t in targets:
            if t["idx"] == int(query):
                return t
        return None
    for t in targets:                        # case-insensitive name substring
        if query.lower() in t["name"].lower():
            return t
    return None

def auto_detect_target(targets: list, want_linux: bool) -> Optional[dict]:
    """Pick the obvious Linux/Windows target, preferring firmware entries."""
    if want_linux:
        pool = [t for t in targets if not _SKIP_RE.search(t["name"])]
    else:
        pool = [t for t in targets if _WIN_RE.search(t["name"])]
    ranked = [t for t in pool if t["kind"] == "firmware"] or pool
    if not ranked:
        return None
    return _best_match(ranked, lambda t: t["name"]) if want_linux else ranked[0]

# ── arming ───────────────────────────────────────────────────────────────────

def _require_privileges():
    """Admin on Windows; on Linux re-exec under sudo if not already root."""
    if IS_WINDOWS:
        if not windows_is_admin():
            sys.exit("Re-run as Administrator.")
    elif os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

def _run_checked(cmd: list, label: str):
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"{label} failed (rc={r.returncode}).")

def arm_target(t: dict):
    """Arm a one-shot boot of `t` using the mechanism for its kind + OS."""
    kind, tid = t["kind"], t["id"]
    if kind == "firmware" and IS_WINDOWS:
        set_firmware_bootnext(tid)                       # {fwbootmgr} bootsequence
    elif kind == "firmware":                             # Linux
        _run_checked(["efibootmgr", "--bootnext", tid], "efibootmgr --bootnext")
    elif kind == "bcd":                                  # Windows only
        _run_checked(["bcdedit", "/bootsequence", tid], "bcdedit /bootsequence")
        set_firmware_bootnext("{bootmgr}")               # firmware -> Windows BM
        print("  (also armed firmware -> Windows Boot Manager so it runs next)")
    elif kind == "grub":                                 # Linux only
        warn_if_not_savedefault()
        set_next_entry_linux(tid)                        # grub-reboot <title>
        _maybe_arm_firmware_for_grub()
    else:
        sys.exit(f"Can't arm a {kind!r} target on {platform.system()}.")

def _maybe_arm_firmware_for_grub():
    """Best-effort: point UEFI BootNext at the local GRUB entry so the firmware
    actually runs GRUB next (needed only if the firmware default isn't GRUB)."""
    efi = shutil.which("efibootmgr")
    if not efi:
        return
    cand = _best_match(
        [e for e in get_efi_bootnum_entries() if not _SKIP_RE.search(e["name"])],
        lambda e: e["name"])
    if cand:
        subprocess.run([efi, "--bootnext", cand["num"]])
        print(f"  (also armed firmware BootNext -> {cand['name']!r})")

# ── Linux grubenv / grub-reboot helpers ──────────────────────────────────────

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
              "GRUB_DEFAULT=saved --\n  the next boot may ignore it. "
              "Fix: set GRUB_DEFAULT=saved and run 'sudo update-grub'.")

def set_next_entry_linux(target: str):
    """Arm a one-shot boot of GRUB menu entry `target` via grub-reboot."""
    grub_reboot = shutil.which("grub-reboot") or shutil.which("grub2-reboot")
    if grub_reboot:
        r = subprocess.run([grub_reboot, target])
        if r.returncode == 0:
            return
        print(f"{grub_reboot} failed (rc={r.returncode}); "
              "falling back to grubenv write...")

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

# ── verification / reboot ─────────────────────────────────────────────────────

def show_pending_state():
    """Echo the pending one-shot boot selection(s) for verification."""
    print("Pending one-shot selection:")
    if IS_WINDOWS:
        r = _run_bcdedit(["/enum", "{fwbootmgr}"])
        hits = [ln.strip() for ln in r.stdout.splitlines()
                if "bootsequence" in ln.lower()]
        print("  firmware " + (hits[0] if hits else "bootsequence (none)"))
    else:
        editenv = _grub_editenv_bin()
        if editenv:
            subprocess.run([editenv, "list"])
        efi = shutil.which("efibootmgr")
        if efi:
            r = subprocess.run([efi], capture_output=True, text=True)
            bn = [ln.strip() for ln in r.stdout.splitlines()
                  if ln.startswith("BootNext")]
            print("  " + (bn[0] if bn else "BootNext: (none)"))

def _arm_and_reboot(t: dict, dry_run: bool):
    print(f"Target: [{t['idx']}] {t['name']}  ({t['kind']}:{t['id']})")
    arm_target(t)
    print("Armed one-shot boot.")
    show_pending_state()
    do_reboot(dry_run)

# ── commands ───────────────────────────────────────────────────────────────

def cmd_list(args):
    if IS_WINDOWS and not windows_is_admin():
        sys.exit("Re-run as Administrator to read boot entries.")
    targets = list_targets()

    print(f"Boot targets on {platform.system()} "
          f"(boot one with: multiboot <idx|name>):")
    print(f"  {'Idx':<4} {'Kind':<9} {'Name':<34} Id")
    print("  " + "-" * 78)
    for t in targets:
        indent = "  " * t.get("depth", 0)
        print(f"  {t['idx']:<4} {t['kind']:<9} {indent}{t['name']:<34} {t['id']}")
    if not targets:
        print("  (no boot targets found)")

    show_pending_state()

    # Windows diagnostic: if nothing non-Windows showed up, dump the ESP tree.
    if IS_WINDOWS and not any(not _SKIP_RE.search(t["name"]) for t in targets):
        letter = ensure_efi_mounted()
        try:
            _dump_efi(letter)
        finally:
            release_efi()
        print("\nNo non-Windows firmware entry detected — is your other OS's "
              "UEFI entry present? (See ESP contents above.)")


def cmd_boot(args):
    _require_privileges()
    targets = list_targets()
    t = resolve_target(targets, args.target)
    if t is None:
        sys.exit(f"No boot target matched {args.target!r}. "
                 "Run 'multiboot list' to see targets.")
    _arm_and_reboot(t, args.dry_run)


def cmd_auto(args, want_linux: bool):
    """Convenience for `multiboot linux` / `multiboot windows`: auto-detect."""
    _require_privileges()
    t = auto_detect_target(list_targets(), want_linux=want_linux)
    if t is None:
        os_name = "Linux" if want_linux else "Windows"
        sys.exit(f"Could not auto-detect a {os_name} target. "
                 "Run 'multiboot list' and pass an index or name.")
    _arm_and_reboot(t, args.dry_run)

# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(
        prog="multiboot",
        description="One-shot reboot to any boot entry, from Windows or Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  multiboot                  # list all boot targets (default)\n"
            "  multiboot 0                # one-shot boot target #0, then reboot\n"
            '  multiboot "Windows 11"     # ...by name substring\n'
            "  multiboot {d7b25d8b-...}   # ...by firmware GUID\n"
            "  multiboot linux            # auto-detect a Linux target and boot it\n"
            "  multiboot 0 --dry-run      # arm it but don't reboot\n"
        ),
    )
    p.add_argument(
        "target", nargs="?", metavar="TARGET",
        help="'list' (default when omitted), 'linux'/'windows' to auto-detect, "
             "or a target to boot: index number, name substring, or {GUID} "
             "(see 'multiboot list')")
    p.add_argument("--dry-run", action="store_true",
                   help="Arm the next boot but skip the actual reboot")

    args = p.parse_args()
    tgt = (args.target or "list").lower()
    if tgt == "list":
        cmd_list(args)
    elif tgt in ("linux", "windows"):
        cmd_auto(args, want_linux=(tgt == "linux"))
    else:
        cmd_boot(args)


if __name__ == "__main__":
    main()

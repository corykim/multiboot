#!/usr/bin/env pythonw
"""multiboot tray icon — right-click to reboot to the other OS.

Install with:  uv tool install --editable "path/to/multiboot[tray]"
"""

import ctypes
import shutil
import sys

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "multiboot tray",
        "Missing dependencies.\n\n"
        "Re-install with:  uv tool install --editable \".[tray]\"",
    )
    sys.exit(1)

# ── helpers ────────────────────────────────────────────────────────────────

def _multiboot_exe() -> str:
    """Find the installed multiboot CLI executable."""
    exe = shutil.which("multiboot")
    if exe:
        return exe
    raise FileNotFoundError(
        "multiboot command not found on PATH.\n"
        "Run: uv tool install --editable <project-dir>"
    )

def _run_elevated(subcmd: str):
    """Run  multiboot <subcmd>  as Administrator via ShellExecuteW runas."""
    exe = _multiboot_exe()
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subcmd, None, 1)

def _run_in_terminal(subcmd: str):
    """Open an *elevated* PowerShell running `multiboot <subcmd>`, kept open so
    the output is readable. `list` needs Administrator (bcdedit)."""
    exe = _multiboot_exe()
    params = f"-NoExit -Command \"& '{exe}' {subcmd}\""
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                        params, None, 1)

# ── icon ───────────────────────────────────────────────────────────────────

def _make_icon(fg: str, label: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    # Circle background
    d.ellipse([2, 2, size - 2, size - 2], fill=fg)
    # Two-arrow symbol (↑↓) drawn as simple triangles
    mid = size // 2
    # Up arrow (top half)
    d.polygon([(mid, 10), (mid + 12, 28), (mid - 12, 28)], fill="white")
    # Down arrow (bottom half)
    d.polygon([(mid, size - 10), (mid + 12, size - 28),
               (mid - 12, size - 28)], fill="white")
    return img

# ── menu callbacks ─────────────────────────────────────────────────────────

def on_to_linux(icon, item):
    _run_elevated("linux")

def on_to_windows(icon, item):
    _run_elevated("windows")

def on_list(icon, item):
    _run_in_terminal("list")

def on_quit(icon, item):
    icon.stop()

# ── main ───────────────────────────────────────────────────────────────────

def main():
    icon_img = _make_icon("#3d85c8", "DB")

    menu = pystray.Menu(
        pystray.MenuItem("Reboot → Linux",   on_to_linux),
        pystray.MenuItem("Reboot → Windows", on_to_windows),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("List boot targets...", on_list),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        name  = "multiboot",
        icon  = icon_img,
        title = "Multiboot",
        menu  = menu,
    )
    icon.run()


if __name__ == "__main__":
    main()

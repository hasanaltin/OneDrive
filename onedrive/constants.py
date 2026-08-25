import ctypes.util
import os
from pathlib import Path

# --- FUSE library resolution -------------------------------------------------
# This system (and many recent distros) only ships libfuse3, not the legacy
# libfuse2 that most Python FUSE bindings assume. `refuse` has the correct
# libfuse3 ABI bindings but still relies on ctypes.util.find_library('fuse'),
# which only matches a `libfuse.so*` name — so we point it at libfuse3
# explicitly before it's ever imported anywhere in the app.
_FUSE3_CANDIDATES = [
    "/usr/lib/x86_64-linux-gnu/libfuse3.so.4",
    "/usr/lib/x86_64-linux-gnu/libfuse3.so.3",
    "/usr/lib/libfuse3.so.4",
    "/usr/lib64/libfuse3.so.4",
]


def ensure_fuse_library_path() -> None:
    if os.environ.get("FUSE_LIBRARY_PATH"):
        return
    found = ctypes.util.find_library("fuse3")
    if found:
        os.environ["FUSE_LIBRARY_PATH"] = found
        return
    for candidate in _FUSE3_CANDIDATES:
        if Path(candidate).exists():
            os.environ["FUSE_LIBRARY_PATH"] = candidate
            return
    # Leave unset; refuse will raise a clear EnvironmentError on import,
    # which is a better failure than silently mounting nothing.


# --- App identity -----------------------------------------------------------
DISPLAY_NAME = "OneDrive for Linux Client"
VERSION = "0.9.0"

# APP_NAME is used for on-disk paths (config/cache/data dirs, log file name,
# lock file, keyring service, FUSE fsname) and the Dolphin plugins' overlay
# socket name. Capitalized "OneDrive" deliberately, not lowercase - Linux
# paths are case-sensitive, so this still avoids colliding with any other
# separately-installed OneDrive CLI client's own ~/.config/onedrive/ while
# using the actual product name instead of a distinguishing suffix like
# the old "onedrive-native".
APP_NAME = "OneDrive"

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
XDG_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))

CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME
CACHE_DIR = XDG_CACHE_HOME / APP_NAME

# --- Auth ---------------------------------------------------------------------
# No app identity is baked into this project's source - every deployment
# brings its own Azure AD app registration (see register_azure_app.sh),
# read here from either an env var or a small config file it writes. This
# is deliberate: a single shared Client ID hardcoded in a public repo would
# mean every installer's traffic runs through one person's own Azure
# tenant/app forever - a single point of failure (and of trust: the consent
# screen would show that one person's identity to every stranger who
# installs this) that doesn't belong in an open-source project meant for
# people across unrelated organizations.
CLIENT_ID_ENV_VAR = "ONEDRIVE_NATIVE_CLIENT_ID"
CLIENT_ID_FILE = CONFIG_DIR / "client_id"


def _load_client_id() -> str | None:
    env_value = os.environ.get(CLIENT_ID_ENV_VAR, "").strip()
    if env_value:
        return env_value
    try:
        return CLIENT_ID_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


# None until register_azure_app.sh has been run (env var or CLIENT_ID_FILE
# populated) - auth.py.AuthManager._ensure_app() raises a clear, actionable
# error rather than letting MSAL fail confusingly on an unset client_id.
CLIENT_ID = _load_client_id()
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.ReadWrite", "Files.ReadWrite.All", "User.Read", "People.Read"]

DATA_DIR = XDG_DATA_HOME / APP_NAME

DB_PATH = DATA_DIR / "metadata.sqlite3"
CONTENT_CACHE_DIR = CACHE_DIR / "content"
TOKEN_CACHE_FILE = CONFIG_DIR / "token_cache.bin"
LOG_FILE = DATA_DIR / f"{APP_NAME}.log"
PROFILE_PHOTO_FILE = DATA_DIR / "profile_photo.jpg"

# Unix domain socket the Dolphin overlay-icon plugin (native KDE plugin,
# see packaging/dolphin-overlay/) connects to, asking "what's the sync
# status of this path?" - lives under runtime dir, not DATA_DIR, since a
# socket file (unlike the DB/cache) is meaningless to persist across a
# reboot and RUNTIME_DIR is guaranteed to be a private, per-session
# tmpfs already cleaned up by the OS.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
OVERLAY_SOCKET_PATH = RUNTIME_DIR / f"{APP_NAME}-overlay.sock"

DEFAULT_MOUNTPOINT = Path.home() / "OneDrive"

# --- Sync tuning ------------------------------------------------------------
DELTA_POLL_INTERVAL_SECONDS = 300
PIN_WORKER_INTERVAL_SECONDS = 1800
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# --- Offline-tolerant on-demand mount tuning ---------------------------------
MOUNT_SYNC_INTERVAL_SECONDS = 60

# --- Folder Pairs (two-way sync) tuning -------------------------------------
PAIR_SYNC_INTERVAL_SECONDS = 60
LOCAL_WATCH_DEBOUNCE_SECONDS = 2.0
# reconcile.py's bootstrap heuristic (a pair's very first sync pass) used to
# trust same-size existing-both-sides files as already-synced with no
# content check at all - two different files that happen to share a byte
# size would silently never get their content diff caught. Hardened to also
# compare quickxorhash.py's locally-computed hash against Graph's own
# quickXorHash for candidates up to this size. That module is pure Python
# (no compiled dependency - see its own docstring for why), measured at
# ~12 MB/s on this machine; capped here so a single huge candidate file
# can't stall an otherwise-quick bootstrap pass for a few seconds of hashing
# that only matters for this one heuristic. Anything larger falls back to
# the original same-size-only trust - same documented risk as before, just
# narrower now.
PAIR_BOOTSTRAP_HASH_MAX_BYTES = 25 * 1024 * 1024
SIMPLE_UPLOAD_MAX_BYTES = 4 * 1024 * 1024
# ~1.25 MiB, exact multiple of 320 KiB per Graph's guidance - was 32x this
# (~10 MiB) until confirmed directly that a real connection here was
# timing out every single chunk at a steady ~10-11s regardless of the
# read/write timeout tuning (raising it 60s -> 180s made no measurable
# difference to the interval, meaning something else - almost certainly
# the connection itself - won't reliably carry a request that long
# regardless of what timeout the client is willing to wait). A much
# smaller chunk gives every individual PUT a real chance of finishing
# before that connection-level ceiling, however it's actually enforced.
UPLOAD_CHUNK_SIZE = 4 * 327680
CONFLICT_COPY_SUFFIX_FORMAT = "{stem} (conflicted copy {ts}){suffix}"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR, CONTENT_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)


# Resolved as soon as this module is imported (rather than requiring every
# caller to remember to do it) since any module importing `refuse.high`
# needs FUSE_LIBRARY_PATH set *before* that import executes.
ensure_fuse_library_path()

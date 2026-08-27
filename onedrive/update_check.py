"""Git-based self-update for the "Check for Updates" button in the About
tab: checks the app's own git remote for a newer commit than what's
currently running, offers to `git pull` it, and can cleanly restart the
app afterward.

Deliberately git-based rather than polling GitHub's REST API for releases -
this project doesn't tag releases separately from ordinary commits (every
commit that lands on `main` is already what a user would want), and reusing
the same remote/credentials the user's own `git clone` already has
configured means this works the same way whether the repo is public or
private, with no separate API token/auth story to build.
"""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent


class UpdateCheckError(Exception):
    pass


def _run_git(*args: str, timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise UpdateCheckError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise UpdateCheckError(f"git {' '.join(args)} timed out") from None
    if result.returncode != 0:
        raise UpdateCheckError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def check_for_update() -> str | None:
    """Returns the new version string (or a short commit hash if the
    remote's constants.py couldn't be read/parsed) if an update is
    available, None if already up to date. Raises UpdateCheckError on any
    git/network failure, or if this checkout has local modifications
    (apply_update()'s `git pull --ff-only` would otherwise fail or, worse,
    silently need a merge) - the caller decides how to surface that."""
    if not (REPO_DIR / ".git").is_dir():
        raise UpdateCheckError("not a git checkout - can't check for updates this way")

    if _run_git("status", "--porcelain"):
        raise UpdateCheckError(
            "This checkout has local changes - resolve or discard them before updating."
        )

    _run_git("fetch", "origin", "main", timeout=30.0)
    local_head = _run_git("rev-parse", "HEAD")
    # FETCH_HEAD, not origin/main: this project's history has been squashed
    # and force-pushed to main more than once (a deliberate scrub of
    # sensitive content each time, not an accident) - real bug found live
    # on a second machine, where "Check for Updates" kept saying "up to
    # date" while its own About tab still showed an old version. `git
    # fetch origin main` given a bare ref name on the command line only
    # reliably updates FETCH_HEAD; whether it also force-updates the
    # refs/remotes/origin/main tracking ref for a non-fast-forward history
    # (exactly what a squash produces) isn't guaranteed the way it would be
    # for a plain `git fetch origin` using the clone's configured refspec.
    # A stale origin/main left over from before a squash happened to equal
    # the also-stale local HEAD, so the comparison below silently passed.
    # FETCH_HEAD has no such ambiguity - it's always exactly what this
    # fetch just retrieved.
    remote_head = _run_git("rev-parse", "FETCH_HEAD")
    if local_head == remote_head:
        return None

    try:
        remote_constants = _run_git("show", "FETCH_HEAD:onedrive/constants.py")
        match = re.search(r'VERSION\s*=\s*"([^"]+)"', remote_constants)
        if match:
            return match.group(1)
    except UpdateCheckError:
        pass
    return remote_head[:8]


def apply_update() -> None:
    """Pulls the update already confirmed available by check_for_update(),
    then reinstalls dependencies (a new version may have added one) -
    mirrors install.sh's own dependency step, safe to re-run (pip no-ops
    on anything already satisfied). Tries --ff-only first - this checkout
    is never meant to carry local commits, so a normal update should always
    be a fast-forward. If the remote history was ever rewritten upstream
    (e.g. a maintainer force-push), --ff-only can't reconcile that and every
    user's checkout would otherwise be stuck needing a git expert to
    unbreak it manually - so as a fallback, and only after re-confirming
    there are truly no local changes to lose, this force-syncs to whatever
    origin/main now is instead of leaving the update button broken."""
    try:
        _run_git("pull", "--ff-only", "origin", "main", timeout=60.0)
    except UpdateCheckError:
        if _run_git("status", "--porcelain"):
            raise UpdateCheckError(
                "Update can't fast-forward and this checkout has local changes - "
                "resolve or discard them before updating."
            )
        logger.warning("fast-forward update failed; force-syncing to origin/main instead")
        _run_git("fetch", "origin", "main", timeout=30.0)
        # FETCH_HEAD, not origin/main - same staleness risk as the one fixed
        # in check_for_update() above, and this path runs a hard reset, so
        # getting it wrong would be worse than just missing an update notice.
        _run_git("reset", "--hard", "FETCH_HEAD", timeout=15.0)

    venv_pip = REPO_DIR / ".venv" / "bin" / "pip"
    if not venv_pip.exists():
        return
    result = subprocess.run(
        [str(venv_pip), "install", "-r", str(REPO_DIR / "requirements.txt")],
        capture_output=True, text=True, timeout=180.0,
    )
    if result.returncode != 0:
        raise UpdateCheckError(f"pip install failed after update: {result.stderr.strip()}")


def restart_app(window) -> None:
    """Cleanly shuts down (unmount, stop background workers - the same
    path Quit already uses) and re-execs the current process in place.
    os.execv is deliberately used over spawning a detached child process:
    it works identically regardless of how this process was launched
    (systemd unit, desktop autostart, or a plain terminal) since nothing
    needs to be told to track a new PID, and it sidesteps any race with
    single_instance's flock entirely - Python opens files close-on-exec
    by default (PEP 446), so the lock's underlying fd is released by the
    same execv syscall that hands control to the new process image,
    confirmed directly rather than assumed (there's no window where both
    the old and new instance could exist and race for the lock)."""
    window._quit_app()
    # `python -m onedrive` only resolves the package because the process's
    # current working directory is the repo root (true today because the
    # .desktop file's Path= sets it, and execv inherits whatever cwd the
    # process already has - it doesn't accept one the way subprocess.Popen
    # does) - explicit chdir here makes that a guarantee rather than an
    # inherited assumption, so this restarts correctly no matter how the
    # app was actually launched or what its cwd happened to be at the time.
    os.chdir(REPO_DIR)
    python = sys.executable
    logger.info("restarting: exec %s -m onedrive (cwd=%s)", python, REPO_DIR)
    os.execv(python, [python, "-m", "onedrive"])

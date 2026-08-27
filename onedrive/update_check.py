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
    available, None if already up to date. Raises UpdateCheckError only on
    a genuine git/network failure or a missing .git directory - deliberately
    does NOT gate on local changes the way an earlier version of this
    function did. Whether apply_update() can cleanly land the update is
    apply_update()'s own problem to self-heal (see its docstring) - an end
    user has no realistic way to run git commands themselves ("bunun stabil
    olmasi lazim ... kullanicilar komut calistiramaz", reported directly),
    so this function's only job is to answer "is there a newer commit",
    accurately, regardless of what state the working tree happens to be in."""
    if not (REPO_DIR / ".git").is_dir():
        raise UpdateCheckError("not a git checkout - can't check for updates this way")

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
    on anything already satisfied).

    Always ends up matching origin/main exactly, however it gets there -
    this checkout is a deployed application's own clone, never meant to
    carry local commits or edits of its own, so there's nothing here worth
    preserving over a clean sync. An earlier version of this function
    raised an error and gave up whenever the tree had local changes or
    couldn't fast-forward - in practice that just left an ordinary end user
    staring at an error with no git command they could realistically be
    expected to run themselves (reported directly: this needs to be
    stable, users can't run commands). So every path below is automatic:
    try the clean fast-forward first (the common case, and the only one
    that can't lose anything even in principle), and if that doesn't apply
    - dirty tree, diverged history from a rewritten remote (this project's
    own main has been squashed and force-pushed more than once), or
    anything else - fall back to force-syncing this checkout to match
    FETCH_HEAD exactly instead of stopping to ask."""
    _run_git("fetch", "origin", "main", timeout=30.0)
    try:
        _run_git("merge", "--ff-only", "FETCH_HEAD", timeout=15.0)
    except UpdateCheckError:
        logger.warning("fast-forward update didn't apply cleanly - force-syncing to FETCH_HEAD instead")
        # FETCH_HEAD, not origin/main - same staleness risk fixed in
        # check_for_update() above, and this path forcibly overwrites the
        # checkout, so getting it wrong would be worse than just missing
        # an update.
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

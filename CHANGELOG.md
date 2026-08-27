# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.9.5] — The update mechanism could dead-end requiring manual git commands

### Changed
- **`check_for_update()`/`apply_update()` are now fully self-healing - reported directly: "this
  needs to be stable, in the future users can't run commands [if this happens again]."** Both
  functions used to give up and raise an error - "resolve or discard local changes", "can't
  fast-forward" - whenever the checkout wasn't in a pristine state, on the theory that a maintainer
  would decide how to handle it. In practice that just left an ordinary end user staring at an
  error with no git knowledge to act on it. This checkout is a deployed app's own clone, never
  meant to carry local edits or commits of its own, so there's nothing worth protecting over a
  clean sync: `check_for_update()` no longer blocks on local changes at all (it only answers "is
  there a newer commit", regardless of working-tree state), and `apply_update()` always ends up
  matching the remote exactly - tries a clean fast-forward first, and force-syncs to `FETCH_HEAD`
  for anything that doesn't fast-forward (a dirty tree, or history diverged by a rewritten
  remote - this project's own `main` has been squashed and force-pushed more than once). Verified
  directly against a simulated dirty-plus-diverged checkout before shipping.

## [0.9.4] — Remote changes on another machine took minutes to show up

### Changed
- **`DELTA_POLL_INTERVAL_SECONDS` lowered from 300s to 60s** - reported live: a file created on
  one machine took roughly 2 minutes to be noticed on another. Graph's `/delta` endpoint isn't
  pushed to us, so a remote change is only as fresh as the last poll; that 5-minute interval,
  stacked with `PairSyncWorker`'s own up-to-60s reconciliation cadence reading whatever was last
  cached, is exactly where a multi-minute delay of this size comes from. Now polls on the same
  60s cadence as `PairSyncWorker` itself - a delta call only returns what changed via a stored
  token, not a full re-listing, so polling it this often costs nothing meaningful. An immediate
  fix without waiting for either interval already existed and still works: "Sync Now" in the tray
  popup, or "Refresh now" in Choose Folders, both wake every worker on demand.

## [0.9.3] — "Check for Updates" could say "up to date" while genuinely behind

### Fixed
- **Real bug, reported live from a second machine:** its About tab showed an old version and
  clicking "Check for Updates" said it was already current, even though `main` on GitHub was
  several commits ahead. `check_for_update()` compared local `HEAD` against `origin/main` after an
  explicit `git fetch origin main` - but a bare ref name given on the command line only reliably
  updates `FETCH_HEAD`; whether it also force-updates the `refs/remotes/origin/main` tracking ref
  for a non-fast-forward history isn't guaranteed the way it is for a plain `git fetch origin`
  using the clone's own configured refspec. This project's history has been squashed and
  force-pushed to `main` more than once (deliberately, to scrub sensitive content - not an
  accident), which is exactly the kind of non-fast-forward change that can leave a stale
  `origin/main` behind. Now compares against `FETCH_HEAD` instead, which is always exactly what
  the fetch just retrieved, with no tracking-ref ambiguity. `apply_update()`'s force-sync fallback
  (`git reset --hard`) had the identical staleness risk and is fixed the same way.

## [0.9.2] — install.sh's Azure-app prompt didn't actually work when piped through bootstrap.sh

### Fixed
- **The "register an Azure app now?" prompt silently skipped itself under `bootstrap.sh`:**
  `install.sh` decided whether to ask interactively by checking `[ -t 0 ]` (is stdin a terminal),
  but running it via `bootstrap.sh`'s `curl ... | bash` puts the whole piped script on stdin, so
  by the time `install.sh` runs, fd 0 is exhausted and reads as non-interactive even in a plain
  terminal session someone is actively watching. It now asks via `/dev/tty` - the actual
  controlling terminal - instead, so the prompt (and the `register_azure_app.sh` it launches on
  "yes") works the same whether you ran `install.sh` directly or through the one-line bootstrap.
  Falls back to printing the manual command only when there's genuinely no terminal attached at
  all (a true headless/CI run). `bootstrap.sh`'s own closing summary now also checks whether the
  Client ID file actually got written, rather than unconditionally telling you to register an app
  you may have just registered one screen up.

## [0.9.1] — One-command install, and a fresh-distro Azure CLI install failure fixed

### Added
- **`bootstrap.sh`** - a single-command installer for a brand-new machine:
  `curl -fsSL https://raw.githubusercontent.com/hasanaltin/OneDrive/main/bootstrap.sh | bash`
  installs `git` if missing, clones the repo to `~/onedrive-linux-client` (or updates it in place
  if already cloned), and runs `install.sh`. Registering an Azure app and the app's own first
  sign-in remain one-time interactive steps by nature (a browser-based `az login` and a
  device-code prompt) - `bootstrap.sh` prints exactly what to run for both once it finishes.

### Fixed
- **`register_azure_app.sh` failed outright on a brand-new distro release:** Microsoft's official
  `curl | sudo bash` Azure CLI installer adds an apt source keyed to the running distro's
  codename, and their `packages.microsoft.com` repo doesn't always have `azure-cli` published for
  a codename the moment it's released - confirmed live as `E: Unable to locate package azure-cli`
  right after a fresh install of a just-released Ubuntu version. Now falls back to installing
  Azure CLI into a dedicated venv via `pip` instead, which only needs PyPI to have published a
  wheel, not a matching apt release.
- **README's Azure app setup section was out of date** - it still said API-permission granting was
  a manual portal step, but `register_azure_app.sh` already automates adding the permissions and
  granting admin consent (falling back to manual steps only if a call in that chain fails).
  Corrected to describe what the script actually does now.

## [0.9.0] — A Folder Pair bootstrap race could flag identical files as conflicts forever

### Fixed
- **Real bug, root-caused from a live report:** pairing an already-identical, Nextcloud-synced
  Pictures folder on a second PC produced hundreds of conflicts instead of silently recognizing
  the matching content, despite `reconcile.py`'s bootstrap same-content heuristic existing
  specifically to prevent that. `DeltaSyncWorker` and `PairSyncWorker` start with no
  synchronization barrier between them, so a brand-new pair's first ("bootstrap") reconcile pass
  can run while the initial whole-account delta crawl is still mid-flight - a file already on
  Graph but not yet in the local delta cache gets misread as local-only and routed to upload,
  Graph 409s "nameAlreadyExists" (correctly - it's not actually new), `graph_client`'s
  retry-as-replace lookup occasionally races Graph's own read-after-write consistency and comes
  back empty, and the resulting `GraphConflictError` was silently swallowed by the pair worker's
  generic handler (it only knows how to refresh an item it already has an id for, which a fresh
  create never does). The pass still stamped `last_sync_at`, permanently flipping the pair's
  bootstrap eligibility off - so the very next pass saw that same, however byte-identical, file
  with no bootstrap trust left to recognize it, and flagged a real conflict regardless of matching
  size/hash.
  - Two-part fix: `graph_client.upload_file()` now retries the post-409 lookup a couple of times
    with backoff before giving up (closes the race at its source in the common case), and
    `pair_worker._sync_one_pair()` now treats a still-unresolved case as a genuinely incomplete
    pass (same as pausing mid-batch) instead of stamping `last_sync_at` - keeps bootstrap
    eligibility truthful for the next pass instead of permanently burning it on a transient race.
  - Only prevents *future* false conflicts - doesn't retroactively clear ones already recorded
    before upgrading (see the bulk-resolve addition below).
- **Secondary bug found while tracing the above:** `_write_bootstrap_baselines` re-derived "already
  synced" baselines from the pre-pass local/remote snapshot with no check for paths that already
  got a real action (including a conflict) during that same pass, so it could silently overwrite a
  just-written, correct post-conflict baseline with stale pre-resolution data. Now takes the set of
  paths actually acted on this pass and skips them.
- **Conflicts dialog's bulk-resolve could silently stop at 200:** `db.list_conflicts()`'s
  `limit=200` default exists for the on-screen review list (loading e.g. 1,401 individual rows
  would be slow), but the new "Keep Local/Server (All N)" buttons reused that same capped query -
  clicking them only ever resolved the first page and silently left the rest, while the button
  label itself under-reported the true count too. Both now use `count_conflicts()` (the real
  total), and the bulk action explicitly re-queries with that total as the limit.
- **`install.sh`'s libfuse3 detection could report "not found" even when it was:** `set -o
  pipefail` combined with `grep -q`'s early exit on the first match sends the upstream `ldconfig`
  process a SIGPIPE, which `pipefail` was then treating as the whole check failing - a classic,
  easy-to-miss shell scripting gotcha. Fixed by capturing `ldconfig -p`'s output into a variable
  first and grepping that, instead of piping the two live.
- **`install.sh` silently trusted a broken `.venv` if the directory merely existed:** a partially-
  created venv (`python3 -m venv` failing partway through, e.g. missing `ensurepip`) or one copied
  from another machine without its executable permissions both left a `.venv/` that looked present
  but had no working `bin/pip` - the "already exists, reusing" fast path took that at face value
  and failed much later, confusingly, at the dependency-install step. Now verified with a real
  `-x bin/pip` check before being trusted, recreating from scratch otherwise.
- **`install.sh` calling `register_azure_app.sh` directly could fail with "Permission denied"**
  if that script's executable bit was lost in transit (the same file-transfer issue as the `.venv`
  case above) - now invoked via `bash register_azure_app.sh` regardless of its own permission bit.
- **`update_check.py`'s "Check for Updates" could report a false "local changes" block** because
  Claude Code's own `.claude/` session directory sat untracked and un-ignored in the repo -
  `.gitignore` now excludes it.

### Added
- **`install.sh` now actually installs the system packages it needs instead of just warning about
  them:** detects apt/dnf/pacman/zypper and installs FUSE 3, and - only if no prebuilt Python wheel
  is available for the running Python version - the C compiler, `pkg-config`, and the
  `libfuse3`/Python development headers needed to build `pyfuse3` from source, plus the
  `python3-venv`-equivalent package if venv creation fails for a missing `ensurepip`. Found and
  fixed each of these one at a time by actually running the fresh-clone install end to end on a
  real machine, not by inspection.
- **`install.sh` now offers to run `register_azure_app.sh` for you** right after a successful
  install if no Azure app is configured yet, instead of only printing the command to run later.
- **`register_azure_app.sh` now adds the required Microsoft Graph API permissions and grants
  admin consent automatically**, closing the one remaining manual portal step. Permission names
  are resolved to their GUIDs via a live lookup against the Microsoft Graph service principal
  itself (the same data the portal's own "Add a permission" search uses), not hardcoded from
  memory - falls back to the original manual instructions if anything can't be resolved or the
  signed-in account lacks the rights to consent.
- **Conflicts dialog: bulk "Keep Local (All)" / "Keep Server (All)" buttons**, alongside the
  existing per-row review - resolving hundreds of conflicts one at a time didn't scale (e.g.
  pairing an already-matching folder on a second device). Reuses the same permanent-delete warning
  the per-row confirm already shows, now stating the true affected count up front.
- **The self-update path (`apply_update()`) now self-heals if `git pull --ff-only` can't
  fast-forward** - e.g. after a maintainer history rewrite - instead of leaving every user's
  "Update Now" broken with no recovery short of manual git commands. Falls back to `fetch` +
  `reset --hard origin/main`, but only after re-confirming the checkout truly has no local changes
  to lose.

## [0.7.3] — Document the required Azure app permissions in README

### Added
- **README's Azure setup section now lists all four required Microsoft Graph delegated permissions
  explicitly** (`Files.ReadWrite`, `Files.ReadWrite.All`, `User.Read`, `People.Read`), each with a
  plain-language explanation of what it grants and why this app needs it - previously only
  `register_azure_app.sh`'s own printed output listed these, so anyone reviewing the README alone
  (without running the script) had no way to see the app's actual permission footprint.
  - Verified against [Microsoft's own permissions
    reference](https://learn.microsoft.com/en-us/graph/permissions-reference) rather than assumed:
    none of these four are classified as admin-only for delegated (as opposed to application/
    app-only) permissions - the "Grant admin consent" step in a managed tenant is a blanket tenant
    policy (many orgs disable self-service consent for any third-party app), not something these
    specific four permissions individually require.

## [0.7.2] — A download-failure log call itself crashed and took the whole mount down

### Fixed
- **A real crash, caught live on the running mount:** a genuine Graph download failure (a plain
  network read timeout) should have been caught cleanly by `fuse/operations.py`'s
  `_ensure_cached()` and turned into an ordinary `FUSEError(EIO)` for that one file - instead, the
  `except Exception:` handler's own `logger.exception(...)` call raised a fresh `NameError: name
  'logger' is not defined` from inside the handler itself. That new, unhandled exception propagated
  straight through pyfuse3's session loop and killed the entire FUSE session - Dolphin then showed
  "Authorization required to enter this folder" on `~/OneDrive` and every access failed with
  `Transport endpoint is not connected` until the mount was manually recovered and the service
  restarted.
  - Root cause of *why* `logger` (an ordinary module-level global) became unreachable in that one
    frame isn't fully pinned down - the failure sits right at a Cython/trio boundary inside
    pyfuse3's own coroutine-resumption machinery, where module-global lookups apparently aren't
    always reliable. Reproduced live to confirm the fix regardless: a genuine forced download
    failure now returns a clean I/O error to the caller and the mount stays fully responsive
    afterward, where it previously took the whole thing down.
  - New `_log_exception_safely()` wraps `logger.exception()` in its own `try/except` so a logging
    failure can never escalate into a worse failure than whatever it was trying to report - applied
    to all three `except Exception: logger.exception(...)` sites in `fuse/operations.py`, not just
    the one confirmed crashing, since they share the identical vulnerable pattern.

## [0.7.1] — Tray popup showed "You opened X" for files the user never touched

### Fixed
- **Browsing a folder under `~/OneDrive` in Dolphin was silently downloading and logging "You
  opened <file>" for files never actually clicked.** Root-caused live (not guessed): temporarily
  logging the calling PID on every FUSE `open()` (via pyfuse3's `RequestContext.pid`) caught KIO's
  own `kmimetypefinder` helper opening extensionless/ambiguous-named files (e.g. a file literally
  named `Alletra` with no extension) to sniff their MIME type from content - something Dolphin
  triggers automatically just from a folder being visible, with zero user action. Two candidate
  culprits were checked and ruled out first: Baloo (KDE's file indexer) turned out to be disabled
  on this system despite its helper process running, and plain `ls`/`readdir` was confirmed clean.
  - `ContentCache.ensure_cached()` gained a `log_open_activity` flag - the content still downloads
    and caches normally either way (the open() call still needs to return real bytes), only the
    misleading activity-log entry is suppressed.
  - `fuse/operations.py`'s `open()` checks the caller's `/proc/<pid>/comm` against a small known-list
    of KIO MIME-probing tool names before logging, verified live both ways: `kmimetypefinder`
    triggered the same download with zero new activity-log rows, while a plain `cat` (simulating a
    genuine open) still logged normally.

## [0.7.0] — "Check for Updates" in the About tab

### Added
- **"Check for Updates" button on the About tab**, with an "Update Now" button that appears when
  one's available. New `onedrive/update_check.py`: deliberately git-based rather than polling
  GitHub's REST API for releases - this project doesn't tag separate releases (every commit on
  `main` is already what a user would want), and reusing the same remote/credentials the user's own
  `git clone` already has configured means it works the same way whether the repo is public or
  private, with no separate API token/auth story to build. `check_for_update()` refuses to proceed
  if the checkout has local modifications (this checkout is never meant to carry local commits, so
  that's a real problem to flag rather than silently work around); `apply_update()` does
  `git pull --ff-only` then reinstalls `requirements.txt` (mirrors `install.sh`'s own dependency
  step, safe to re-run - pip no-ops on anything already satisfied) in case the new version added a
  dependency.
- **Clean in-place restart** (`update_check.restart_app()`) after an update, once the user confirms:
  runs the exact same shutdown path `_quit_app()`/Quit already uses (unmount, stop background
  workers), then `os.execv()`s into a fresh process. Chosen over spawning a detached child process
  because it works identically regardless of how the app was launched (systemd unit, desktop
  autostart, plain terminal) - nothing needs to track a new PID - and it sidesteps any race with
  `single_instance`'s flock entirely: Python opens files close-on-exec by default (PEP 446), so the
  lock's underlying fd is released by the same `execv` syscall that hands control to the new process
  image. Confirmed directly with a standalone test program (flock, execv into a checker process,
  confirm the checker can re-acquire the same lock) rather than assumed - a wrong assumption here
  would mean the app can never restart itself, deadlocked against its own just-released lock.

## [0.6.1] — App identity renamed OneDrive (from onedrive-native), public-release text scrub

### Changed
- **`APP_NAME` renamed from `"onedrive-native"` to `"OneDrive"`** - now used consistently for
  `~/.config/OneDrive`, `~/.cache/OneDrive`, `~/.local/share/OneDrive`, the lock file, the keyring
  service name, the FUSE `fsname`, and the Dolphin plugins' overlay socket name
  (`OneDrive-overlay.sock`). Capitalized deliberately, not lowercase `onedrive` - Linux paths are
  case-sensitive, so this still avoids colliding with any other separately-installed `onedrive`
  CLI client's own `~/.config/onedrive/`, while dropping the old "-native" disambiguator now that the project
  has its own distinct capitalization to lean on instead. Both Dolphin plugins
  (`packaging/dolphin-overlay`, `packaging/dolphin-pin-action`) needed their hardcoded socket
  filename updated and reinstalling for this to take effect - a plain source change alone doesn't
  reach an already-installed `.so`.
- Clone-directory suggestion in `README.md` shortened from `onedrive-linux-client` to `onedrive` -
  no collision risk here (it's just a local checkout directory, unrelated to the config-dir
  collision above).
- Confirmed `install.sh` already fully automates every step short of Azure app registration and
  the app's own device-code sign-in (neither of which can be scripted) - venv creation, dependency
  install, autostart `.desktop` file with correctly resolved `Exec`/`Path`, and an Applications
  menu entry, verified via a genuinely fresh `git clone` + `./install.sh` run.
- Scrubbed remaining Turkish-language text and real personal filenames from code comments and
  `CHANGELOG.md` (quoted user feedback, and two real example filenames from testing) now that this
  project's history is public-facing - kept the same technical meaning, translated to English,
  genericized the file examples.

## [0.6.0] — No app identity shipped in source: bring-your-own Azure app registration

Prompted by the decision to open-source this project publicly. Previously every install signed in
through one shared Azure app registration (the maintainer's own, created via
`register_azure_app.sh`) - fine for a personal tool, but wrong for a project meant for strangers
across unrelated organizations: it made one person's Azure tenant a single point of failure for
everyone, and the consent screen showed that one person's identity rather than the software's own.

### Changed
- **`CLIENT_ID` is no longer hardcoded anywhere in the source.** `constants.py` now resolves it at
  startup from either the `ONEDRIVE_NATIVE_CLIENT_ID` environment variable or
  `~/.config/onedrive-native/client_id` (respecting `$XDG_CONFIG_HOME`) - `None` if neither is set.
  `auth.py`'s `AuthManager._ensure_app()` checks for this explicitly and raises a clear "No Azure
  app Client ID configured. Run ./register_azure_app.sh first..." error instead of letting MSAL
  fail confusingly on an empty client ID.
- **`register_azure_app.sh` reframed from a maintainer-only, run-once script into a required
  first-time setup step for every install.** Same `az ad app create` flow as before (multi-tenant,
  public client, so any Microsoft account can sign in afterward regardless of which tenant
  registered the app), but now writes the resulting Client ID directly to the config file above
  instead of telling the user to hand-edit `constants.py` - no source changes or rebuild needed.
  Its default `--web-home-page-url` also changed from the previous maintainer's personal site to
  this project's own GitHub URL, since it's now a default used by every installer's own app
  registration, not a single shared one.
- `README.md` gained a new required "Setup: register your own Azure app" section between
  Installation and Usage; `install.sh` now prints a reminder to run `register_azure_app.sh` first
  if no Client ID is configured yet (non-blocking - install still completes either way).
- Scrubbed a handful of comments/CHANGELOG entries that named this project's own original
  organization/tenant as a live example (e.g. a real company name in the tenant-display-name
  screenshots' description) - replaced with a generic placeholder ("Contoso Ltd") now that this
  history is public-facing.

## [0.5.1] — Mount writes counted in the tray's "N items left", dead conflict_policy removed

### Added
- The tray popup's "Syncing... N items left" counter now includes the on-demand mount's own
  offline-write queue (`pending_mount_ops`, drained by `MountSyncWorker`), not just Folder Pairs'
  progress. Previously invisible entirely - the same gap PinWorker had before it got its own line in
  this same status row. New `db.count_pending_mount_ops()`; `ActivityPopup` gained a `drive_id_getter`
  constructor param to query it.

### Removed
- `folder_pairs.conflict_policy` dropped from the `Pair` dataclass and `create_pair()`'s signature -
  it was written with a hardcoded `'keep_both'` default and read back, but nothing anywhere ever
  branched on it; no UI exposed a way to set it to anything else. Kept as a misleading dead field, it
  suggested `prefer_local`/`prefer_remote` were real, selectable options when only keep-both conflict
  resolution is actually implemented. The DB column itself is left alone (SQLite has no cheap DROP
  COLUMN, and its own schema-level default already keeps populating it) - just no longer surfaced in
  Python, matching this project's existing precedent for `pair_files`' similarly-inert
  `exclude_patterns`.

## [0.5.0] — Hash-verified Folder Pairs bootstrap

### Fixed
- **Folder Pairs' first-ever sync pass could silently miss a genuine content conflict.** When pairing
  an existing local folder against an existing remote folder, any file present on both sides with no
  synced baseline yet used to be trusted as "already synced" based on byte size alone - two
  *different* files that happened to share the same size would never have their content difference
  noticed, and neither version would ever get uploaded/downloaded to match the other. Hardened:
  when both sides report a `quickXorHash`, that's now cross-checked too - same size but a different
  hash is correctly classified as a genuine conflict (auto-resolved via the existing keep-both dance,
  same as any other conflict) instead of silently treated as already in sync.
  - New `onedrive/quickxorhash.py`: a pure-Python implementation of Microsoft's QuickXorHash (the
    algorithm behind Graph's `file.hashes.quickXorHash`). Deliberately hand-rolled instead of
    depending on the `quickxorhash` PyPI package, which is a C extension with no published wheels -
    every install would need a working compiler toolchain, a real risk for less technical users of
    the .deb. Verified byte-for-byte against real Graph-reported hashes for a size-spread of actual
    cached files in this account (`scripts/verify_quickxorhash.py`) before being trusted anywhere.
    Measured at ~12 MB/s; new `PAIR_BOOTSTRAP_HASH_MAX_BYTES` (25 MB) caps candidates so one huge file
    can't stall an otherwise-quick bootstrap pass - larger files fall back to the original
    same-size-only trust, same documented risk as before, just narrower now.
  - `pair_worker.py`'s new `_attach_bootstrap_hashes()` only computes this for the (usually small) set
    of files that could actually hit the heuristic - present on both sides, same size, no baseline yet
    - not every local file.
  - New unit tests in `tests/test_reconcile.py` for the hardened heuristic (same-hash trusted,
    different-hash now a conflict, hash-unavailable falls back to size-only), plus a new
    `scripts/verify_pair_bootstrap_hash.py` exercising the real `PairSyncWorker` path end to end
    against the live account - confirmed a same-size/different-content pair now correctly produces a
    conflict with both versions preserved, where the old code would have missed it entirely.

## [0.4.99] — Mount conflict resolution dialog

### Added
- **"N sync conflicts to review" link** on the Account tab (same style/placement as the existing
  "N sync problems" link), opening a review dialog for conflicts raised by the on-demand mount's
  offline write path. Previously these only ever showed up as a "conflict" line in the Recent
  Activity popup - unlike Folder Pairs, which already had a per-pair "View N Conflicts…" menu entry
  with Keep Local / Keep Server / Dismiss actions. Hidden entirely when there's nothing to review.
- `sync/conflict_actions.py`: new `resolve_mount_conflict()`, the mount-side counterpart to the
  existing `resolve_pair_conflict()`. Same three decisions and same "both versions are already
  preserved" premise, but reads/writes the whole-account `items` cache + `content_cache` directly
  instead of a pair's `pair_files` table + real local filesystem paths - the mount has no directory
  tree of its own, FUSE serves everything from those two places.
- `gui/conflicts_dialog.py`'s `ConflictsDialog` generalized to take a `source`/`title`/`resolve_fn`
  instead of being hard-wired to one Folder Pair, so it's now shared by both the Backup tab's
  per-pair conflict review and the new Account tab one.

### Fixed
- Found live while writing the verification script for the above: a mount conflict's "conflicted
  copy" file was never actually cached locally after being created (`mount_sync_worker.py`'s
  `_resolve_write_conflict` uploads it straight from the *original* item's cache slot - the new
  item's own slot stays empty, `content_state='none'`, until something opens/reads it). Resolving
  such a conflict as "keep local" before ever opening the copy in Dolphin would have hit a missing
  local file. `resolve_mount_conflict()` now downloads the conflict copy's content on demand if it
  isn't already cached, the same lazy-fetch pattern `ContentCache.ensure_cached()` already uses
  everywhere else.
- New `scripts/verify_mount_conflict_resolution.py`, run against the real account: reproduces two
  genuine both-sides-changed mount conflicts, resolves one as keep_local and the other as
  keep_server, and asserts the DB row, the local cache bytes, AND the actual remote item all end up
  correct for each.

## [0.4.97] — Share moved into the "OneDrive" submenu; Purpose/QML plugin retired

### Changed
- **"Share" now lives inside the "OneDrive" submenu** alongside Copy link/Manage access/pin
  actions, instead of only appearing in Dolphin's separate generic "Share" submenu. Since Purpose
  plugins can only ever place themselves in Dolphin's own "Share" menu - not an arbitrary other
  plugin's submenu - the recipient-picker dialog itself moved from a KDE Purpose plugin (0.4.95's
  QML config dialog) to a native PyQt6 dialog (new `onedrive/gui/share_dialog.py`), triggered the
  exact same fire-and-forget way as Manage Access: a new `OPENSHARE <path>` overlay-server request
  + `WorkerSignals.share_requested` cross-thread signal.
  - This also simplifies things structurally: `search_people()`/`invite()` are now called directly
    in-process from the dialog (same `GraphClient` this app already holds) instead of round-tripping
    through the overlay socket to a separate C++/QML plugin - removing an entire subsystem (a
    second Purpose plugin, its own QML module with a `qmldir`, and the i18n/chicken-and-egg-loading
    issues that subsystem needed working around). `packaging/onedrive-purpose-share/` is deleted;
    the now-unused `PEOPLESEARCH`/`INVITE` overlay-server requests are removed too (nothing calls
    them anymore).
- Menu labels for the three per-item actions now match the OneDrive/SharePoint web UI's own wording
  exactly - **"Share…"**, **"Copy link"**, **"Manage access"** - rather than repeating "OneDrive" in
  each one, since the parent submenu they're all under already provides that context.

## [0.4.96] — "Manage OneDrive Access" and "Copy OneDrive Link" restored

### Added
- **"Manage OneDrive Access…"** in Dolphin's right-click menu (top-level, alongside pin actions) -
  the missing counterpart to 0.4.95's Share dialog and 0.4.94's Copy Link: lists everyone (and
  every sharing link) currently granted access to a file or folder, with a "Remove" button per
  entry to revoke it. Mirrors the OneDrive/SharePoint web UI's own "Manage access" panel.
  - `GraphClient.list_permissions(drive_id, item_id)` - `GET .../permissions`.
  - `GraphClient.delete_permission(drive_id, item_id, permission_id)` - `DELETE
    .../permissions/{id}`. Neither needs a new scope, `Files.ReadWrite` already covers both.
  - New `onedrive/gui/manage_access_dialog.py` (`ManageAccessDialog`), styled directly after the
    existing `SyncProblemsDialog` (list widget of per-row custom widgets, each with its own action
    button). Inherited permissions (`inheritedFrom` set) show as non-removable, matching Graph's
    own rule that only non-inherited permissions can be deleted.
  - New cross-thread wiring: `OverlayServer` gains a `MANAGEACCESS <path>` request and an
    `on_manage_access` callback, following the exact same pattern already established for
    `auth_required`/`_prompt_reconsent` - `WorkerSignals` gains `manage_access_requested =
    pyqtSignal(str, str)`, and `OverlayServer` is now constructed *after* `WorkerSignals` (reordered
    in `main_window.py.__init__`) so its bound `.emit` can be passed straight in as the callback,
    the same way every sync worker already receives `on_auth_required`. Necessary because opening a
    `QDialog` has to happen on the GUI thread, not the background thread a Dolphin request arrives
    on.
- **"Copy OneDrive Link"** is back (it had been folded into the 0.4.95 Share dialog's flow and
  briefly dropped) - same simple anyone-with-the-link `createLink` behavior from 0.4.94, unchanged,
  for when a full named-recipient invite is more than what's needed.
- All three pin-action-plugin entries (pin/unpin, Copy Link, Manage Access) now live under a single
  **"OneDrive" submenu** instead of appearing loose at the top level, requested directly after
  0.4.94/0.4.96 started accumulating separate top-level items. `KAbstractFileItemActionPlugin` has
  no notion of a plugin-named submenu itself - confirmed directly from KIO's own
  `kfileitemactions.cpp` that its one submenu-related metadata flag (`X-KDE-Show-In-Submenu`) only
  ever routes into a single shared, generic "Actions" catch-all, not a per-plugin one - so this uses
  the plain standard Qt technique instead: one `QAction` titled "OneDrive" with `QAction::setMenu()`
  pointing at a `QMenu` holding the real entries. Dolphin/KFileItemActions still only ever sees one
  `QAction` from the plugin either way, so the additive-combination safety property this plugin type
  was chosen for in the first place is unaffected.

## [0.4.95] — Real "Share" dialog: pick people, pick a permission level

### Added
- **"Share via OneDrive" in Dolphin's Share submenu**, replacing the plain "Copy OneDrive Link"
  action from 0.4.94 - now opens a real dialog to search the org directory live, pick one or more
  specific people, choose "Can view" vs "Can edit", and optionally attach a message, matching what
  the OneDrive/SharePoint web UI's own Share dialog does. Sends a real named-recipient invitation
  (with an email notification) via Graph's `driveItem: invite`, rather than a link anyone with it
  could use.
  - Root cause chase for *why the action wasn't appearing in the Share submenu at all* (this took
    most of the investigation): Dolphin's Share submenu only lists Purpose plugins whose
    `X-Purpose-PluginTypes` includes `"Export"` - our plugin only declared `"ShareUrl"`. Confirmed
    directly via a standalone program against `Purpose::AlternativesModel` (bypassing Dolphin/
    screenshots entirely) that adding `"Export"` alongside `"ShareUrl"` fixes it, and that our
    plugin correctly drops out for mimetypes it shouldn't match (verified against a PDF, same as
    the built-in Pastebin plugin's own `text/plain`-only constraint).
  - `GraphClient.search_people(query)` - fuzzy org-directory search via `/me/people?$search=`,
    used for the live recipient picker. Required adding `People.Read` to `SCOPES`, which forces a
    one-time re-consent on next sign-in for existing users (unavoidable - it's a new permission).
  - `GraphClient.invite(drive_id, item_id, emails, role, message)` - `POST .../invite` with
    `sendInvitation: true`, `requireSignIn: true`. Needs no new Graph scope beyond
    `Files.ReadWrite`, already granted.
  - `OverlayServer` gains `PEOPLESEARCH <query>` (returns a JSON array, never errors outward -
    a lookup blip shouldn't surface as a dialog error while the user is still typing) and
    `INVITE <json>` (resolves the path via the same mount-or-Folder-Pair logic `SHARELINK`
    already used, then calls `invite`).
  - New `packaging/onedrive-purpose-share/quick/peoplesearchmodel.{h,cpp}` - a
    `QAbstractListModel` exposed to QML as `PeopleSearchModel`, debounced (350ms) async search
    over the same Unix socket the rest of the Dolphin integration uses.
  - New `onedrivenativeshare_config.qml` - Purpose's own config-dialog mechanism (a
    `<pluginId>_config.qml`, shown by `JobView.qml` before the job runs, whose properties -
    `recipients`/`role`/`message`, declared in `X-Purpose-Configuration` - get synced back into
    the job's `data()` automatically). Verified this exact property-name-matching mechanism by
    reading Purpose's own `JobView.qml` source before writing the QML, rather than guessing.
  - Two real bugs found and fixed while getting the dialog to actually render (both diagnosed with
    a standalone `QQmlComponent`-loading test program instead of guessing from screenshots):
    1. Every `i18n(...)` call in the QML was an "unqualified access" - KDE's `i18n()` needs an
       explicit `KI18nContext`, it isn't a bare global like in some other KDE QML contexts. Switched
       to QML's own built-in `qsTr()`, caught via `qmllint` before ever touching Dolphin again.
    2. `PeopleSearchModel` was originally registered from *inside* the `kf6/purpose` plugin
       (`onedriveshareplugin.cpp`, via `Q_COREAPP_STARTUP_FUNCTION`) - which Purpose only dlopens
       when the user clicks Send, by which point the config QML (needing the type *while the
       dialog is still being built*) has already failed to import it. Fixed by moving
       `PeopleSearchModel` into a genuine, separately-loadable QML import module
       (`quick/onedrivequickplugin.{h,cpp}` + `quick/qmldir`, installed under Qt's own QML import
       path) - the exact same structure KDE Purpose's own reviewboard/phabricator plugins use for
       their "quick" models. Verified end-to-end via `QML_IMPORT_PATH` pointed at a directory
       mirroring the real install layout, with zero manual pre-loading tricks, before touching
       Dolphin.
  - Verified the full path end-to-end via direct socket calls before any GUI testing: `PEOPLESEARCH`
    against the real org directory, and `INVITE` actually granting access (confirmed via a raw
    Graph call showing the resulting `permission` object) - caught and diagnosed one real error
    along the way (Graph correctly rejects inviting yourself; not a bug).

## [0.4.94] — "Copy OneDrive Link" in Dolphin's right-click menu

### Added
- **"Copy OneDrive Link" in Dolphin's right-click menu**, for a single selected file or folder -
  third item on the "feels like a complete native app" list. Generates (or reuses an existing
  identical) sharing link via Graph's `createLink` and copies the URL straight to the clipboard,
  no dialog needed.
  - `GraphClient.create_share_link(drive_id, item_id)` - `POST .../createLink` with
    `{"type": "view", "scope": "organization"}`. Deliberately view-only and scoped to
    `"organization"` rather than `"anonymous"`: this app targets work/school tenants (device-code
    sign-in against a tenant), and such tenants commonly disable anonymous/public link sharing
    entirely via policy, which would make an anonymous-scoped request fail outright.
  - `OverlayServer` gains a `SHARELINK <path>` request (`LINK <url>` / `NONE` / `ERROR <message>`)
    and a `graph_client` constructor parameter. Unlike `PINSTATE`/`SETPIN`, this resolves paths
    under **either** the on-demand mount **or** any local Folder Pair root (via the pair's
    `remote_path` prefix) - sharing a file from a paired `~/Documents`/`~/Pictures` is just as
    common as sharing something browsed through `~/OneDrive` itself. Applies to files and folders
    alike, unlike pinning which is folder-only.
  - Reused `packaging/dolphin-pin-action/` (added a third `QAction`) rather than a new plugin
    directory - same proven socket boilerplate, and it's already the natural home for
    OneDrive-specific context-menu actions. Menu visibility uses the existing local-only `STATUS`
    check (no Graph call) so opening a context menu never itself triggers a Graph request - only
    an actual click on "Copy OneDrive Link" does. Single-selection only in v1, matching how most
    cloud clients only offer "Copy link" for one item at a time (the clipboard holds one value).
  - Verified live via a direct socket test against the running service: a file under the mount, a
    file under a Folder Pair (`~/Documents`), an untracked path (`~/.bashrc`), and a nonexistent
    path all resolved correctly (`LINK ...`, `LINK ...`, `NONE`, `NONE` respectively). Sharing the
    mount's own top-level root folder returns a Graph 400 - an edge case surfaced as `ERROR ...`
    and silently ignored client-side (no clipboard write, no crash); not a realistic use case worth
    chasing further for v1.

## [0.4.93] — Fixed: unpinning didn't stop an in-progress download pass

### Fixed
- **`PinWorker` kept downloading a folder's entire subtree even after it was unpinned mid-pass** -
  the bug flagged (but not yet fixed) in `[0.4.92]`'s own entry, now fixed and verified live. Root
  cause: `_pass_once()` reads `get_pinned_folders()` once at the start of a pass and
  `_download_subtree()` only ever checked the worker's own shutdown flag while walking a folder's
  children, never the folder's live pin state - so a folder unpinned seconds after its pass began
  (trivially easy now that pinning is one right-click away) kept downloading regardless, all the
  way to the end.
  - `_download_subtree()` now re-checks the ROOT folder's live `is_pinned` flag (via
    `db.get_item_by_id`, one cheap indexed lookup) before every single child, not just the
    worker's own stop flag - unpinning now actually interrupts an in-progress pass almost
    immediately instead of only being respected on the next scheduled run.
  - Verified against the exact same repro from `[0.4.92]`'s report: pin a large folder
    (`/Reports`, multi-GB of photos), wait ~500ms, unpin it. Before this fix: the entire subtree
    downloaded regardless, unbounded. After: exactly 1 file (the one already in flight at the
    moment of the check) completed, then downloading stopped completely - confirmed via a 5s
    quiet-log follow-up showing zero further `Downloading item` lines.

## [0.4.92] — Pin/unpin from Dolphin's own right-click menu

### Added
- **"Always keep on this device" / "Free up space (OneDrive)" in Dolphin's right-click menu**, for
  folders under `~/OneDrive` - second item on the "feels like a complete native app" list, and
  unlike the reverted thumbnailer attempt (`[0.4.91]`), verified safe against KDE's own `kio`
  source *before* building anything this time: `KFileItemActions` (`src/widgets/kfileitemactions.cpp`)
  calls `actions()` on every enabled, mimetype-matching plugin and additively merges all their
  results into the menu - there is no "only one plugin wins" resolution for this plugin type the
  way there was for thumbnail creators, so this can't silently suppress or conflict with anything
  else.
  - `dolphin_overlay_server.py`'s socket protocol gained `PINSTATE <path>` (→ `PINNED`/`UNPINNED`/
    `NONE`) and `SETPIN <0|1> <path>` (→ `OK`/`ERROR`), both scoped to folders this app tracks
    (`NONE` for anything outside the mount or a plain file - pinning keeps the same folder-only
    scope the existing "Choose folders" tree already has).
  - New `packaging/dolphin-pin-action/` - a `KAbstractFileItemActionPlugin`, compiled and verified
    against a real folder via the socket directly (pin → confirmed `PINNED` → unpin → confirmed
    `UNPINNED`, and a plain file / outside-mount path both correctly return `NONE`) before handing
    off the C++ side for the user to build/install.
  - **Found a real, separate, pre-existing bug while testing**: `PinWorker._pass_once()` reads
    `get_pinned_folders()` once at the start of a pass and never re-checks live pin state while
    walking a folder's subtree - unpinning a folder *after* its download pass has already started
    does not stop it. Confirmed live: pinning a large folder for under a second, then unpinning it,
    still triggered its entire multi-GB photo subtree to download; only killing the app process
    actually stopped it (already-downloaded bytes aren't deleted, matching this app's documented
    "unpinning never deletes cached content" policy, but the in-flight pass itself doesn't respect
    a fast unpin). Not fixed here - flagged for a follow-up, since a context-menu action makes
    quick pin/unpin cycles more likely than the original checkbox tree did.

## [0.4.91] — Reverted: real OneDrive thumbnails in Dolphin

### Removed
- **The 0.4.89/0.4.90 Dolphin thumbnailer plugin is fully removed** - the 0.4.90 fix (GENERIC
  response, never `fail()` for an in-mount path) turned out to only be half the problem. Read
  KDE's own `kio` source (`src/gui/previewjob.cpp`) directly to find the real root cause: when
  more than one enabled thumbnail plugin claims the same mimetype, KIO does not pick by priority
  at all -
  ```cpp
  if (pluginIsEnabled && !setupData.pluginByMimeTable.contains(mimeType)) {
      setupData.pluginByMimeTable.insert(mimeType, plugin);
  }
  ```
  - whichever plugin `KPluginMetaData::findPlugins("kf6/thumbcreator")` happens to enumerate
  *first* silently wins that mimetype forever, and every other enabled plugin registered for it
  (including ours) is never consulted at all. Confirmed live: `onedrivenativethumbnailer` sorts
  alphabetically after the built-in `imagethumbnail`/`jpegthumbnail`/PDF plugins, so for exactly
  the file types this was built for (images, PDFs, Office documents) the built-in thumbnailer -
  which has no idea this is an on-demand mount - won every time, opened the file with ordinary
  POSIX I/O, and silently downloaded it in full. Reported live and confirmed directly:
  "nesnelere tiklamadigim halde otomatik iniyorlar" (they're downloading automatically even though
  I didn't click on the objects) - every visible file in a folder view was downloading the moment
  the folder was opened, well before 0.4.90's GENERIC/fail() distinction could even matter.
  There's no supported way to force plugin ordering for this case (no priority key in the JSON
  plugin metadata format, confirmed against KDE's own bundled thumbnailers' `.json` files) - fixing
  this properly would need an upstream KIO change, not something achievable from this project's
  own plugin. Removed entirely rather than leave a plugin installed that can silently trigger full
  downloads: `packaging/dolphin-thumbnailer/`, `onedrive/thumbnail_cache.py`,
  `graph_client.py:get_thumbnail_bytes()`, the `THUMBNAIL` request type from
  `dolphin_overlay_server.py` (back to `STATUS`-only), and `constants.THUMBNAIL_CACHE_DIR`.
  `packaging/dolphin-overlay/` (the sync-status badge plugin, unaffected by any of this - it
  doesn't compete with any other plugin for anything) is untouched.

## [0.4.90] — Fixed: thumbnails were silently triggering full downloads

### Fixed
- **Enabling Dolphin's remote-storage previews caused every unthumbnailed file to download in full
  just from browsing the folder** - reported live, immediately after the 0.4.89 thumbnailer went
  in and Dolphin's own "Remote storage: Show previews for" setting was raised from its default
  "no file" ("Ben klasorlere girince icerik otomatik iniyor" - entering folders was
  auto-downloading content). Root cause: the plugin returned `KIO::ThumbnailResult::fail()`
  whenever the running app had no real Graph thumbnail for an item (not just when the path was
  outside the mount) - and `fail()` always makes KIO fall through to the next registered
  thumbnailer for that mimetype. Every other thumbnailer on the system (built-in image/PDF/Office
  ones) has no idea this is an on-demand mount; it just opens the file with ordinary POSIX I/O,
  which our own FUSE `open()` handler correctly treats as "download this," exactly as designed for
  every OTHER caller - just not for a thumbnailer that only wanted a preview.
  - `dolphin_overlay_server.py`'s `THUMBNAIL` response now distinguishes two cases the plugin must
    treat completely differently: `NONE` (path isn't under the mount at all - safe to fail()) vs.
    the new `GENERIC` (path IS under the mount, we just don't have a real thumbnail for it - must
    still be handled here, never fail()).
  - The plugin now renders a plain mimetype-icon image itself for `GENERIC` (`QMimeDatabase` +
    `QIcon::fromTheme`, the same lookup Dolphin's own no-preview fallback already uses) and passes
    that, so every request for an in-mount path terminates inside this plugin one way or another -
    it can never again reach a thumbnailer that would try to read the actual file.
  - Rebuilt and verified compiling cleanly; **needs reinstalling** (`sudo cmake --install build`
    again in `packaging/dolphin-thumbnailer/`) to replace the already-installed 0.4.89 binary.

## [0.4.89] — Real OneDrive thumbnails, without downloading files

### Added
- **Real thumbnails for cloud-only files in Dolphin, without downloading them** - first of a list
  of remaining "feels like a complete native app" gaps, worked through one at a time in order.
  Previously browsing a folder of cloud-only photos/documents either showed generic file-type
  icons or, worse, could trigger a full download of every file in view just to generate a local
  preview - defeating the entire point of the on-demand mount. New `graph_client.py:
  get_thumbnail_bytes()` fetches Graph's own server-generated thumbnail JPEG directly (the
  `/thumbnails/0/{size}/content` endpoint - never touches the file's actual content); new
  `onedrive/thumbnail_cache.py` caches it locally keyed by item id + etag (a changed file's new
  etag is simply a different cache filename, so a stale thumbnail is never served, no separate
  invalidation bookkeeping needed). `dolphin_overlay_server.py`'s existing IPC socket (already used
  by the overlay-icon plugin) gained a second request type, `THUMBNAIL <size> <path>`, resolving a
  mount path to its cached-or-freshly-fetched thumbnail file.
  - **New `packaging/dolphin-thumbnailer/`** - a native `KIO::ThumbnailCreator` plugin (same
    pattern as the existing overlay-icon plugin: loads into Dolphin's own process, talks to the
    running app over the same Unix socket) registered for common image/PDF/Office mimetypes.
    Compiles and links cleanly against KDE Frameworks 6; **not yet installed** - `sudo cmake
    --install build` needs to run interactively (this environment can't authenticate sudo).
    Registered system-wide by mimetype (KIO's thumbnail dispatch has no path-scoping mechanism) -
    a request for a path outside the OneDrive mount gets `ThumbnailResult::fail()`, which KIO
    falls through on to the next registered thumbnailer for that type, so files elsewhere on the
    system are unaffected **in principle** - this specifically needs live verification after
    install (confirm thumbnails still work normally for files outside `~/OneDrive` too, not just
    inside it), flagged clearly in the plugin's own README.

## [0.4.88] — Stuck sync operations: self-heal + "Sync Problems" visibility

### Fixed
- **Two classes of mount-sync operation retried forever with zero visibility** - the top priority
  from "urunun daha iyi hale gelebilmesi icin onerilerin var mi?" (do you have suggestions to make
  the product better), confirmed live: op 203 (a `delete`) and op 211 (a `create_file`) had been
  failing on every single `MountSyncWorker` pass, across multiple restarts, with nothing but a log
  line to show for it.
  - `_execute_delete` now treats a 404 from `delete_item` as success, not a failure - the item is
    already gone remotely (typically because an earlier attempt's DELETE actually landed
    server-side before a crash/kill kept the op from being cleared), so the op's own goal state
    ("this doesn't exist on OneDrive") was already true.
  - `_execute_create` now catches the 409 `GraphConflictError` both `create_folder` and
    `upload_file`'s new-item path can raise (`conflictBehavior: "fail"`), looks up the
    already-created item by name via `get_item_by_path`, and adopts it as this op's result instead
    of treating an already-successful create as a failure - the same retry-idempotency gap the
    project's own plan notes had flagged as unsolved.

### Added
- **New "Sync Problems" indicator and dialog** for whatever doesn't self-heal via the fixes above -
  a genuine, permanent failure (e.g. a filename OneDrive itself will always reject) still needs a
  human to know about it and decide what to do. New `db.py` methods
  `count_mount_op_errors`/`list_mount_op_errors`/`dismiss_mount_op`; a red "⚠ N sync problems -
  view" link appears in the Account tab's Sync Locations box (hidden entirely when there's nothing
  to show) opening `gui/sync_problems_dialog.py` - lists each stuck change with its error, a
  "Retry Now" button (wakes `MountSyncWorker` immediately), and a per-item "Dismiss" to give up on
  one specific change without retrying it forever.

## [0.4.87] — Removed "Copy files…"

### Removed
- **The "Copy files…" dialog added in 0.4.86 is gone** - requested directly right after shipping
  ("this is not useful. lets not have this in the client app."). `gui/copy_files_dialog.py`
  deleted, the link and its handler removed from the Account tab's Sync Locations box. The
  underlying Dolphin/Plasma 6 paste bug this was meant to work around is still real and still
  documented in README.md's Troubleshooting section (terminal `cp` remains the confirmed-working
  path) - only the in-app dialog was removed, not the diagnosis.

## [0.4.86] — In-app "Copy files…" to work around Dolphin's paste bug

### Added
- **New "Copy files…" dialog** (`gui/copy_files_dialog.py`), reachable from the Account tab's Sync
  Locations box next to "Choose folders" - a real fix for the Dolphin/Plasma 6 paste bug
  documented in README.md's Troubleshooting section, after being asked directly not to just point
  users at a terminal command ("kullanicilar terminal kullanarak mi bu sorunu cozecekler?" - are
  users supposed to solve this using a terminal?). Lets the user pick one or more source files
  (Qt's own `QFileDialog`, not Dolphin's) and a destination folder from a lazy-loaded tree of the
  OneDrive account (same `list_top_level_folders`/`list_children` pattern the Choose Folders
  dialog already uses), then copies them with a plain `shutil.copy2()` directly against the mount
  - on a background thread, since a still-cloud-only source file triggers a real download the
  moment it's opened for reading. This never goes through Dolphin's clipboard/CopyJob machinery at
  all, so the broken pre-flight "can I paste here" check that misjudges FUSE subfolders as
  unwritable never gets a chance to block it - verified end-to-end against the exact folder from
  the original report (`Customers/BULUTSAN/BAIS`), copying a real file in successfully with
  matching size.

## [0.4.85] — Pause-badged tray icon when sync is paused

### Added
- **New tray icon state for "sync is paused"** - requested directly ("when onedrive is paused use
  pause icon on onedrive"). Previously paused and offline both just showed the same plain gray
  cloud, distinguished only by tooltip text. New `gui/theme.py:paused_tray_icon()` draws the same
  gray cloud with two bold white pause bars over it (the universal media-player pause glyph),
  sized to stay legible even scaled down to a real system-tray render size (~16-24px), not a tiny
  corner badge that would vanish at that scale. `MainWindow._update_tray_icon()` now shows it for
  every reason sync might not be running other than being offline - manual pause, or the metered-
  connection/battery-saver auto-pause settings - while genuinely offline (not a choice the app or
  user made) keeps the plain gray cloud, unchanged.

## [0.4.84] — New "changed" badge for edits to already-synced files

### Fixed
- **Editing an existing, already-synced file showed the "uploaded" up-arrow badge in the tray
  popup's activity list** - reported directly with a screenshot: "bu yeni bir dosya degil ...
  Bu dosyada degisiklik yaptigim icin degisiklik iconu gorunmesi lazim" (this isn't a new file...
  since I made a change to it, the change icon should show). Root cause: the badge was picked
  straight from the raw `event_type` stored in `activity_log` ("uploaded"/"downloaded"), while the
  text label right next to it was already correctly derived via `_verb_for()` (which turns both of
  those into "changed" - or "opened" for the mount's first-time cache-fill case - based on
  `pair_worker`/`mount_sync_worker`'s own event semantics: `event_type="uploaded"` there always
  means "content replaced on an already-synced item," never a brand-new file - new files log
  `event_type="created"` instead). The badge now reuses that same `verb`, so the icon always
  matches the text: "You changed X" gets the new **"changed"** badge (a solid blue dot, added to
  `gui/theme.py`'s `badged_pixmap()`/`_draw_badge_symbol()`), "You created X" keeps the green +,
  and the mount's "You opened X" (viewing a cloud file for the first time, not an edit) now
  correctly falls back to the plain checkmark instead of a stray download arrow.

## [0.4.83] — Desktop notification on sync conflict

### Added
- **Desktop notification when a sync conflict is resolved** - requested directly ("Conflict varsa
  kullaniciya bilgi verebilir miyiz? ... General altina bir ayar ekleyebiliriz belki" - can we
  inform the user if there's a conflict, maybe as a setting under General). Previously a conflict
  (a file changed on both this device and OneDrive at once, both versions kept) was only
  discoverable by noticing the conflict count on the Backup tab or scrolling the tray popup's
  activity list - now `PairSyncWorker`/`MountSyncWorker` both take a new `on_conflict` callback,
  fired right after the conflict is actually resolved (`db.log_activity("conflict", ...)`), wired
  through a new `WorkerSignals.conflict_detected` signal to `MainWindow._on_conflict_detected()`,
  which shows a real desktop notification via `QSystemTrayIcon.showMessage()` naming the file and
  where the conflicted copy was kept.
- **New "Notifications" section in Settings > General**: "Notify me when a sync conflict occurs"
  checkbox (default on, `notify_on_conflict` in `sync_state`), matching the reference client's own
  Settings > General "Notifications" group from earlier screenshots.

## [0.4.82] — Backup tab rename + side-by-side bandwidth

### Changed
- **"Folder Pairs (Use Backups)" tab renamed to plain "Backup"** - requested directly ("Folder
  Pairs kismini sadece Backup olarak kullanalim" - let's use the Folder Pairs part only as
  Backup). Same `PairsPanel` underneath, just a shorter, single-purpose label instead of naming
  both the general mechanism and its backup use case together.
- **Download/Upload Bandwidth placed side by side instead of stacked** - requested directly
  ("upload ve download bandwith kisimlarini alt alta degil yanyana kullanalim"). The Bandwidth
  group box in the Network tab is now two columns (`QHBoxLayout` of two `QVBoxLayout`s) instead
  of one long column - same radios/spinboxes/behavior, just laid out side by side.

## [0.4.81] — Flattened the tab strip

### Changed
- **Un-nested Settings' sub-tabs back onto the main tab strip** - reported directly right after
  the tabbed-Settings redesign shipped ("Bu tab menuleri bir birinden ayirmayalim" - don't
  separate these tab menus from each other), with the exact order and names spelled out too. The
  window now has one flat tab row: **General, Account, Folder Pairs (Use Backups), Network,
  About** - the outer "Settings" tab that used to wrap Account/General/Network/About in a nested
  QTabWidget is gone; `SettingsPanel.general_page`/`.network_page`, the Account tab, and the About
  tab are now added directly to the same top-level `QTabWidget` as Folder Pairs. "Folder Pairs"
  is relabeled "Folder Pairs (Use Backups)" - it's this app's real equivalent of the reference
  client's separate "Backup" tab (the Desktop/Documents/Pictures pairs are just three ordinary
  Folder Pairs here), so the label says so instead of adding a second, empty tab for it.

## [0.4.80] — Account tab absorbs the old "Browse" tab

### Changed
- **Merged the standalone "Browse" tab into the new "Account" tab** - requested directly ("Browse
  ile Account kismi birlesebilir gibi" - Browse and Account could merge), with a screenshot of the
  native Windows OneDrive client's own Account tab (storage quota, "Manage storage" link, "1
  location is syncing" with "Choose folders"/"Stop sync") as the reference. The Account tab now
  has two boxes: "OneDrive" (signed-in identity, cloud storage quota via the already-existing
  `_format_quota()`, a new "Manage storage" link that opens the account's real drive `webUrl` via
  `_get_drive_web_url()` - already tenant-aware, so this works correctly for work/school accounts
  too - and the existing Sign in/out button) and "Sync Locations" (the on-demand mount's path
  field/Browse.../Mount-Unmount button, unchanged from the old Browse tab, plus a new live
  "X MB/GB used on this PC" figure and a "Choose folders" link). "Choose folders" opens the pin
  tree - previously the Browse tab's main content - in its own dialog now (`self._choose_folders_dialog`,
  built once at startup, not lazily) instead of a permanent top-level tab, closer to how the
  reference client surfaces it. No sync/mount/pinning behavior changed - this was purely a
  relocation of existing widgets and controllers into a different container.

## [0.4.79] — Tabbed Settings surface

### Changed
- **Settings restructured into its own tab strip** (Account / General / Network / About) instead
  of one long scrolling column of group boxes - reported directly that the settings surface had
  gotten too long ("sanirim client arayuzunu degistirmemiz lazim cunku cok uzadi"), with the
  native Windows OneDrive client's own tabbed Settings dialog (screenshots attached) as the
  reference for doing it "section by section, just like Microsoft." `SettingsPanel` no longer
  lays itself out as one flat column - it now exposes `general_page` (Power, Tray Popup, Ignored
  Files) and `network_page` (Bandwidth, Proxy) as separate widgets, and `MainWindow` assembles
  them alongside a new "Account" tab (the sign-in/out row, moved out of its old group-box wrapper
  - the tab itself is now the section) and a new "About" tab (version/publisher/license/GitHub
  link, the same text the tray menu's About dialog already showed, now also visible inline).
  Deliberately did **not** copy the reference's "Backup" or "Office" tabs - this app has no actual
  feature behind either one, and an empty tab would just be misleading chrome rather than a real
  section.

## [0.4.78] — Syncing badge for Folder Pair files with pending changes

### Fixed
- **Folder Pair files never showed a "syncing" status, ever.** `sync_status.status_for_path()`
  (the single source of truth for both the tray popup's activity icons and the Dolphin
  overlay-icon emblems) always returned `LOCAL` for any existing path under a Folder Pair's local
  folder, regardless of whether it had just been edited and was still queued/mid-upload, or was
  being downloaded right now. Only the on-demand mount's own placeholder files ever got the
  `SYNCING` status. Fixed: a Folder Pair file's on-disk mtime/size is now compared against
  `pair_files.last_synced_mtime`/`last_synced_size` (the exact same "already synced?" check
  `sync/reconcile.py` itself uses) - a mismatch, or no `pair_files` row at all yet, means
  `SYNCING`; a match means `LOCAL`. This naturally covers both "edited, not uploaded yet" and
  "upload/download actually in flight" as the same window, since `pair_files` is only updated once
  the corresponding Graph call actually succeeds - and clears itself the moment
  `PairSyncWorker` finishes, with no new tracking state needed.

### Changed
- **Redesigned the "syncing" badge itself** - requested directly (files actively being changed
  should show a circle/loop-style icon) - from a single open arc (read as an ambiguous "C" at small
  sizes) to a proper two-arrow refresh/loop symbol, the same glyph Dropbox/Google
  Drive/Nextcloud all use for "sync in progress." Applied to both the Dolphin overlay-icon emblem
  (`dolphin_integration.py`'s `onedrive-status-syncing` SVG) and the tray popup's activity-list
  badge (`gui/theme.py`'s `badged_pixmap`), built from the same circle geometry so both look
  identical. Verified by rendering both the old and new designs side by side at 16-64px before
  picking this one - the loop reads clearly as "syncing" even at 16px, where the old arc did not.

## [0.4.77] — Bandwidth limit UI redesign

### Changed
- **"Bandwidth" section in Settings rebuilt as radio buttons** - reported directly that the previous
  single-spinbox-with-0-means-unlimited style didn't match the same reference screenshots used for
  the Proxy section. Now: "No limit" / "Limit to: [value] KB/s", independently for Download and
  Upload, each pair its own `QButtonGroup` (needed since both radio pairs share the same group box
  as a parent widget, which would otherwise make all four mutually exclusive instead of two
  independent pairs of two). Deliberately does **not** include the reference's third "Limit
  automatically" option - asked directly and confirmed the app has no real bandwidth-detection
  capability, so a fake version of that option wasn't wanted. Underlying storage
  (`upload_limit_kbps`/`download_limit_kbps` in `sync_state`, `0` = unlimited) is unchanged - only
  the UI presentation changed, so `GraphClient`'s rate limiters needed no changes.

## [0.4.76] — Proxy settings

### Added
- **New "Proxy" section in Settings** - requested directly, with screenshots of another client's own
  "Connection settings" panel as the reference: No proxy / Use system proxy / Manually specify
  proxy (host, port, optional username+password). New `onedrive/proxy_config.py` computes a
  `(proxies, trust_env)` pair from the saved settings and applies it to both network layers
  independently - `GraphClient.apply_proxy()` (its shared `requests.Session`, covering every Graph
  API call) and `AuthManager.set_proxies()` (MSAL's `PublicClientApplication`, which only accepts
  `proxies` at construction time, so this drops the cached client and lets it rebuild lazily on next
  use). Both are re-applied immediately when the setting changes, no restart needed. "Use system
  proxy" deliberately just means "don't set anything, let requests/MSAL fall back to the standard
  `HTTP_PROXY`/`HTTPS_PROXY` environment variables" rather than reading a specific desktop's own
  proxy config (GNOME's gsettings, KDE's kioslaverc, ...), which isn't portable the way this project
  needs to stay. The proxy password is stored via the OS keyring (mirroring `auth.py`'s own token
  cache storage), not in the plain `sync_state` table alongside the non-secret host/port/username.

## [0.4.75] — Confirmation before unmounting

### Added
- **Clicking "Unmount" now asks for confirmation first** - requested directly, after being
  surprised that the mountpoint went empty right after unmounting. That's expected for a FUSE
  mount (the folder is a live view; downloaded/pinned content stays cached on disk and comes right
  back on remount, but nothing is visible at all while unmounted, unlike a real synced folder) - the
  confirmation just catches an accidental click before it causes that same surprise again.

## [0.4.74] — Office-style W/X/P letter icons for Word/Excel/PowerPoint, plus another invisible-text bug

### Changed
- **Word/Excel/PowerPoint file icons redesigned as bold single-letter marks (W/X/P) on their brand
  colors** - requested directly, with a screenshot of the real Microsoft OneDrive Windows client as
  the reference. Same general visual language Office's own icons use (a letter mark identifying the
  app, not a content pictogram) applied to this app's existing hand-drawn page shape - not a copy of
  Microsoft's actual trademarked logo glyphs.

### Fixed
- **The new letter marks weren't rendering - and neither was the existing "PDF" text label, caught
  in the same pass.** Same bug class as 0.4.72's badge symbols: `_page_pixmap()` sets
  `Qt.PenStyle.NoPen` to draw the page shape's fill without an outline, and that style stuck around
  for every later `drawText()` call in a caller (`pdf_pixmap`, and now the letter-mark icons) -
  `drawText` draws with the pen, not the brush, so the text was silently invisible regardless of
  color. Fixed at the shared source this time (`_page_pixmap` itself sets a real pen before
  returning) rather than patching each caller separately. Verified by rendering all 9 page-based
  icons to PNGs and inspecting them directly, including the ones that weren't touched by this
  change, to confirm nothing else regressed.

## [0.4.73] — New badge style: no background circle, just a bold symbol with a white halo

### Changed
- **Replaced the filled-circle badge look entirely** - shown 5 side-by-side style options directly
  (filled circle, outline circle, rounded square, smaller/subtle, no background) after the
  original was rejected (didn't like that one); this one was picked. Bold colored symbol (+/X/↑/↓/
  check/arc/cloud), no background shape at all, with a wide white "halo" stroke drawn behind it
  for contrast against whatever's underneath - `_draw_badge_symbol()` now draws each symbol twice
  (halo pass, then color pass on top) rather than once against a filled circle. The cloud icon
  needed its own halo approach (a scaled-up white silhouette behind the normal-size colored one)
  since stroking its fine detail with the same thick pen used for simple line symbols just blobbed
  it into a solid shape. Verified by rendering all 7 badges to PNGs and inspecting them directly
  before shipping, same as 0.4.72.

## [0.4.72] — Every badge symbol was silently invisible - real bug, not a sizing issue

### Fixed
- **Every activity-list badge symbol - the original checkmark included, not just 0.4.70/0.4.71's
  new ones - was invisible, just a plain solid-colored dot** (reported directly: it should be
  green with a visible + like the reference picture, not just a plain green dot). Confirmed by
  rendering each badge to a PNG
  and looking at it directly rather than guessing again. Root cause: `painter.pen()` was called
  right after drawing the badge's background circle with `Qt.PenStyle.NoPen` (fill only, no
  outline) - mutating that returned pen's color/width doesn't change its STYLE, which stays
  `NoPen`, so every `drawLine`/`drawArc` after it was silently a no-op regardless of color. Fixed
  by constructing a fresh `QPen` instead of mutating the inherited one. Also made the "+" bolder
  and larger (width 2→3, reach 0.22→0.32) once it was actually visible to compare against the
  others - a symmetric plus at the same weight as the checkmark/X read as a blurry blob rather
  than a clear symbol.

## [0.4.71] — Distinct badge symbols for created/uploaded/downloaded too, not just deleted

### Added
- **Created (+), uploaded (↑), and downloaded (↓) activity rows now get their own distinct badge
  symbol, matching 0.4.70's deleted (X)** - requested directly (asking why a downward arrow
  couldn't show for a download or an upward arrow for an upload), same bottom-right corner position on
  every icon. `badged_pixmap()` reworked from a pile of individual boolean flags
  (in_progress/cloud_only/deleted) into a single `badge: str` parameter ("check"/"syncing"/"cloud"/
  "deleted"/"created"/"uploaded"/"downloaded") - clearer with seven distinct kinds than continuing
  to add more booleans. `cloud_only` (a file still not downloaded to this device right now) keeps
  priority over the event-type badge when both could apply, since it reflects current state rather
  than a specific past event.

## [0.4.70] — Red X badge for deleted items in the activity list

### Added
- **Deleted files/folders in the activity list now get a small red X badge instead of the green
  check/blue cloud dot** - requested directly, with a screenshot of another client's own activity
  feed as the reference. `badged_pixmap()` gains a `deleted` parameter (red circle, two crossing
  lines) alongside its existing in_progress/cloud_only badges; wired through `_ActivityRow` from
  the same `is_deleted` flag `_render()` already computed for other purposes (resolving the click
  target, suppressing the cloud-only check).

## [0.4.69] — Header itself was taller than its content needed, not the gap between the lines

### Changed
- **"Too much space between the display name and the tenant/company name line" reported a fourth time despite 0.4.66's
  fix measuring a ~0px gap between the two lines themselves** - the gap really was fixed; the
  remaining space was the header frame itself being taller (64px) than its content actually needs.
  Recomputed from the actual constraint: the avatar (36px) plus the header's own 8px top/bottom
  margins is 52px, the real minimum - not a value picked to leave slack for a two-line stack that
  turned out to only need ~31px. Header shrunk 64px → 52px, and the account name button's own
  height tightened 20px → 18px (exactly its font's natural line height, removing the last bit of
  slack there too). Verified via the same isolated geometry measurement as 0.4.66 - avatar and text
  both still fit without clipping at the new height.

## [0.4.68] — Removed the account name button's hover highlight

### Changed
- **Removed the hover-background highlight on the "Hasan Altin ▾" account name button** - reported
  directly as not looking good. Likely made noticeable by 0.4.66's header fix: that button now
  stretches to the identity column's full width (fixing the header-spacing bug), so the highlight
  spanned the whole row on hover instead of just hugging the name text.

## [0.4.67] — "Show sync status bar" hides the whole row, not just the button

### Changed
- **The "Show 'Sync now' button" setting only ever hid the button itself, leaving the status
  checkmark and text still visible** - reported directly, with a screenshot circling the entire
  row: turning it off didn't make that marked section disappear the way the name implied. Now
  hides the whole status row (checkmark, "All synced!"/progress text, and the button together).
  Renamed to "Show sync status bar" to actually describe that, with a tooltip spelling out what the
  row contains. Backing sync_state key renamed `popup_show_sync_button` → `popup_show_status_row`
  to match (no migration needed - the existing value was already "1", same as the new default).

## [0.4.66] — Pin-download ETA, and the actual root cause of the header gap

### Added
- **Pin-download progress ("Downloading pinned files... N remaining") now has an ETA too**
  (reported directly - "kalan dosyayi gosteriyor ama suresi yok", it shows the remaining count but
  no duration). New separate streak tracker (`_pin_streak_start`), sampled from
  `_sync_progress_summary()` itself on whatever cadence it's already called at (`refresh()`'s ~3s
  timer) - coarser sampling than pair syncing's own per-file-signal tracker, but same underlying
  rate-estimate math.

### Fixed
- **The header spacing gap survived two previous fix attempts (padding, then setFixedHeight) -
  found the actual cause this time by measuring real widget geometry in an isolated test instead of
  guessing a third time.** A 7px gap remained even with every margin/padding/height on the button
  and label explicitly zeroed; tracing it down showed the button used to sit inside its own nested
  `QHBoxLayout` (added only for an `addStretch(1)` to keep it left-aligned) - nesting that layout
  into `identity_col`'s `QVBoxLayout` via `addLayout()` reserved the extra space by itself,
  independent of any of that layout's own margin/spacing settings. Adding the button directly to
  `identity_col` (no nested layout) measured a 0px gap in the same test.

## [0.4.65] — Popup had no idea PinWorker existed, plus a real header-spacing fix

### Added
- **Popup now shows "Downloading pinned files... N remaining" when PinWorker (the on-demand
  mount's eager pre-download for pinned folders) is active** - reported directly: files were
  visibly downloading with zero indication of that anywhere in the popup. The popup's entire
  progress feature was keyed off `folder_pairs.last_sync_status`, which PinWorker never touches at
  all - it's a completely separate mechanism, invisible to that tracking by construction, not a
  regression. New `db.count_pending_pinned_downloads()` reuses `list_descendants()` (a plain
  indexed walk, not a recursive CTE - see that method's own docstring for why a CTE measured 60+
  seconds on this account). No rate-based ETA for this one, just an honest count - there's no
  per-file signal feeding a streak tracker the way pair syncing gets.

### Fixed
- **The tray popup's "too much space between the name and tenant line" gap survived an earlier
  padding-only fix** (reported directly, again). Root cause this time: a `QPushButton` reserves
  extra vertical space from the platform style itself, independent of its own padding - zeroing the
  padding wasn't enough. `setFixedHeight(20)` on the account name button (and a matching
  `setFixedHeight(14)` on the tenant label) forces both tight regardless of what the style adds.

## [0.4.64] — Popup didn't open, or opened late, under heavy background sync load

### Fixed
- **The tray popup sometimes didn't open at all, or took a noticeable moment to appear** (reported
  directly). `show_near()` called `refresh()` - several
  database queries - before `self.show()`, so the window itself didn't become visible until all of
  them finished; under a heavy active sync pass (thousands of items, the same database connection
  under real write load from `PairSyncWorker`), that could take a genuinely noticeable moment.
  Reordered: the window now shows immediately (with whatever content it already has, stale from
  last time or empty on first run), and `refresh()` runs right after via the event loop's next
  iteration instead of blocking the same call that makes the window visible - a brief flash of
  stale content beats the window not appearing at all. Repositioning is now factored into a shared
  `_anchor_pos()` helper, called once with the pre-refresh size and again after content settles, so
  the popup stays flush in its corner instead of drifting once real content changes its size.

### Fixed
- **Pausing sync (manually, or via metered/battery auto-pause) didn't actually stop a pair sync
  already in progress until it had ground through its entire action list** - reported directly,
  live, against a real backlog of thousands of items ("sync paused diyor ama sync ediyor", it says
  paused but it's still syncing - confirmed right after 0.4.62's own status-display fix made the
  discrepancy visible for the first time). `PairSyncWorker.stop()` only sets a flag that the outer
  run loop checks between pairs; the inner loop processing one pair's own action list never checked
  it at all, so a pair with thousands of queued changes just kept running regardless of `.stop()`.
  Same gap existed in `MountSyncWorker`'s drain loop over queued mount writes - fixed there too.
  Both now check the stop flag between individual actions and break early, safe because the next
  pass re-derives whatever's left over fresh (`reconcile_pair()` for pairs, the queue table itself
  for mount writes) - nothing is lost, just deferred. Also fixed a related correctness bug this
  surfaced: the code after the pair loop unconditionally marked the pair "idle" with a fresh
  `last_sync_at` even on an early stop, which would have falsely marked a genuinely-incomplete pass
  as finished and skewed the next pass's bootstrap-heuristic check - stopping early now skips both.

## [0.4.62] — Two real bugs: stale "syncing" display while paused, and low-battery pause never firing

### Fixed
- **The popup kept showing an active upload/download in progress even while sync was paused**
  (reported directly - "sync durdurulmus olsa bile upload ediyor"). Root cause:
  `pair.last_sync_status` is only ever written by `PairSyncWorker` itself - once the worker is
  stopped, whatever status string it last wrote (e.g. "Uploading Screenshots/...") just sits in the
  database unchanged, and `_current_sync_state()`/`_current_syncing_rows()` were reading that text
  with no check on whether a worker was even still running to have produced it. Both now check a
  new `sync_active_getter` (wired to `self.pair_worker is not None`) first and report nothing
  in-progress at all when sync isn't actually running; the status line also now says "Sync paused"
  explicitly instead of falling through to "All synced!".
- **The low-battery auto-pause setting (0.4.57) never actually fired** (reported directly at both
  10% and 7% remaining - "batarya az olsa bile senktron ediyor"). `is_battery_saver()` alone was
  the only check, and confirmed directly, live, that this machine never actually switches its
  power-profile to "power-saver" just from running low - the setting was watching a condition that
  never became true. New `power_status.is_battery_low()` checks the raw UPower battery percentage
  against a 20% threshold instead (UPower's own `WarningLevel` property was tried first as the more
  semantically precise source, but confirmed live to stay at `NONE` even at 7% on this machine - not
  reliable here). The existing setting now triggers on either condition; checkbox label and tooltip
  updated to describe both.

### Fixed
- **0.4.59's fix (disconnecting the status signals before `db.close()`) didn't actually close the
  race - confirmed directly, the exact same crash happened again on the very next restart that hit
  it, plus a second, different crash from the same root cause** (`pin_worker.py` calling
  `db.set_content_state()` directly, not through a signal at all, mid-download, after `db.close()`
  had already run). Disconnecting a signal doesn't retract an event already queued for delivery,
  and does nothing for a worker thread calling the database directly.
- **First replacement attempt - actually joining each worker thread before `db.close()` - was also
  wrong, caught directly by timing the restarts**: joins bounded at 10s were reliably taking
  ~5.2s and never crashing, which looked like success until checking `journalctl` showed
  `systemd[...]: app-onedrive@autostart.service: Failed with result 'timeout'` on those same
  restarts. The unit's `TimeoutStopUSec` is 5s - shorter than a single worker's realistic join
  time - so systemd was SIGKILLing the process mid-join before the race could even occur, not
  because the join finished. That's not a fix, it's a coin flip against systemd's own timeout.
- **The actual fix: stop calling `db.close()` on quit at all.** The database is already opened in
  WAL mode specifically for durability against an abrupt disconnect - the same property this app
  already relies on elsewhere for crash recovery ("reconciliation is the recovery mechanism", see
  the Folder Pairs design notes). Not explicitly closing the connection is safe here; the OS
  reclaims the file handle when the process exits right after regardless. `_stop_background_workers()`
  reverted to its original non-blocking form (`.stop()` only) - it no longer needs to guarantee
  anything before returning, since there's no longer a closed connection for a late worker
  operation to crash against.

## [0.4.60] — Revert main window header back to email

### Changed
- **0.4.58's swap of the main window header's second line from email to tenant name is reverted**
  - corrected directly ("ayarlardaki mail kalabilirdi" - the email could have stayed). That change
  was never actually asked for; the original complaint was just about the spacing between the two
  lines, which stays fixed. It also exposed a real gap in the 0.4.58 change: unlike the tray
  popup's tenant label, this one had no eliding, so a long legal tenant name (this account's is
  over 60 characters) just overflowed the header instead of looking clean - reverting to the
  shorter email sidesteps that too.

## [0.4.59] — Fixed a shutdown crash on quit while a pair sync was in flight

### Fixed
- **Quitting while a Folder Pair sync was in progress could crash with `sqlite3.ProgrammingError:
  Cannot operate on a closed database`** - caught directly, on the very restart that shipped
  0.4.58. Root cause: worker `.stop()` only sets a flag, it doesn't join the thread, so a worker
  can still be mid-iteration and emit one more queued status signal after `closeEvent` has already
  called `db.close()` - that signal is delivered later on the Qt event loop and crashes trying to
  read from the now-closed connection. Fixed by disconnecting `status_changed`/
  `pair_status_changed` before closing the database, so any such late signal is a harmless no-op
  instead of a crash.

## [0.4.58] — Folder Pairs status: icon only; main window header shows tenant name too

### Changed
- **Folder Pairs table's Status column dropped all inline text, including in-progress upload/
  download detail** (requested directly - "burada sadece senkron edildigini gosteren icon olsun",
  just an icon showing whether it's synced) - the icon's color already carries the state (green/
  blue/gray/red); the full status string is now a tooltip instead of always-visible text.
- **Main window header's second line now shows the tenant/company name (matching the tray popup)
  instead of the plain email**, falling back to the email domain until it's fetched - consistently
  referred to as "tenant" directly, so it should show the same thing the popup does.
- **Tightened the same "too much space between name and tenant" spacing in the main window's
  header** (`identity_col` spacing 2 → 0) - the same complaint, and same fix, as 0.4.57's popup
  header change.

## [0.4.57] — Battery-saver auto-pause, popup display toggles, tightened header spacing

### Added
- **New "Automatically pause sync when this device is in battery saver mode" setting** (requested
  directly, with a screenshot of another client's equivalent setting as the reference). New
  `onedrive/power_status.py::is_battery_saver()`, detected via power-profiles-daemon's
  `ActiveProfile` (standard freedesktop.org, not KDE-specific) - same fail-closed-on-error pattern
  as the existing metered-connection check, checked on the same 30s timer, wired into
  `_should_sync()`/`_apply_sync_state()`/the tray tooltip alongside it.
- **Two new "Tray Popup" settings: "Show 'Sync now' button" and "Show tenant/company name under
  account name"**, both requested directly as customizable on/off toggles rather than fixed. Both
  default on; toggling either refreshes the popup immediately if it's currently open.

### Fixed
- **Too much vertical space between the display name and the tenant-name line under it** (reported
  directly). The account-menu button's own padding (3px top/bottom) was adding to the header's
  extra height from 0.4.55 - tightened to 1px and reduced the header back from 68px to 64px.
- **A stray tooltip bubble on the tenant-name label (added in 0.4.55, showing the full un-elided
  name on hover) was left open in a screenshot and mistaken for an unwanted blank popup** - removed
  the tooltip. The elided text is still fully readable at the popup's fixed width for any tenant
  name that isn't extremely long; a hover tooltip wasn't essential and was the likely source of the
  confusion.

## [0.4.56] — Tray popup: show the actual company name, not the email domain

### Changed
- **0.4.55's tenant line under the display name used the email domain (e.g. "contoso.com") -
  corrected directly to use the real company name instead (e.g. "Contoso Ltd"), which is not
  the same thing.** First tried `/me`'s `companyName` field, on the assumption it would avoid
  needing a new consent scope - confirmed directly against the real account that it comes back
  empty (an optional Azure AD field this tenant simply doesn't populate). Switched to
  `GraphClient.get_tenant_name()`, reading `/organization`'s `displayName` instead - confirmed
  directly that this works with the scopes the app already has (`Files.ReadWrite`/`User.Read`), no
  `Organization.Read.All`/`Directory.Read.All` or new consent prompt needed. Fetched alongside the
  display name and profile photo in `_refresh_account_info_async`, cached in
  `sync_state["tenant_name"]`. Long legal entity names (this tenant's is over 60 characters) are
  exactly what the existing elide-with-"..." handling from 0.4.55 was built for.

## [0.4.55] — Tray popup: show the tenant domain under the display name

### Added
- **The signed-in account's tenant domain (the part after "@" in the email) now shows under the
  display name in the popup header** (requested directly), matching the reference layout from
  earlier (name on one line, an identifying second line underneath). Elided with "..." rather than
  wrapping or overflowing if it doesn't fit the popup's fixed width - the full domain is still
  available as a tooltip. Header grew from 60px to 68px tall to fit the extra line without
  crowding the existing name/avatar row.

## [0.4.54] — Tray popup: drop the search box and close button, reorder the account menu

### Changed
- **"View online" and "Settings" swapped order in the popup's account menu** (requested directly)
  - now View online, then Settings.
- **The unreadable gray "Ignored Files" description in Settings now uses the normal text color**
  (reported directly as unreadable) - it was styled with `palette(mid)`, a muted tone meant for
  subtle helper text, which turned out to render close to invisible against the group box's
  background on this theme.

### Removed
- **The popup's search box and header close ("✕") button**, both requested directly as unneeded.
  The search box's filtering logic (`_apply_filter`) is gone along with it - `refresh()` now renders
  the full event list directly. The close button was one of several ways to dismiss the popup
  (alongside Escape, clicking the status row, and now-working outside-click auto-close from 0.4.48)
  so removing it doesn't remove any actual capability, just a redundant control.

### Note
- Every code comment and this changelog are English-only from this point forward, per explicit
  instruction - earlier entries that quoted the original request verbatim in Turkish have been
  rewritten in English.

## [0.4.53] — Ignored Files editor: a real table with Add/Remove buttons

### Changed
- **The global "Ignored Files" editor (new in 0.4.52) was a single multi-line text box - rebuilt as
  a real table with Add/Remove/Remove all buttons instead**, matching Nextcloud's own "Ignored
  Files Editor" (a screenshot of it was supplied directly as the reference, asking for add/remove
  buttons like it has). New `onedrive/gui/ignored_files_dialog.py`. Deliberately
  skips the reference's "Allow Deletion" checkbox column - that's a Nextcloud-specific concept
  (whether a pattern-matched item may itself be deleted to unblock removing its containing folder)
  with no equivalent in this app's exclude model, where a pattern only ever means "never sync this
  at all"; an inert checkbox that did nothing would just be confusing.

## [0.4.52] — Folder Pairs: global excludes, edit an existing pair, drop redundant "idle" text

### Added
- **A new "Ignored Files" section in Settings, with one global exclude-pattern list applying to
  every Folder Pair** (requested directly, with a screenshot of Nextcloud's own single "Ignored
  Files Editor" as the reference, asking that excludes live in Settings globally rather than being
  configured separately for each pair). Replaces the old per-pair "Edit Excludes..." row action -
  `pair_worker.py` now reads `sync_state["global_exclude_patterns"]` (falling back to the same
  `DEFAULT_EXCLUDE_PATTERNS` every pair used to be seeded with) instead of each pair's own
  `exclude_patterns` column, which stays in the schema unused rather than migrated away.
- **"Edit Pair…" row action** - previously the only way to change an existing pair's local or
  remote folder was deleting it and adding a new one from scratch (reported directly as a gap - no
  way to edit an existing pair). Reuses `AddPairDialog` in a new edit mode
  (pre-filled fields, excludes the pair being edited from its own overlap check) and a new
  `db.update_pair_mapping()` that also clears the pair's `pair_files` history and `last_sync_at` so
  the next sync pass treats the new mapping as a fresh bootstrap rather than comparing against
  now-meaningless old-mapping state.

### Changed
- **Folder Pairs table no longer shows "idle" as text** (requested directly - the icon alone is
  enough) - the green checkmark icon already says that; the Status column now shows
  text only for anything more informative than idle (syncing progress, paused, an error, or a
  conflict count).

## [0.4.51] — Main window: center on open, sign-out moved to Settings, fixed invisible close button, Folder Pairs as a real table

### Changed
- **Main window now centers itself on the primary screen's available geometry every time it's
  shown** (requested directly), instead of opening wherever the WM's default placement (or a
  previous header-drag) happened to leave it.
- **"Sign in"/"Sign out" moved from the header bar into a new "Account" box at the top of the
  Settings tab** (requested directly). The header
  keeps just the identity display (avatar, name, quota); the button and a "Signed in as ..." /
  "Not signed in" status line now live in Settings instead.
- **Folder Pairs list rebuilt as a real `QTableWidget`** (Name / Status / Local Folder / Remote
  Folder / Last Synced / actions columns) instead of one gray, stacked title/subtitle/extra text
  block per row - reported directly as hard to read gray text, asking for a real side-by-side
  table instead. Local/remote paths that don't fit their column carry the full path as
  a tooltip; the "N conflicts to review" note now appends to the Status cell instead of a separate
  barely-visible line.

### Fixed
- **The main window's close button ("✕") was invisible - the button's outline showed, but no glyph
  inside it** (reported directly, with a screenshot). Root cause: it only had `setFixedWidth(28)`
  and relied on the shared `QFrame#headerBar QPushButton` stylesheet, whose `padding: 6px 14px`
  (28px of horizontal padding alone) exactly consumed the entire 28px width, leaving zero space to
  actually draw the character. Gave it its own tight stylesheet instead, matching the tray popup's
  own already-correct close button.

## [0.4.50] — Tray popup: remove drag-to-move entirely

### Removed
- **Drag-to-move on the popup header, added in 0.4.35 as a fallback for unreliable auto-placement
  and still present after 0.4.49's corner-anchor fix, was itself rejected just as directly**
  ("it's movable, I don't want that") - a native system-
  tray flyout (the Bluetooth-applet reference from 0.4.49) isn't draggable at all, and having it be
  possible read as the popup still not being properly docked. Removed the header's drag
  `eventFilter`, its right-click "Reset popup position" menu, and the `popup_pos_x`/`popup_pos_y`
  persistence entirely - `show_near()` now unconditionally computes the same bottom-right-corner
  position every time, with no saved-position override left to reintroduce drift.

## [0.4.49] — Tray popup: dock to the bottom-right corner, not the cursor

### Changed
- **The popup's opening position was centered on the exact cursor click point within the tray
  icon, which reads as "moving around" between opens rather than the fixed dock point native
  system-tray flyouts have** (reported directly, with a screenshot of Plasma's own Bluetooth
  applet flyout as the reference, asking for the same corner-docked placement). The default
  (never-dragged) placement now always anchors to the
  bottom-right corner of the available screen area - `_EDGE_MARGIN` (8px) from the right edge,
  flush with the taskbar per the existing `_TASKBAR_CLEARANCE` - instead of `pos.x()`-centered.
  Also cleared a stale dragged position left over from earlier testing this session, which would
  otherwise have silently overridden this fix and made it look like nothing changed. Manual
  drag-to-move (and its right-click "Reset popup position") is untouched - still there for any
  desktop where this corner genuinely isn't where the tray lives - it's just no longer what
  happens by default.

## [0.4.48] — Tray popup: same taskbar fix as the main window, plus a real close-on-outside-click

### Fixed
- **The tray popup had the exact same taskbar-icon problem 0.4.47 fixed for the main window, just
  never noticed/fixed for the popup itself** (reported directly, with a screenshot showing two
  distinct OneDrive icons - one wanted, one not). `Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint`
  was never enough on its own for the main window either; the popup was never given the follow-up
  `x11_hints.skip_taskbar()` call that fixed it there. Now calls it too, on first show and again on
  every `show_near()` (mirroring `MainWindow._show_window()`'s own re-assert-on-every-show pattern).
- **The popup never actually closed itself on an outside click - it only dropped behind whatever
  was clicked into, via normal Tool-window WM stacking** (reported directly: "popup just goes back
  to active windows doesnt close itself when i click somewhere else"). The close-on-focus-loss logic
  was deliberately removed entirely back in 0.4.43 to fix a different regression (Tool + the old
  Popup-era focusOutEvent/watchdog combination caused an instant self-close on open) - "drops
  behind" was left as the only outside-click behavior, which reads as "stuck" rather than "closed".
  Re-added it the way 0.4.44 already proved works for a Tool-type window - `applicationStateChanged`
  with a startup grace period (avoids the instant self-close 0.4.43 was fixing) and a debounce
  (absorbs same-app focus blips) - this time on the actual right target. Verified live in isolation
  before deploying: opens and stays open; a real focus change to another window closes it; opening
  the popup's own account/right-click menu (a same-process nested event loop) does not falsely
  close it.

## [0.4.47] — Set _NET_WM_STATE_SKIP_TASKBAR directly - the previous fix wasn't enough either

### Fixed
- **0.4.46's `Tool | FramelessWindowHint` combination - the exact one the tray popup itself uses -
  still wasn't enough for the main window. Reported directly, still showing after that fix too.**
  `xprop` on the live window kept showing `_NET_WM_WINDOW_TYPE_UTILITY` alongside a `_NORMAL`
  fallback regardless of window flags tried (Tool alone, Tool+Frameless, Dialog), and this
  desktop's Task Manager widget evidently respects that fallback no matter what. Stopped guessing
  at window-type flag combinations and went straight to the actual purpose-built mechanism
  instead: `_NET_WM_STATE_SKIP_TASKBAR`, a state independent of window type, set directly via a
  raw EWMH property/ClientMessage (new `onedrive/gui/x11_hints.py`, using `python-xlib` - a new
  dependency, standard freedesktop.org EWMH rather than anything KDE-specific, so it stays
  consistent with this project's requirement to work across desktop environments; it simply does
  nothing under native Wayland). Confirmed working via a live side-by-side test window against
  the real taskbar before wiring it into the actual app. Applied on the window's initial creation
  (the property-set form, reliable for first map) and again every time it's re-shown after being
  hidden (the ClientMessage form, needed for that case).

## [0.4.46] — Actually remove the main window's taskbar entry this time

### Fixed
- **0.4.45's `Qt.WindowType.Tool` flag alone wasn't enough - the taskbar icon was still there,
  reported directly.** Root-caused properly this time with direct `xprop` inspection instead of
  guessing again: a `Tool` window that still has native decorations gets `_NET_WM_WINDOW_TYPE` set
  to *both* `_UTILITY` and a `_NORMAL` fallback, and Plasma's Task Manager widget here respects
  that fallback and shows it anyway (also tried `Qt.WindowType.Dialog` directly - same result).
  Confirmed via 3 real side-by-side test windows, checked against the live taskbar: only
  `Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint` (the exact combination the tray popup
  already uses) actually produces a hint Plasma respects.
- The trade-off: frameless also drops the native title bar, which was the only thing providing
  close/move for this window. The existing blue header bar picks up a close button (wired to the
  same `closeEvent` hide-to-tray path as before) and drag-to-move via `eventFilter`, mirroring the
  tray popup's own already-proven header pattern exactly, so nothing is lost - just no longer
  native-drawn.

## [0.4.45] — Main window: no taskbar entry, not "hide on click-away"

### Changed
- **0.4.44 solved the wrong problem.** It made the main window auto-hide when the application lost
  focus, keeping its normal taskbar entry the whole time - technically correct and verified
  working, but not what was actually being asked for: "I dont want to see OneDrive icon on
  taskbar when I click on somewhere else. I just want to see OneDrive icon in system tray." The
  actual ask is simpler than click-away detection: never show a taskbar entry for this window at
  all, the same way the tray popup already doesn't. Replaced the entire `applicationStateChanged`/
  grace-period/debounce mechanism from 0.4.44 with a single `Qt.WindowType.Tool` flag on the
  window itself - matching how the popup's own taskbar-visibility was already solved. No focus-
  loss-triggered close/hide logic here at all (that combination is exactly what broke the popup
  once already when Tool was tried there) - the window still opens the same way (double-click the
  tray icon) and still hides to the tray instead of quitting when closed (unchanged `closeEvent`);
  it just no longer claims its own taskbar slot while doing either.

## [0.4.44] — Main window also hides on click-away, not just the tray popup

### Added
- **The main app window (Browse/Folder Pairs/Settings) now also hides itself when you click into
  a different application** - requested directly, right after the tray popup's own version of
  this was fixed. Unlike the popup, this window keeps a taskbar entry, so it can't reuse the
  `Qt.WindowType.Tool` trick - it needed its own explicit check using
  `QApplication.applicationStateChanged`, reusing the exact same "hide to tray, don't quit" path
  `closeEvent` already uses for its own X button.
- Two real failure modes were caught by testing this live, in isolation, before wiring it into
  the actual app (same discipline as the popup fix): a brief `ApplicationInactive` flickers within
  about a second of any `show()` (compositor noise, not a real focus change) - a 1s grace period
  per show ignores this. More importantly, opening one of this window's own child dialogs
  (confirmed directly with both a `QMessageBox` and a `QFileDialog`, the two dialog types this app
  actually uses) *also* flickers `ApplicationInactive` for a moment during the focus handoff, even
  though the user never left the app - hiding on that would make the window vanish while its own
  dialog is still open. A 250ms debounce defers the actual hide and cancels it if the state flips
  back to Active first (the normal case for an in-app dialog stealing focus), only following
  through if it's still inactive once the debounce elapses.

## [0.4.43] — Switch the popup to a real window - it now drops behind on click, no longer pinned on top

### Fixed
- **The popup no longer stays pinned above every other window.** Root-caused properly this time,
  not just reattempted: `Qt.WindowType.Popup` is override-redirect at the X11 level, permanently
  excluded from the window manager's normal stacking order - independent of anything this app
  does, it can never drop behind another window, and (confirmed via two separate isolated live
  tests, one with a raw `QWidget` and one with Qt's own `QMenu` - the same class Nextcloud's
  desktop client uses) it structurally cannot detect a click on a native-Wayland window either,
  since Wayland requires a popup grab to be tied to a genuine input event on its own parent
  surface, which a system-tray-icon click never delivers to any app. Read Nextcloud's own open-
  source implementation for how they actually handle this: under Wayland they don't use a
  transient popup at all, they open a plain, regular, centered window instead - confirmed directly
  in their source, in a comment that verbatim matches this app's own diagnosis: "Wayland cannot
  create an arbitrary QWidget popup from a tray activation."
- Switched to `Qt.WindowType.Tool` - a normal WM-managed window, so it naturally drops behind
  whatever's clicked into (ordinary stacking, no click-detection code needed for that part at
  all) while still skipping the taskbar/pager like Popup did. Tool was tried once before and
  reverted fast because it closed itself immediately on open - root-caused this time: that
  regression wasn't the window type, it was *keeping* the old Popup-era "close on any focus
  loss" logic (`focusOutEvent`, the `_NET_ACTIVE_WINDOW` watchdog) while switching to a window
  type that briefly gets a real, WM-driven activate-then-deactivate on show(). That entire
  subsystem is removed this time, not just swapped to a different flag - `xprop`-shelling-out
  code and all. Turns out KWin has its own native handling for `Tool`-type windows losing focus
  (hides them) that doesn't need any application-level focus tracking at all once nothing is
  fighting it.
- Verified with real, live isolated tests before touching the shipped app (matching the standard
  set after the last regression): an offscreen regression suite covering all prior click/Escape/
  idle-close behavior, then a live test with a diagnostic heartbeat and instrumented focus/show/
  hide events against the real desktop - confirmed stable for 70+ seconds with zero premature
  self-close (the exact prior failure mode), confirmed it correctly hides on both a manual close
  and a genuine outside click with no `focusOutEvent` involved at all, and confirmed it reopens
  correctly after being hidden.

## [0.4.42] — Exclude LibreOffice's own lock files from Folder Pairs

### Fixed
- **LibreOffice's `.~lock.<name>#` lock file for an open document repeated a failed upload every
  single sync pass, forever, for as long as the document stayed open** - found while auditing the
  log for other issues. The trailing `#` is an illegal OneDrive filename character, so every
  attempt 400'd. Added `.~lock.*#` to `DEFAULT_EXCLUDE_PATTERNS`, alongside the other editor-
  generated transient files already excluded there for the same reason (Kate swap files, Chrome
  PWA shortcuts). Also appended the pattern directly to the 3 already-existing live pairs' stored
  `exclude_patterns` (a per-pair DB column, so the new default alone wouldn't reach pairs created
  before this change) - confirmed the glob matches the real failing filename, and that this needs
  no restart to take effect since patterns are re-read from the DB on every sync pass.

## [0.4.41] — Auto-close the popup after a period of no interaction

### Added
- **The popup now closes itself after 60 seconds of no interaction** - a portable backstop for
  the still-unresolved click-outside-doesn't-close limitation (confirmed structural: neither
  outside-click detection nor the WM active-window watchdog can observe a click landing on a
  native-Wayland window from this XWayland-hosted popup, and a from-scratch attempt at a real
  native-Wayland `xdg_popup` - tested in complete isolation, nothing in the live app touched -
  hit a hard Wayland security-model wall: a popup grab requires a genuine, serial-bearing input
  event on its parent surface, which a system tray icon click never delivers to the app at all,
  tray icons being owned by the desktop shell and mediated over D-Bus). Unlike the last two
  attempts at this, this fix has zero window-manager dependency - it's a plain Qt timer - so it
  can't regress the way those did. Resets on any real interaction (a click inside it, a key
  press, typing in search, scrolling the list), so it won't interrupt someone actively reading
  it. Verified in isolation: fires after the idle window, resets correctly on each of those four
  interaction types, and a manual close (X button/Escape) doesn't leave a stray timer that fires
  later.

## [0.4.40] — Self-heal stale-etag mount renames instead of stalling for minutes

### Fixed
- **A mount rename op could 412 ("Graph conflict") every ~60s pass for 15+ minutes straight**,
  found while auditing the log for other issues - `_execute_rename` sent `if_match=item.etag`
  straight to Graph with no fallback, unlike `_execute_write` (which already does a pre-check
  GET+etag-compare) and `graph_client.upload_file()` (which already self-heals its own known
  conflict class). A rename's etag mismatch is far more often ordinary staleness - drifted since
  this item's cached etag was last refreshed - than a genuine conflict; unlike a content write,
  there's no content to protect with a keep-both dance, so blindly retrying with a corrected etag
  is safe. Confirmed live: the stuck rename eventually succeeded on its own only because
  `DeltaSyncWorker`'s much slower, independent poll happened to refresh the stale etag first.
  `_execute_rename` now catches the 412, fetches the item's real current etag, and retries once
  immediately - turning a multi-minute error-logging stall into one extra Graph call. A genuine,
  persistent conflict (both attempts fail) still propagates normally rather than being silently
  swallowed. Verified with mocked Graph calls: self-heals on a one-off stale etag, and a real
  persistent conflict still propagates and leaves the op queued for retry.

## [0.4.39] — Revert the Tool-window experiment - it broke worse than it fixed

### Fixed
- **0.4.38's Tool-window experiment made the popup close itself immediately on open, with no
  click at all** - reported directly right after deploying it, well before any positioning/
  dragging feedback could even be gathered. Unlike `Qt.WindowType.Popup`, `Tool` windows ARE
  normal WM-managed windows, so the WM briefly tries to activate one on `show()` - then, per
  typical "utility window shouldn't steal focus" policy, immediately hands focus back to whatever
  was active before. Both `focusOutEvent` and the `_NET_ACTIVE_WINDOW` watchdog correctly (if
  completely unhelpfully) read that as "lost focus - close," closing the popup before the user
  could do anything with it. This is exactly the kind of real-window-manager interaction that
  can't be verified in an isolated Qt test harness (which is all that was checked before shipping
  0.4.38) - noted as a real gap, not repeated here.
- Reverted to `Popup`. The always-on-top behavior reported yesterday is real and unresolved, but a
  working annoyance is strictly better than a popup that can't stay open long enough to read.

## [0.4.38] — EXPERIMENTAL: stop the popup floating above every other window

### Changed
- **The popup stayed visually pinned on top of every other window, not just failing to auto-close
  on an outside click** - reported directly with a screen recording showing it still fully visible
  above Dolphin's window the entire time Dolphin was the one actually being used. Root cause:
  `Qt.WindowType.Popup` is "override-redirect" at the X11 protocol level, which independently of
  anything this app does means the window manager excludes it from the normal stacking order and
  renders it in a permanent above-everything layer - a different, more disruptive consequence of
  the same window-type choice already under scrutiny since 0.4.33.
- Switched to `Qt.WindowType.Tool`, a normal WM-managed window that should participate in ordinary
  raise/lower stacking. **Marked experimental and NOT yet confirmed working** - this file's own
  prior history already found Tool windows "frequently never activated by the WM at all" for a
  different reason (why Popup was adopted in the first place), and losing Popup's automatic X11
  grab very likely means losing the one mechanism that let *some* outside clicks close the popup
  at all. Verified only what's verifiable without a live window manager (all existing click/Escape/
  Sync-now handlers still function correctly on the new window type, via `QTest`) - actual on-top
  behavior, positioning, and dragging need the user's live confirmation before this can be called
  fixed rather than reverted.

## [0.4.37] — Push-update the popup instead of waiting on its 3s timer; add a sync ETA

### Added
- **Sync status now shows an ETA** - "Syncing... 50 items left (~6s left)" - requested directly.
  Tracked as a "streak": when a pair starts actively syncing, records the item count and a start
  time, then estimates from items-completed-since / elapsed-time. Restarts the baseline (rather
  than diluting the rate) if the remaining count *grows* mid-sync - i.e. new local/remote changes
  arrived while a sync was already in progress. No estimate shown for the first 3 seconds of a
  streak (not enough data yet) or while idle - both verified directly, along with the growth-
  restart behavior.

### Fixed
- **The popup only reflected new sync activity on its own 3-second timer, and only while already
  open** - reported directly as feeling slow. `PairSyncWorker` already emits a status-changed
  signal on every single step of a sync (this is also what now drives the ETA tracker above), but
  nothing connected it to the popup itself before. Wired it in `main_window.py`, coalesced to at
  most once every 500ms (matching the Folder Pairs table's own existing coalescing) so a fast
  multi-file sync doesn't hammer the widget rebuild - real activity now shows up in well under a
  second instead of waiting up to 3s.

## [0.4.36] — Revert the broken cursor fallback; add Escape-to-close instead

### Fixed
- **0.4.35's cursor-position fallback for click-outside-closes was built on a wrong assumption
  and didn't work either** - the user correctly pushed back ("still the same problem") and sent a
  screen recording proving it. Added temporary diagnostic logging and got direct proof: with the
  real cursor demonstrably moving around the desktop and clicking icons (visible in the recording),
  `QCursor.pos()` as read by this XWayland-hosted app stayed frozen at the exact same coordinate
  for 5+ continuous seconds, only updating again once the real cursor moved back over this app's
  own window. So `QCursor.pos()` has the *same* blind spot as `_NET_ACTIVE_WINDOW` for native-
  Wayland surfaces, not an independent signal the way 0.4.35 assumed - it's fundamentally the same
  hard XWayland/native-Wayland protocol boundary, not fixable from here. Removed the non-working
  fallback and its diagnostic logging rather than leave it in giving false confidence.
- **Added `Escape` to close the popup** - a genuinely reliable dismiss path, since keyboard input
  delivery to a widget that already has it is basic Qt behavior independent of observing any other
  window's focus or position (unlike every outside-click mechanism tried across 0.4.33-0.4.35).
  Verified in isolation with `QTest.keyClick`. Between this and the existing close button, there's
  now always a one-action way to dismiss the popup even though a genuine click-outside-closes
  can't be made reliable here without embedding a KWin compositor script (a much larger, more
  fragile addition - not pursued without the user explicitly wanting that trade-off).

## [0.4.35] — Fix click-outside-closes on this mixed XWayland/native-Wayland desktop

### Fixed
- **The popup didn't close when clicking elsewhere on the desktop** - reported directly, distinct
  from 0.4.33/0.4.34's summary-row issue. Root-caused, not guessed: this session runs KDE Plasma
  on native Wayland (`XDG_SESSION_TYPE=wayland`), while `__main__.py` forces this app specifically
  onto XWayland (required for window positioning to work at all - see that file's own comment).
  Both existing outside-click mechanisms - the popup's own X11 pointer grab, and the watchdog
  comparing `_NET_ACTIVE_WINDOW` via `xprop` - only ever see windows that XWayland itself is aware
  of. Confirmed directly: `xprop -id <the logged baseline window id>` resolved to nothing at all -
  it's a synthetic placeholder XWayland reports when there's no real XWayland-managed window
  active, which is exactly the case every time focus moves to one of the native-Wayland apps
  making up the rest of this desktop (i.e. most apps) - the property simply never changes for
  that, so the watchdog never fired for the single most common case of "clicked something else."
  Added a second, independent fallback to the same watchdog: global cursor position, which XWayland
  *does* track session-wide regardless of protocol (it needs to, for its own hit-testing) - closes
  once the cursor has stayed continuously outside the popup's bounds for 700ms, independent of
  what the WM reports as active. Verified in isolation (mocking `QCursor.pos()`, no live desktop
  needed): stays open with the cursor inside or only briefly outside, closes once it's been outside
  past the threshold, and correctly resets if the cursor comes back before then.

## [0.4.34] — Widen the sync summary's click target to the whole row

### Fixed
- **0.4.33's fix only made the "All synced!" text itself clickable, and the user reported it still
  not closing.** Reproduced directly with a simulated click (`QTest.mouseClick`) on the adjacent
  "✓" checkmark and on blank space within the same row, both still bare/inert - confirming the
  target was too narrow for a click landing anywhere else in that row. `_ClickableLabel` replaced
  with `_ClickableFrame`, wrapping the entire status row (checkmark + text) instead of just the
  text label, so a click anywhere in the row closes the popup. Verified with 5 simulated clicks:
  checkmark, blank row space, and the text all now close it; the "Sync now" button still works
  independently and does not also close the popup; click-outside-bounds is unaffected.

## [0.4.33] — Clicking the popup's sync summary now closes it

### Fixed
- **Clicking the tray popup's status summary ("All synced!" / "Syncing... N items left") did
  nothing** - reported directly by the user. It was a bare `QLabel` with no click handling of its
  own; the popup's own `mousePressEvent` only closes on a click *outside* its bounds, so a click
  landing on this label (inside the bounds) fell through to a no-op. Unlike an activity row, the
  summary has no specific file to open, so a click on it now just closes the popup - same as every
  other click inside the popup that actually does something. New small `_ClickableLabel` used only
  for this one label.

## [0.4.32] — Stop promising conflicts the Conflicts dialog can't actually show

### Fixed
- **The Folder Pairs badge/menu showed `folder_pairs.conflict_count` - a permanent lifetime
  counter that never decreases - as if it meant "N conflicts to review".** Reported directly by
  the user after clicking "View 4 Conflicts…" on the Desktop pair and finding nothing, right after
  I'd shipped the new Conflicts dialog: those 4 conflicts happened before conflicts were exempted
  from the activity log's pruning (0.4.28), so the count was real but the underlying records were
  already gone. Showing that number as an invitation to review was actively misleading. Fixed by
  driving the badge and the "View N Conflicts…" menu entry off a new `db.count_conflicts()` (what
  the dialog can actually show right now) instead - the menu entry no longer even appears when
  there's nothing left to review. `folder_pairs.conflict_count` still exists for the dialog's own
  "conflicts happened but the record is gone" explanatory note, just no longer surfaced as if it
  were an actionable count anywhere else.

## [0.4.31] — Fix repeating 404s after a Folder Pairs remote delete

### Fixed
- **`_execute_action`'s `DELETE_REMOTE` branch deleted the item on Graph and purged its
  `pair_files` baseline, but never tombstoned the matching `items` cache row** - caught live via a
  `tail` the user shared, showing the same file's `Downloading ...` immediately followed by a 404
  and `local filesystem op failed`, repeating every ~60s pair-sync pass. Root cause: with the
  `pair_files` baseline gone but the stale `items` row still present, the next reconciliation pass
  still saw the path in `list_descendants()`, now with no synced baseline, misclassified it as
  "new, remote-only" and tried to download an item that pair sync itself had just deleted -
  self-healing only once `DeltaSyncWorker`'s independent, slower poll caught up. `DELETE_REMOTE`
  now calls `db.mark_deleted()` right after the Graph delete succeeds, matching the pattern
  `conflict_actions.py` already had to use for the same reason. Verified directly against the live
  database: items deleted by this pass now show `deleted=1` with no lag, and the same file no
  longer round-trips through a failed download on the very next pass.
- This was surfaced while investigating a related, benign, and much larger-scale finding: pair
  sync has been correctly recycling thousands of orphaned `(1)`/`(2)`-suffixed duplicate files
  (leftovers from before this app existed) that have no local counterpart under their exact
  suffixed name - confirmed on a sample that the real, non-suffixed file remains untouched
  locally and remotely. Not a bug; left running at the user's request.

## [0.4.30] — Conflict review UI: pick which version to keep

### Added
- **Folder Pairs' existing "keep both" conflict auto-resolution is unchanged, but conflicts can
  now be reviewed and cleaned up** - requested directly, after the user compared this app's
  previous conflicts view (list-only) against Nextcloud's own conflict-resolution dialog, which
  lets you pick local or server per file. Kept the safe part of the existing design (conflicts
  still auto-resolve immediately as "keep both" - never stuck, nothing silently overwritten) and
  added the missing piece: each pair's existing Conflicts dialog now shows **Keep Local**, **Keep
  Server**, and **Dismiss** per conflict. "Keep Server" deletes the conflicted-copy file (locally,
  and remotely via OneDrive's recycle bin, not a hard delete). "Keep Local" uploads the conflicted
  copy's content over the original file's current remote version, replaces the local original with
  it, and removes the now-redundant copy. "Dismiss" leaves both files exactly as they are and just
  drops the conflict off the review list. Runs on a background thread with a confirmation prompt
  before either destructive choice, so the GUI never blocks on the network call.
- `activity_log` gained a `dismissed` column so reviewed conflicts drop out of the list without
  losing the permanent audit trail (conflict rows were already exempt from the log's normal
  pruning).
- Verified end-to-end against live Graph calls (`scripts/verify_conflict_resolution.py`): all
  three resolution paths, using a real manufactured both-sides-changed conflict, confirming file
  content and remote item state after each. Along the way, found and fixed a small pre-existing
  rough edge in the new resolution code itself - deleting a conflict copy's remote item without
  also tombstoning it in the local `items` cache could make the very next reconciliation pass
  briefly try to download the item that was just deleted (harmless - logged and self-heals once
  `DeltaSyncWorker`'s next poll catches up - but now avoided outright by calling `mark_deleted`
  immediately after a successful remote delete).

## [0.4.29] — Stop leaking upload sessions on every failed retry

### Fixed
- **Every failed upload attempt left its Graph upload session dangling, uncanceled** - confirmed
  directly via a `tail`'d log the user shared: `createUploadSession` itself started returning
  fresh `409 Graph conflict` errors on retries, not just on chunk PUTs. Graph rejects creating a
  new session for a target while an old, uncanceled one is still considered active for it - since
  this app created a brand-new session on every retry (each failed attempt, each 60s pair-sync
  pass) without ever calling the existing (but previously unused) `cancel_upload_session()`, failed
  attempts against a slow connection were piling up their own abandoned sessions and blocking every
  subsequent attempt, compounding with each retry. `upload_file()` now cancels its own session on
  any failure before letting the error propagate, so the next retry starts from a clean slate.
  Verified directly with mocked network calls: a forced-failing upload correctly cancels its
  session exactly once, while a successful one never cancels at all.
- **`upload_large()`'s chunk-resume loop had no cap** - on a connection that never lets a single
  chunk succeed, it would spin indefinitely within one call, retrying the same chunk every ~10s
  forever with no backoff and no chance for the outer retry layer or the new session-cleanup to
  ever run. Capped at 8 consecutive chunk failures before giving up and letting the caller's own
  retry (with a fresh session) pick it up next pass - verified directly: exactly 8 failures logged,
  then a clean raise, with the session correctly canceled by the fix above.

## [0.4.28] — Fix delta sync crash-looping on a path collision; smaller upload chunks

### Fixed
- **The whole-account delta sync started crash-looping** shortly after 0.4.27's upload fix let a
  previously-stuck large file's create attempt finally get past its conflict - confirmed live via
  a `tail`'d log the user shared directly: `sqlite3.IntegrityError: UNIQUE constraint failed:
  items.drive_id, items.path`, repeating every few minutes. Root cause: `_sync_once()` processes
  Graph's `/delta` feed in one loop, calling `upsert_item()` per item with no exception handling -
  when Graph's own feed returned two distinct items both claiming the same path (most likely a
  residual side effect of the earlier stuck-upload saga on OneDrive's own side), the INSERT's
  `ON CONFLICT(drive_id, id)` clause doesn't cover the *separate* `idx_items_path` unique index, so
  it raised instead of upserting - aborting the whole pass before `delta_link` could be saved. That
  meant the *next* poll re-fetched the exact same page and hit the exact same item again, forever,
  with no way to ever make forward progress on anything after it in the feed either.
  `upsert_item()` now catches this specific `IntegrityError`, rolls back, logs full diagnostic
  detail (both items' ids and the colliding path) and skips just that one item, letting the rest of
  the batch - and `delta_link`'s advancement - proceed normally. Verified in isolation first (two
  items forced to collide on the same path, confirmed the second is skipped without raising, the
  first stays intact, and the connection remains fully usable for a subsequent insert) before
  deploying.

### Changed
- **Upload chunk size lowered from ~10 MiB to ~1.25 MiB.** Raising the chunk timeout (0.4.27) made
  no measurable difference to how often chunks were timing out on a real slow connection - the
  failure interval stayed a steady ~10-11s regardless, meaning something at the connection level
  won't reliably carry a request that long no matter what timeout the client is willing to wait for.
  A much smaller chunk gives each individual PUT a real chance of completing before that ceiling.

## [0.4.27] — Fix files stuck re-uploading forever, exclude editor swap files

### Fixed
- **Some files retried uploading every single sync pass, forever, never once succeeding** -
  reported directly (the software kept re-updating certain files for no clear reason, more than
  one of them), reproduced and root-caused against two real stuck files (a 394MB file stuck for
  hours; a large archive in a different pair). Traced by comparing our local
  cache (`pair_files`/`items`), the actual local file, and a *live* direct Graph API call: our
  cache had zero record of the file ever syncing, but Graph rejected the create with
  `nameAlreadyExists` in ~350ms - too fast to be a real chunk transfer, confirming it was the very
  first `createUploadSession` call itself getting rejected, over and over. Graph believed an item
  already existed there (most likely an old orphaned/incomplete upload session or soft-deleted
  remnant not shown by browsing normally) that our own cache never learned about, so every pass's
  "create" was doomed before it started - the existing `GraphConflictError` handler just refreshed
  metadata (a no-op with no known item to refresh) and retried the identical doomed request next
  pass, forever.
  `GraphClient.upload_file()` now self-heals this specific case: on a create's `nameAlreadyExists`
  conflict, it looks up the real existing item at that path (new `get_item_by_path()`) and retries
  as a replace against the discovered id/etag, instead of reproducing the same failure indefinitely.
- **A second, previously-unreachable bug this immediately exposed**: unblocking the create-conflict
  meant these large files' chunked uploads could finally run for real, on a connection that turned
  out to time out on individual chunk PUTs - and `upload_large()`'s connection-failure recovery path
  was broken. It called Graph's upload-session status endpoint (`nextExpectedRanges`) to figure out
  where to resume, but then treated ANY 200 response from that status check as if it were a
  completed-upload response, including on a session that was still very much in progress - crashed
  every time with `KeyError: 'id'` in `db.upsert_item()`, since a status response has no such key.
  `_resume_upload_session()` now returns the parsed status itself instead of a raw response the
  caller could misinterpret this way, and `upload_large()` correctly resumes from the confirmed byte
  offset instead of ever fabricating a fake success.
- **Chunk upload timeout was too tight for a slow connection**: a ~10MB chunk needed ~175KB/s
  sustained throughput just to avoid timing out inside the old 60s window - confirmed directly, every
  single chunk of the 394MB file timed out this way. Raised to 180s (~58KB/s floor), generous enough
  for a genuinely slow link while a truly dead connection still times out, just not as eagerly.

### Added
- **New default exclude patterns** for Folder Pairs: `*.swp`/`*.swo`/`*.swx`/`*.kate-swp`/`*~`
  (editor swap/backup files), `.goutputstream-*` (GNOME atomic-save temp files), `chrome-*.desktop`
  (Chrome PWA shortcut files), `*.part`/`*.crdownload` (partial downloads) - added after two
  confirmed real examples (a Kate swap file and a Chrome PWA shortcut) showing up as repeating
  create/delete activity purely from being open, not from any real content change worth syncing.
  Applied to all 3 existing pairs on this machine directly, not just future new ones.

## [0.4.26] — Drop the background badge; blue when online, gray when offline/paused

### Changed
- **Removed the circular background badge from the icon** - requested directly ("change the color
  of the cloud instead of changing background... lets try without background"). `cloud_pixmap()`
  is back to just the cloud silhouette on a transparent background, color-parameterized so the
  same shape can be recolored per state instead of desaturating a rendered pixmap.
- **Icon now reflects real connectivity, not just pause state**: blue while online, gray while
  offline *or* paused - requested directly ("when it is online lets have a blue icon, when it is
  offline or paused show gray icon"). New `network_status.is_online()` queries NetworkManager's
  `Connectivity` D-Bus property (same pattern as the existing `is_metered()`, verified directly
  against this machine's real NetworkManager) - only `FULL` counts as online, since a captive
  portal or limited connection means real internet access isn't actually working yet either.
  Deliberately fails *open* to "online" on any query error, the opposite of `is_metered()`'s
  fail-closed default: a wrongly-gray icon claiming offline when this app has no way to tell is a
  more misleading failure than just showing normal and finding out for real on the next sync
  attempt. Folded into the same periodic timer that already checked metered status, renamed
  `_check_metered` → `_check_network_status` to reflect the wider scope, both routed through the
  same `_apply_sync_state()`/`_update_tray_icon()` reconciliation as before.

## [0.4.25] — A real original icon for this project, used consistently everywhere

### Changed
- **New self-drawn app icon** (`theme.cloud_pixmap()`) - requested directly ("can you create our
  own icon and use it everywhere?"), after settling that neither Microsoft's actual trademarked
  logo nor an icon theme's own interpretation (Papirus's "ms-onedrive") were going to be used. A
  white cloud glyph on a diagonal blue-gradient circle badge, entirely QPainter-drawn (no external
  image file, no icon-theme dependency) - `app_icon()` no longer prefers a themed icon at all, so
  this now looks identical everywhere regardless of what theme is installed: tray, window/About
  dialog, and the Applications-menu/KRunner icon.
- First draft of the cloud shape had a real bug worth noting: the three overlapping circles making
  up the cloud's lobes left visible gaps where the background color showed through, in a pinwheel
  pattern. Root cause was `QPainterPath`'s default fill rule (`OddEvenFill`) treating any point
  covered by an *even* number of overlapping shapes as a hole - fixed by switching to
  `WindingFill`, which correctly treats "covered by anything" as filled, before calling
  `.simplified()`.
- Applications-menu icon file regenerated to match; `kbuildsycoca6`/`plasmashell` caches refreshed
  again for this machine.

## [0.4.24] — Fix the popup blocking the tray icon's own right-click menu

### Fixed
- **The popup's explicit `grabMouse()` call (added in 0.4.14) was blocking the tray icon's own
  right-click context menu entirely** - reported directly, with a screenshot ("I cannot see
  system tray icon menus when I make a right click"). An explicit application-level mouse grab
  intercepts every click system-wide, including ones over a completely different process's window
  - here, the desktop shell's own panel, which owns the tray icon and its context menu. A
  right-click meant for that menu got consumed by the popup's grab instead of ever reaching it.
  `Qt.WindowType.Popup` already grabs the mouse automatically and is specifically built to
  correctly release and let a dismissing click still reach whatever's underneath (exactly how
  every `QMenu`/`QComboBox` dropdown in Qt already behaves without this problem) - removed the
  redundant explicit `grabMouse()`/`releaseMouse()` pair entirely, keeping the `mousePressEvent`
  override for the actual close decision, now fed by Qt's own native grab instead of an extra one
  on top of it.

### Changed
- Tray menu's "Open {DISPLAY_NAME}" action shortened to plain "Open OneDrive" - requested
  directly.
- Regenerated the Applications-menu/KRunner icon cache (`kbuildsycoca6 --noincremental` +
  `plasmashell --replace`) - the installed icon file itself was already correct (the Papirus
  "ms-onedrive" icon, confirmed via its file timestamp and pixel content), the mismatch reported
  ("this icon also should have been like systemray icon") was a stale desktop-shell icon cache,
  not a wrong file. Reiterated (again, requested directly a second time): Microsoft's actual
  OneDrive logo file itself still can't be used - this app has no license to redistribute it and
  isn't affiliated with Microsoft - Papirus's own OneDrive-styled interpretation remains the
  closest legitimate option.

## [0.4.23] — About lives only in the tray's right-click menu now

### Changed
- **Removed About from the popup's own account menu** - requested directly, right after 0.4.22
  added it there too ("remove About from the popup menu"). It now lives only in the tray's
  right-click menu (Open OneDrive → About → Quit), not duplicated in the popup's Pause sync /
  Settings / View online / Exit menu. The now-unused `on_about` callback plumbing was removed from
  `ActivityPopup` entirely rather than left as dead code.

## [0.4.22] — Also add About to the tray's right-click menu

### Changed
- **The tray icon's right-click context menu now has About too** (Open OneDrive / About / Exit),
  not just the popup's own account menu - requested directly ("if you show this About page only
  when user make a right click icon user can see like this Open OneDrive, About and Exit
  options"), matching the standard tray-icon convention of a quick app-level menu on right-click
  distinct from the full interactive popup on left-click. The About dialog itself moved from
  `activity_popup.py` into `MainWindow._show_about()` - one implementation now, reached via a new
  `on_about` callback from the popup's menu and directly from the tray context menu, instead of
  duplicating the dialog-building code in both places.

## [0.4.21] — Add an About entry (version, publisher, license)

### Added
- **"About OneDrive for Linux Client" in the account menu** - requested directly ("can you also
  put about option to give some information version and publisher informations?"). Shows the
  version (`constants.VERSION`), publisher, and license via a plain `QMessageBox.about()`.
  Publisher ("Hasan Altin", hasanaltin.com) and license (MIT) are pulled from README.md's own
  existing Author/License sections rather than invented here, so this can't silently drift from
  what the repo itself already says.

## [0.4.20] — Bandwidth limits and metered-connection auto-pause, new Settings tab

### Added
- **New "Settings" tab** (`gui/settings_panel.py`) with upload/download bandwidth limits (KB/s, 0 =
  unlimited) and an "Automatically pause sync on metered connections" checkbox - requested directly
  ("in the settings we should have upload and download settings plus also metered connection
  settings"). Persists straight to `sync_state` on every change, no separate Save button.
- **Real bandwidth throttling**, not just a stored setting: new `onedrive/rate_limiter.py`
  (`RateLimiter`, verified directly - a 100 KB/s limit sending 500 KB took exactly 5.0s; unlimited
  adds zero measurable overhead) wired into every `GraphClient` transfer call site -
  `download_content` (per-chunk), `upload_large` (per-chunk, one limiter for the whole transfer,
  not reset per chunk), and `upload_small`/`replace_small` (single-shot PUTs, throttled by sleeping
  the remaining budget after the request completes). `GraphClient` takes limit-getter callables
  reading fresh from `sync_state` on every call, so a changed setting applies to the very next
  transfer without needing to reconstruct anything.
- **Metered-connection detection** (`onedrive/network_status.py`, `is_metered()`) via
  NetworkManager's own D-Bus `Metered` property (`gdbus call`, no new Python dependency) - verified
  directly against this machine's real NetworkManager. Checked every 30s (plus once ~2s after
  startup) and reconciled through the same pause/resume machinery 0.4.18 already built: a new
  `MainWindow._apply_sync_state()` is now the single place that decides whether the background
  workers should be running, combining the user's manual pause with the metered-auto-pause setting,
  so the periodic check and manual toggle can't fight each other over worker state (e.g. resuming
  manually while still metered with auto-pause on correctly stays paused). The tray icon now also
  distinguishes "paused" from "paused - metered connection" in its tooltip.

## [0.4.19] — Popup flush against the taskbar, grayed-out tray icon while paused

### Changed
- **Popup now sits flush against the taskbar** instead of floating with a gap above it, matching
  Windows OneDrive/Nextcloud's own popups (requested directly - "keep the popup touched to the
  task bar"). `_TASKBAR_CLEARANCE` (a deliberate 56px gap, originally added as a safety margin on
  desktops that don't report taskbar space correctly) is now 0. Also cleared this machine's
  already-saved popup position - it had been dragged with a gap in an earlier session, and a saved
  position always wins over the automatic flush placement, so the clearance change alone wouldn't
  have been visible without this too.

### Added
- **Tray icon now grays out while sync is paused**, matching the real Windows OneDrive client's
  own convention (same cloud glyph, desaturated, rather than a different icon) - requested
  directly ("when it is stopped sync we should show onedrive paused on the icon"). New
  `theme.paused_tray_icon()`, wired through a new `MainWindow._update_tray_icon()` called from
  `_toggle_sync_paused()` and once at startup so a pause that was already active in a previous
  session shows correctly from the moment the tray icon first appears, not just after the next
  toggle. Tooltip also gets a " - Sync paused" suffix.

## [0.4.18] — Real display name, real pause/resume sync, consolidated menu

### Added
- **Real pause/resume sync**, requested directly after the account menu became clickable
  ("there is no pause sync and start sync button"). The menu's "Pause sync"/"Resume sync" toggle
  now actually stops/starts all four background workers (`DeltaSyncWorker`, `PinWorker`,
  `PairSyncWorker`, `MountSyncWorker`) via the exact same `_stop_background_workers`/
  `_start_background_workers` methods sign-out already used - not new pause-aware branching
  inside each worker's loop. The mount itself is untouched either way (browsing is pure
  local-cache reads, independent of these workers), so pausing just stops network activity, not
  file access. Persisted (`sync_state.sync_paused`) so it survives a restart instead of silently
  resuming; a fresh interactive sign-in always resets it back to unpaused.
- **The account name now shows the real Microsoft Graph display name** ("Hasan Altin") instead of
  a name derived from the email address's local part ("hasan.altin") - requested directly ("My
  name should be seen as it is seen in microsoft graph from Display Name"). New
  `GraphClient.get_display_name()` (`GET /me?$select=displayName`), fetched and cached
  (`sync_state.display_name`) the same way the profile photo already was, in both the main window
  header and the popup.

### Changed
- **Consolidated the popup's footer into the account menu.** Requested directly - "no need to
  have settings at footer... move view online to the menu... remove open folder because we have
  folder icon." The footer row (Open folder / View online / Settings) is gone entirely: Settings
  and View online moved into the account dropdown alongside the new pause/resume toggle, and Open
  folder was dropped outright since the header's folder-icon button already does the same thing.

## [0.4.17] — Make the popup's account name/chevron a real, working menu

### Fixed
- **Reported directly: "Drop down menu doesn't work."** The name/chevron next to the avatar in the
  popup header was genuinely inert, not just visually static - both the name `QLabel` and the "▾"
  `QLabel` had `WA_TransparentForMouseEvents` set, so a click passed straight through to the
  header's own drag-handling `eventFilter` underneath and did nothing at all. Replaced both with a
  single flat `QPushButton` showing "{name} ▾", wired to a real `QMenu` with **Settings** (same
  action as the existing footer link) and **Exit** (wired to `MainWindow._quit_app`). Deliberately
  does *not* add a "Pause sync" entry like Nextcloud's own menu has - there's no pause/resume
  plumbing behind any of the sync workers yet, and shipping a menu item that silently does nothing
  would just be this exact bug again under a different label.

## [0.4.16] — Show the real Microsoft account profile photo instead of initials

### Added
- **Main window header and the tray activity popup now show the signed-in account's actual Graph
  profile photo**, matching Nextcloud's client (requested directly, side-by-side against a
  screenshot of it) - previously both always showed a plain circular "H" initials placeholder,
  even though a real photo is available via `/me/photo/$value`. New `GraphClient.get_profile_photo()`
  (treats a 404 - no photo set on the account - as a normal `None` result, not an error) and
  `theme.photo_avatar()` (crops/circular-clips it to match `initials_avatar()`'s exact footprint,
  falling back to the initials version if the account has no photo or the bytes fail to decode).
  Verified end-to-end against the real signed-in account (55.9 KB JPEG, 420x420).
- Fetched once per sign-in/app-start on a background thread and cached to disk
  (`profile_photo.jpg` in the data dir) so it's available instantly on the next launch without
  waiting on a fresh network round-trip; the fetch result is marshalled back to the GUI thread via
  a new `WorkerSignals.avatar_ready` signal rather than touching the cached bytes directly from the
  background thread, since constructing a `QPixmap` off the GUI thread isn't safe.

## [0.4.15] — Use a real OneDrive-styled icon instead of the plain generic cloud

### Changed
- **Tray/window icon and the Dolphin Places sidebar shortcut now use an actual OneDrive-styled
  icon** instead of the plain self-drawn generic cloud silhouette used until now. Requested
  directly ("can you use original microsoft onedrive icon?") - can't ship Microsoft's actual
  copyrighted/trademarked logo file (this app has no license to redistribute it, and isn't
  affiliated with Microsoft), but the Papirus icon theme already installed on this system ships
  its own "ms-onedrive" icon - a blue-cloud icon its own artists drew for exactly this purpose,
  under Papirus's own license - which is a legitimate, already-local asset that reads as
  recognizably OneDrive. New `theme.app_icon()` prefers `QIcon.fromTheme("ms-onedrive")`, falling
  back to the old self-drawn cloud on any system without a theme that ships it (kept as
  `cloud_icon()`, unchanged) - same theme-icon-with-fallback pattern already used everywhere else
  in this app.
- The Dolphin Places sidebar bookmark's icon changed from the generic `folder-cloud` to
  `folder-onedrive` (a folder-shaped variant, appropriate for a location rather than the app
  itself - shipped by both Breeze and Papirus, so it resolves even without Papirus installed).
  `add_places_bookmark()` now also upgrades an already-written bookmark's icon in place, scoped
  strictly to this app's own bookmark block so it can't touch some unrelated bookmark that happens
  to share the old icon name.
- `install.sh`'s icon generation step updated to match (`app_icon()` instead of the raw self-drawn
  pixmap), so a fresh install's Applications-menu/autostart icon matches the running app's tray
  icon from the start. The already-installed icon file and desktop entries were also regenerated
  directly for this machine, and the existing Places bookmark's icon upgraded, without waiting for
  a full reinstall.

## [0.4.14] — Popup dismiss: stop tracking window activation, grab the mouse and act on the click directly

### Fixed
- **Reported directly again: the popup still only closes on an outside click "sometimes."** All
  three prior fixes (0.4.9-0.4.11) tried to infer "the user clicked elsewhere" indirectly, through
  some notion of window *activation* (`isActiveWindow()`, `applicationState()`, then polling the WM's
  `_NET_ACTIVE_WINDOW` against a baseline). The `_NET_ACTIVE_WINDOW` approach has a real blind spot
  that explains the "sometimes": its baseline is whatever window the WM considered active right
  before the tray icon was clicked - and clicking a Plasma tray icon typically does *not* shift
  window activation away from whatever app already had it. So if the user's dismiss-click lands back
  on that *same* already-active window (very plausible - e.g. clicking the visible Dolphin window
  sitting right behind the popup), nothing about "active window" ever looks like it changed, and the
  watchdog never fires. Clicking a genuinely different window happened to work, which is exactly the
  "sometimes" reported.
- Replaced the whole approach with the mechanism Qt actually provides for this: `Qt.WindowType.Popup`
  is documented to grab the mouse so any outside click is delivered to the popup directly - the popup
  now grabs explicitly (`grabMouse()`, redundant-but-harmless if Qt's own grab already worked) and
  handles it directly in a new `mousePressEvent` override, closing whenever the click's position
  isn't within its own bounds. This depends only on where the click physically landed, not on any
  window-manager focus/activation state - so it isn't subject to the same-window blind spot above,
  or to whatever made the two earlier Qt-signal-based attempts unreliable in the first place. The
  `_NET_ACTIVE_WINDOW` watchdog is left in place as a secondary fallback (still useful for e.g.
  Alt-Tab switching away with no click involved), but is no longer the primary mechanism.
- `releaseMouse()` added to `hideEvent()`, paired with the new explicit `grabMouse()` - an unreleased
  grab would otherwise capture every click on the whole desktop for as long as the (by then hidden)
  popup widget exists.

## [0.4.13] — Fix the actual cause of the false "You changed X" entries: a mislabeled event, not a real write

### Fixed
- **0.4.12's fix targeted the wrong layer.** Checked the activity log directly after reproducing
  the report again (opening a PDF that was never edited) and the underlying `activity_log` row was
  `'downloaded'`, not any kind of write/change event - the mount-write hash-diff fix from 0.4.12 was
  never in the picture, since no write ever happened in the first place. The real bug was in the
  popup's own `_EVENT_VERBS` label table: it mapped `"downloaded"` straight to the display verb
  `"changed"` unconditionally. That's correct for a Folder Pair (a pair-synced file is always fully
  local, so a `"downloaded"` row there only happens when reconciliation pulled down a genuine remote
  change), but wrong for the on-demand mount, where `ContentCache.ensure_cached()` logs the exact
  same `"downloaded"` event purely because `open()` triggered a first-time cache fill - i.e. just
  from viewing a cloud-only file, with zero content change involved. The popup now picks the verb
  based on the event's source: a mount-sourced download reads "You opened X", a pair-sourced one
  still reads "You changed X" as before.
- 0.4.12's fix (skip re-uploading a mount write if its content hashes identical to what it started
  as) is kept regardless - it's still a real, separate correctness improvement for genuine no-op
  writes, just not what caused this particular report.

## [0.4.12] — Don't treat a merely-opened file as "changed"

### Fixed
- **Opening a file through the mount (just viewing it, no edits) could get logged as "You changed
  X" and queued for a re-upload** - reproduced directly: opening a CSV in LibreOffice, which opens
  files `O_RDWR` and leaves behind its usual `.~lock.<name>#` lock file, triggered a spurious
  "changed" activity entry for a file that was never actually edited. `write()` unconditionally
  set the pending-write's `dirty` flag on any call, including a harmless zero-byte "probe" write
  some apps issue just to confirm a file descriptor is writable, with no real edit involved.
  Fixed on two levels: `write()` now only marks a handle dirty when it actually writes at least one
  byte, and - more generally, since a probe isn't the only way this can happen - `flush()`/
  `release()` now hash the file's content against a baseline hash captured the moment it was opened
  for writing, and skip the upload/log entry entirely if the bytes end up byte-identical to what
  they started as, regardless of what write/truncate calls happened in between.

## [0.4.11] — Bypass Qt entirely for popup-dismiss: ask the window manager directly

### Fixed
- **0.4.10's fix (polling `QGuiApplication.applicationState()`) also didn't work** - confirmed
  directly, again. Two different Qt-level "am I still focused" signals have now both failed to
  reflect this popup losing focus in this environment, which stopped looking like "wrong API" and
  started looking like neither one gets accurate native focus events delivered to it at all for
  this window type here. Given up on asking Qt and started asking the window manager directly
  instead: the popup now polls the WM's own EWMH `_NET_ACTIVE_WINDOW` root property via `xprop`
  (ground truth, not anything Qt is inferring) against a baseline captured at open time, and closes
  the moment it changes. Added logging around the baseline capture and any dismissal decision
  specifically so a further failure would leave actual diagnostic evidence instead of another guess.

## [0.4.10] — The popup-dismiss fix from 0.4.9 didn't actually work; corrected

### Fixed
- **0.4.9's fix for the popup not dismissing on an outside click didn't work** - confirmed
  directly: it stayed on top even after switching to an entirely different application. The
  `isActiveWindow()` polling it used turns out to be structurally unable to detect this for a
  `Qt.WindowType.Popup` window specifically - that flag makes the window override-redirect at the
  X11 level (so it can appear instantly with no window-manager negotiation), which also means the
  window manager never manages or tracks focus for it the normal way, so `isActiveWindow()` just
  never flips back to `False` once Qt has set it, no matter what else on screen actually has focus.
  Switched to polling `QGuiApplication.applicationState()` instead - tracked through a different,
  whole-application-level mechanism (the same one behind "did this app get backgrounded" on every
  desktop OS) rather than any one window's WM-managed state, so it isn't subject to the same
  override-redirect blind spot.

## [0.4.9] — Fix the tray popup not closing on an outside click

### Fixed
- **Clicking anywhere outside the tray popup (the desktop, another app) didn't dismiss it** -
  reproduced directly. `Qt.WindowType.Popup`'s automatic click-outside grab, and the
  `focusOutEvent` fallback already in place for when that grab is unreliable, both depend on this
  window manager actually delivering a deactivation/focus-out event - which it apparently doesn't
  do reliably here even under the XWayland session 0.3.2 already forces this app onto specifically
  to make that grab work. Rather than chase exactly which WM-specific event isn't arriving, the
  popup now polls (every 150ms while open) whether it's still the active window and closes itself
  the moment it isn't - a live check of actual state instead of a reaction to an event that might
  never come, so it no longer depends on getting that WM interaction exactly right.

## [0.4.8] — Actually root-caused the recurring "Transport endpoint is not connected" mount failure

### Fixed
- **Found the real cause of a mount that kept going silently unusable, after multiple sessions of
  adding diagnostics that never caught a single Python exception or native crash**: it was never a
  pyfuse3 or kernel bug. `systemctl stop`/`restart` sends a bare `SIGTERM`, which this app never
  intercepted - the process just died immediately, giving its own cleanup code (which calls
  `fusermount3` to unmount properly) no chance to run at all. The kernel has no way to know the FUSE
  server behind a mount is gone unless it's told, so it kept listing the mountpoint as mounted with
  nothing actually serving it - every access failed with "Transport endpoint is not connected" - and
  every subsequent app startup's own `is_mounted()` check saw that same stale entry and concluded
  "already mounted, nothing to do," leaving it broken indefinitely. Confirmed directly by
  correlating the mount's failure against the exact `systemctl restart` timestamps in the journal.
  Two fixes, addressing both sides of it: the app now handles `SIGTERM`/`SIGINT` and unmounts
  cleanly before exiting (so a normal restart/stop no longer creates the stale state at all), and
  as a safety net for any other way the process could die uncleanly (`kill -9`, a crash, a power
  loss), startup now detects a mountpoint that's listed as mounted but is actually unresponsive and
  force-unmounts it before deciding whether to auto-mount. Verified over several restart cycles
  against the real mount with no manual intervention needed.

## [0.4.7] — Single-instance guard: launching the app again opens the folder instead of duplicating it

### Fixed
- **Launching the app while it was already running (e.g. via the new Applications menu entry,
  with the autostart copy already up) silently started a second full process** - reproduced
  directly right after adding that menu entry. The second instance ran its own complete set of
  background sync workers hammering the same account and database alongside the first, and its
  Dolphin overlay-icon socket server stole the first instance's socket file out from under it
  (deletes-then-rebinds the same path), leaving the original instance's Dolphin integration
  silently dead until restarted - a real, observed failure mode, not a hypothetical one. A new
  `onedrive/single_instance.py` uses a `flock()`-held lock file (released automatically by the OS
  the moment a process exits, however it exits, so there's no stale-lock cleanup to get wrong) to
  detect this before any real startup work happens; a duplicate launch now just opens the
  already-running instance's OneDrive folder and exits immediately instead.

## [0.4.6] — Add an actual Applications menu entry (install.sh only ever installed autostart)

### Fixed
- **The app never showed up in the Applications menu, KRunner, or any other launcher search** -
  `install.sh` only ever wrote its `.desktop` file to `~/.config/autostart/`, which login-autostart
  mechanisms scan but regular application launchers never do. `~/.local/share/applications/` is the
  separate location every desktop's actual app menu/search scans, and nothing was ever installed
  there. `install.sh` now installs a copy to both locations (the menu entry unconditionally, even
  with `--skip-autostart`), and rebuilds the desktop's application cache (`kbuildsycoca6`/
  `update-desktop-database`) so it shows up immediately without needing a logout/login.

## [0.4.5] — Dolphin overlay-icon groundwork; better diagnostics for a recurring silent unmount

### Added
- **Groundwork for showing sync-status badges directly on files/folders in Dolphin**, matching
  Nextcloud's own desktop integration (the tray popup's green/blue badges from 0.4.3 only ever
  showed in that one popup). New `onedrive/sync_status.py` is the single shared source of truth for
  "is this path local, cloud-only, or syncing right now" - the popup now reads from it too, so it
  can never disagree with Dolphin. A new `OverlayServer` (`onedrive/dolphin_overlay_server.py`)
  runs in the background and listens on a Unix socket, verified end-to-end against the real account
  (including staying correct even while the mount itself was down, since it reads the sync database
  directly rather than the live filesystem). Three status emblem icons are installed to the standard
  `hicolor` icon theme fallback so they resolve regardless of what theme is active. The actual
  Dolphin-side piece - a native `KOverlayIconPlugin` - is written (`packaging/dolphin-overlay/`) but
  not yet built or installed; it needs KDE Frameworks 6 development packages this system doesn't
  have yet (see that directory's README).

### Changed
- **Better diagnostics for a mount that silently becomes unavailable with nothing useful logged
  anywhere** - observed repeatedly, still not root-caused. `pyfuse3.main()` returning with no
  exception looks identical whether the app itself asked for an unmount or the kernel side closed
  the connection for some unrelated reason, and nothing distinguished the two before now - the mount
  thread's exit path logs which one actually happened. `faulthandler` is now enabled for the whole
  process (writing to a separate `faulthandler.log`), in case this turns out to be a native-level
  crash (e.g. inside pyfuse3's C extension) rather than a Python exception, which the existing
  exception handling around the mount thread can't see or log on its own.

## [0.4.4] — Prefer the real system icon theme, self-drawn icons only as fallback

### Changed
- **File/folder icons now use the actual system icon theme when one is installed and configured**
  (confirmed working end-to-end after installing Papirus: `QIcon.fromTheme()` now resolves every
  name this app asks for, with a full range of sizes) - these look native and match whatever
  theme/variant is actually chosen, instead of always using the flat self-drawn icons added in
  0.4.2. Those self-drawn icons aren't gone, though: `QIcon.fromTheme()` still returns nothing at
  all on a system with no icon theme configured (confirmed on this exact machine before Papirus was
  installed), so every icon category still falls back to them automatically whenever the theme
  doesn't provide a given name - the file/folder icons never regress to looking identical again
  regardless of what's installed.

## [0.4.3] — Green vs blue badge: local file on disk vs. cloud-only placeholder

### Added
- **The small badge on each activity-list icon now distinguishes a file that's actually on your
  computer from one that only exists on OneDrive so far** - green checkmark for the former, a blue
  cloud outline for the latter, matching the same convention Nextcloud's own client uses (checked
  against a reference screenshot of its Nautilus integration). Only meaningful for files reached
  through the on-demand `~/OneDrive` mount, where a listed file can exist as a placeholder before
  its content is actually downloaded - a Folder Pair's files are always real, fully-downloaded
  local files by definition, so those always show the green checkmark.

## [0.4.2] — One row per synced item, with a real icon per file type

### Changed
- **The tray popup's activity list now shows one row per file/folder instead of collapsing a burst
  of changes into a single "A, B, C and N more" row.** The combining behavior existed to avoid a
  wall of comma-separated names during a large bulk sync, but the list is already capped to the 30
  most recent events, so showing each individually reads cleanly either way - and matches how every
  other synced-files client (Windows OneDrive, Nextcloud) actually presents this list.
- **Every row now shows an icon for its actual type** - a folder, a picture, a PDF, a Word/Excel/
  PowerPoint file, an archive, audio, video, or a plain document each get a distinct, colored
  icon, instead of every single row rendering identically. `QIcon.fromTheme()` (the previous
  approach) returns a null icon for nearly every one of these names on this system - the same root
  cause 0.3.2 already had to work around for the window/tray icon - so these are now self-drawn,
  matching that existing precedent instead of depending on the system icon theme at all. Knowing an
  event is a folder needed a genuinely new signal: `activity_log` didn't record that, so it gained
  an `is_folder` column, populated at every call site that already knows it (including a real gap
  found while wiring this up - `rmdir()` through the on-demand mount never logged a "deleted" event
  for a folder at all, unlike `unlink()` for files).

## [0.4.1] — Click a tray activity item to open it; fix Folder Pairs breaking on mount-created files

### Added
- **Clicking a file or folder in the tray popup's activity list now opens it** (via `xdg-open` -
  the file with its default app, a folder in the file manager), matching how every other synced
  file client works. A row that's actively syncing right now (the blue in-progress rows) is
  clickable too. A deleted item's row opens its containing folder instead, since the file itself
  no longer exists.

### Fixed
- **Any Folder Pair whose remote folder contained a file or folder created through the on-demand
  mount would fail to sync that item on every single pass**, logging a repeating "local filesystem
  op failed" - traced to a real gap in `PairSyncWorker._build_remote_map`, which read an item's
  `id` directly as its Graph id. That's always been true for items `DeltaSyncWorker`/`PairSyncWorker`
  themselves create, but the new offline-tolerant mount (0.4.0) can leave an item's `id` as a local
  synthetic placeholder forever, with the real Graph id living in a separate `remote_id` field -
  `_build_remote_map` sent that placeholder straight into a Graph URL and got a 400 back, over and
  over. Reproduced directly against a real account: a file created through `~/OneDrive` inside a
  folder also tracked by a Folder Pair (e.g. `~/Desktop`) never synced down to the paired local
  folder at all. Now resolves the real id correctly (and skips an item entirely if it hasn't synced
  yet, since the mount's own background worker already owns getting it there).

## [0.4.0] — Offline-tolerant on-demand mount: create/edit/delete anything while offline

### Added
- **The on-demand mount now supports full offline create/edit/delete for *any* folder**, not just
  folders explicitly set up as a Folder Pair - matching the reliability guarantee Folder Pairs
  already had. `mkdir`, file creation, content edits, deletes, and renames through `~/OneDrive`
  now all succeed instantly with zero network, and sync automatically - conflict-safe - once the
  network comes back.
  - Every write is local-first: it updates the local database and staged file immediately and
    queues the actual Microsoft Graph call for a new background `MountSyncWorker`, instead of
    making a synchronous Graph call on the FUSE thread (the old design, which just failed outright
    offline). Offline-created items get a real, permanent database row under a synthetic id the
    moment they're created, so they show up in directory listings and `stat()` for free.
  - The write queue is persisted (survives a crash or `kill -9` before anything synced) and staged
    file content lives at the same canonical, id-derivable path every other cached file uses, so a
    surviving queue entry can always find its bytes again.
  - A genuine two-sided conflict (edited offline, also changed remotely before reconnecting) is
    handled with the same keep-both pattern already proven for Folder Pairs: the local edit is
    preserved as a new `(conflicted copy ...)` file, and the original path is refreshed to the
    current remote version - both the database record and the actual local file content, not just
    the record (a real gap found in the old synchronous version, which never re-downloaded).
  - A subtle race was specifically hardened against: deleting a file while its offline create is
    still being uploaded in the background no longer risks the completed upload "resurrecting" the
    just-deleted file - the delete correctly waits for the create to settle, then removes the real
    uploaded copy.

### Fixed
These three were caught only by driving the real, live mount by hand after the automated
verification scripts above all passed - a reminder that a persisted queue can be internally
consistent and still get the actual filesystem tree wrong.
- **A file created inside an offline-created folder could vanish from directory listings the
  moment it finished syncing**, even though its database row was perfectly intact
  (`deleted=0`). `upsert_item()` always set a synced item's `parent_id` to whatever Microsoft Graph
  reported, which is always the parent's *real* id - but an offline-created parent keeps its local,
  synthetic id forever (only `remote_id` becomes the real one once it syncs), so the child ended up
  pointing at a `parent_id` that matched no row's `id` at all. Reproduced directly: `mkdir`, create
  a file inside it, edit that file - the edit's own upload response silently orphaned it.
  `upsert_item()` now resolves a Graph-reported parent id back to its local row (a no-op for any
  already-fully-synced parent, since its `remote_id` already equals its `id`).
- **Overwriting an already-synced file through the mount (e.g. a plain shell `>` redirect) could
  leave old trailing bytes behind** instead of truncating first - `open()`'s write path never
  checked the `O_TRUNC` flag at all (only `create()`'s already-exists branch did). Fixed by
  truncating there too, matching the existing `create()` behavior.
- **Editing a file and then immediately renaming it offline (before either had a chance to sync)
  could silently revert the rename**: the queued content upload's Graph response reflects the
  file's *current* server-side name, which is still the old one until the separately-queued rename
  actually runs - and applying that full response via `upsert_item()` stomped the already-applied
  local rename back to the old name before the rename op ever got to it. Content-only sync
  confirmations (a plain re-upload, and the conflict resolver's remote-content refresh) now go
  through `confirm_synced_item()` instead, which - by design - never touches name/parent/path.

## [0.3.5] — No timeout on any Graph call, sign-in state lost at boot, window on every autostart

### Fixed
- **No `requests` call anywhere in `graph_client.py` specified a timeout**, and `requests` has no
  default - a connection that was already open while online and then goes dead mid-request (as
  opposed to a clean, immediate DNS/refused-connection failure) could hang indefinitely rather
  than fail, blocking whatever FUSE operation was waiting on it forever. Reproduced directly:
  creating a file through the on-demand mount while offline left Dolphin's transfer dialog stuck
  indefinitely instead of failing cleanly. Every call site now has an explicit timeout - `(10, 30)`
  for ordinary metadata/API calls (applies to gaps between bytes on a streamed download, not the
  whole transfer, so a large-but-progressing download isn't cut off early), `(10, 60)` for
  chunk uploads.
- **Sign-in state could be lost at every autostart even with a valid saved token**: the token
  cache loads from the OS keyring, but the keyring's D-Bus backend isn't always up yet at the
  exact moment autostart fires (confirmed via a related D-Bus portal registration failure in the
  same session log) - a `keyring.get_password()` call landing in that window silently returns
  nothing, and there was no in-memory retry once the app had already loaded an empty cache. The
  token is now saved to both the keyring *and* a permission-locked (0600) file on every save, not
  only as an on-exception fallback - so a keyring race at boot can fall back to a file that's
  actually kept up to date, instead of one that was only ever written when keyring itself failed.
- **The main window popped up on every single autostart**, not just the first run - unlike every
  other background sync client this app's tray/mount design is otherwise modeled on. It now only
  auto-opens if the user isn't signed in yet (so a first-time user still has something to sign in
  with); a returning, already-signed-in user gets the tray icon only, exactly like a manual
  double-click reopen.

## [0.3.4] — Fix the whole mount dying on a single offline file access

### Fixed
- **Opening (or creating, or writing to) any file whose content wasn't already cached locally
  would kill the entire FUSE mount, for every file, if that content download/upload failed** -
  most commonly from having no network. `ensure_cached()` (and the Graph calls behind
  mkdir/unlink/rmdir/rename) correctly raise on failure, but nothing in the FUSE handler layer
  ever caught that and translated it into a proper FUSE error - the raw exception propagated
  straight through pyfuse3's session loop and killed the whole `trio` event loop backing the
  mount. The kernel side of the mount was left orphaned ("Transport endpoint is not connected"),
  even though every other file's *metadata* was sitting right there in the local cache and should
  have kept working. Confirmed via a real reproduction: turning WiFi off, then having any app
  (including just Dolphin refreshing) try to open one not-yet-downloaded file, took the entire
  `~/OneDrive` mount down until the process was killed and restarted.
- Every FUSE handler that can make a network call (`open`, `create`, `setattr`'s truncate path,
  `mkdir`, `unlink`, `rmdir`, `rename`) now catches failures and returns a proper `EIO` to the
  calling app instead - that one file/operation fails cleanly, the rest of the mount (browsing,
  already-cached files) keeps working exactly as it should while offline. Verified by directly
  reproducing the exact crash (a mocked `ConnectionError` from the same code path the real crash
  came from) against the fixed code and confirming it now raises a proper `FUSEError` instead.

## [0.3.3] — Fix crash-on-boot when network isn't up yet, mount from cache offline

### Fixed
- **The app would crash outright on every autostart launch if the network wasn't fully up yet**
  (routine at login/boot - autostart fires before NetworkManager finishes connecting).
  `msal.PublicClientApplication()` always performs a live network call during construction (no
  parameter skips it - confirmed against MSAL's own source), and that constructor ran inside
  `MainWindow.__init__()`, before any window was even shown - an unhandled `ConnectionError` there
  took the whole process down with exit code 1 before anything was on screen. Verified via a real
  systemd autostart failure log. `AuthManager` no longer performs this network call eagerly:
  sign-in state now reads directly from the local token cache (works with zero network), and MSAL
  client construction is deferred/retried lazily only when something actually needs live auth
  (device-code sign-in, silent token refresh) - a network failure there now correctly surfaces as
  a retryable error instead of a fake "please sign in again" prompt.
- **Auto-mount required a successful network sync first**, so even without the crash above, a
  returning user with a fully populated local cache from earlier sessions couldn't get their
  on-demand mount back while offline - despite every byte of metadata needed to serve it already
  sitting in the local database. Auto-mount now runs immediately at startup using whatever's
  cached, independent of whether `DeltaSyncWorker` has completed (or can complete) a network pass.
  Verified end-to-end with DNS fully blocked: the app starts, reports the correct signed-in
  account, and mounts `~/OneDrive` from cache alone.

## [0.3.2] — Nextcloud-style tray popup, install script, conflict visibility

### Added
- `install.sh`: automates the README's setup steps (venv, dependencies, app icon, login
  autostart). Safe to re-run; `--skip-autostart` opts out of the login-autostart step.
- Tray popup redesigned to match the Nextcloud desktop client: header with avatar/account,
  search box that filters activity, "All synced! / Sync now" status row, and grouped multi-file
  activity rows. Files actively uploading/downloading right now show at the top with a blue
  in-progress badge, distinct from completed items' green checkmark.
- Folder Pairs' status line now shows live progress ("Syncing 745 items...", "Uploading
  (42/745): path") instead of a static "syncing" placeholder for the whole pass.
- "View N Conflicts…" on a pair's "⋯" menu lists which specific files hit a genuine two-sided
  conflict and what they were preserved as - previously only a bare count was visible anywhere.
- Folder create/delete now show up in the activity feed (previously only file uploads/downloads/
  deletes did, so a folder-level change looked like it had gone completely undetected).

### Fixed
- The tray popup could land anywhere from mid-screen to behind the taskbar depending on the
  desktop - traced to KDE Plasma's Wayland session not letting a plain client window position or
  move itself. The app now runs under XWayland (`QT_QPA_PLATFORM=xcb`), where normal positioning
  and click-outside-to-dismiss both work as expected.
- `activity_log`'s pruning kept only the 500 most-recent rows *globally*, so a single large bulk
  sync could evict genuine conflict records before anyone had a chance to see which files were
  affected. Conflict entries are now exempt from pruning.
- Deletes were syncing correctly but never appeared in the activity feed at all (missing
  `log_activity()` call) - looked exactly like "not detecting deletes," when detection itself was
  actually fast.
- The window/tray icon and the popup's folder-shortcut icon depended on `QIcon.fromTheme()` names
  most icon themes don't actually ship, silently falling back to a generic/invisible icon. Both
  are now self-drawn (no dependency on the system icon theme).
- "View online" always opened the consumer `onedrive.live.com`, which doesn't resolve for work/
  school accounts (this account's real URL lives under a SharePoint tenant domain) - now uses the
  signed-in drive's actual `webUrl` from Graph.
- Closing the main window no longer pops a "still running in the background" tray notification
  every single time.

## [0.3.1] — Freeze fix, GUI redesign

### Fixed
- A critical freeze/"Not Responding" bug: `list_descendants()` (used by every Folder Pairs sync
  pass) ran a SQL recursive CTE that SQLite planned using the wrong index once the metadata cache
  reached account scale (150k+ rows) — 57+ seconds per call, pegging a core and making the whole
  app appear hung. Rewritten as a plain Python breadth-first walk over indexed lookups; same query
  now takes ~4ms.
- Folder Pairs' default exclude patterns (`.db-wal`, `.db`, `.log`, etc.) were literal
  `fnmatch` patterns with no wildcard, so they only matched an exact filename and never actually
  excluded anything like `.nextcloudsync.log` — causing pointless repeated re-uploads of another
  app's own churn files. Defaults now use proper globs (`*.db-wal`, `*.log`, `.nextcloud*`, ...).
- GUI status-change signals are now debounced and disk-usage (cache size) is computed on a
  background thread instead of the GUI thread, removing a secondary source of UI unresponsiveness
  under heavy sync activity.

### Changed
- Main window redesigned with a branded header bar (avatar, account name, gradient background)
  and a styled tab bar, replacing the plain default Qt look.
- Folder Pairs list redesigned from a plain table into a Nextcloud-style card list: colored status
  icons (synced/syncing/error/paused), bold title with local↔remote subtitle, per-row overflow
  menu (Enable/Disable, Edit Excludes, Remove).
- Recent Activity tray popup redesigned to match Windows OneDrive's own popup more closely: file
  icons now carry a small green "synced" checkmark badge, and the footer ("Open folder", "View
  online", "Settings") is now plain flat text links instead of bordered toolbuttons.

## [0.3.0] — Writable mount, polish

### Added
- Full read/write support in the on-demand FUSE mount: create, edit, rename, delete, `mkdir`
  through the mount, with uploads happening on `flush()` (the point where `close()` can actually
  report an error back to the calling app, unlike `release()`).
- Conflict protection for direct mount edits: before overwriting an existing remote file, the
  current remote state is checked; on a genuine conflict the local edit is preserved as a new
  `(conflicted copy ...)` file instead of being silently discarded.
- Exclude-pattern filtering for Folder Pairs (glob patterns, e.g. `*.tmp`, `.sync_*.db*`,
  `~$*`), with sensible defaults, editable per pair from the GUI.
- Auto-mount on startup (remembers the last-used mountpoint) and an XDG autostart entry so the
  app comes back after a reboot without manual intervention.
- System tray "recent activity" popup (styled after Windows OneDrive's own tray popup) showing
  recently synced files with per-type icons and relative timestamps.
- Project renamed from the internal working name `onedrive-native` to **OneDrive for Linux
  Client** (Python package `onedrive`), published to GitHub.

### Fixed
- A real bug in `GraphClient._write()`: caller-supplied headers (e.g. `Content-Type` on
  uploads) collided with the auth header, crashing every write call before this was caught by
  testing against the live API instead of mocks.
- Folder deletes in Folder Pairs could loop forever: the `If-Match` header used a stale cached
  etag that never got refreshed, so Microsoft correctly rejected the delete every single sync
  pass. Now uses the freshly-rebuilt remote metadata for each attempt.
- Reconciliation no longer misclassifies folders using file-style content diffing (a folder's
  etag naturally drifts on any child change and isn't a meaningful "conflict" signal).

## [0.2.0] — Folder Pairs (two-way sync)

### Added
- "Folder Pairs": pick any local folder and pair it with any remote OneDrive folder (independent
  naming/location, Nextcloud-style), with real two-way sync — local edits upload, remote edits
  download, deletes propagate both directions.
- A pure, independently-tested three-way (local / remote / last-synced) reconciliation engine
  covering every combination of create/edit/delete on either side, including the asymmetric
  "modified one side, deleted the other" cases.
- Conflict resolution: on a genuine two-sided conflict, both versions are kept — the local edit
  is renamed to a `(conflicted copy ...)` file and uploaded as new, the current remote version is
  downloaded to the original name. Crash-safe: an interruption mid-resolution self-heals on the
  next sync pass with no special recovery code needed.
- Local file-change watching (`watchdog`) with debouncing, plus a periodic full reconciliation
  pass as a catch-all for changes made while the app wasn't running.
- OAuth scope expanded from read-only to read/write (`Files.ReadWrite`, `Files.ReadWrite.All`),
  with automatic re-prompt for existing sign-ins when a write call reveals the cached token is
  missing the new permissions.

## [0.1.0] — Initial on-demand read-only mount

### Added
- On-demand FUSE mount of the whole OneDrive account: directory listings are served entirely
  from a local SQLite cache of remote metadata (kept fresh via Microsoft Graph's `/delta`
  endpoint), so browsing is instant with zero network calls — file content only downloads when a
  file is actually opened.
- Per-folder "always keep on this device" pinning: eagerly downloads and keeps a folder's content
  available offline.
- Device-code sign-in flow, PyQt6 GUI with a lazy-loaded folder tree, system tray with
  mount/unmount control.

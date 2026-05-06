"""Subprocess executor abstraction.

Allows collectors to run commands either locally (`LocalExecutor`) or on a
remote host via SSH with ControlMaster reuse (`SSHExecutor`). Step 1 shipped
LocalExecutor only; step 2 added a stubbed SSHExecutor so multi-cluster
TOML could parse. Step 3 (this file) ships the real SSHExecutor.

Multi-cluster contract: each `Cluster` owns one Executor. Collectors take
the Executor as their first argument (no globals). The same code path runs
unchanged whether the cluster is local or remote — the only difference is
how `executor.run(argv)` reaches its destination.

SSH lifecycle (since 2026-05-05): the ControlMaster is bound to the
rohanboard process — `ControlPersist=60` (a stale mux self-cleans 60 s
after the last call), pre-flight `ssh -O check` skips wedged sockets, and
an atexit + SIGTERM/SIGINT handler runs `ssh -O exit` on every known mux
before the process dies. A per-host semaphore caps in-flight probes at 1
so a slow/hanging probe blocks the next dispatch instead of stacking
hundreds of stuck `ssh` children behind a dead socket.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shlex
import signal
import subprocess
import threading
from typing import Protocol


log = logging.getLogger(__name__)


class Executor(Protocol):
    async def run(self, argv: list[str], timeout: float = 15.0) -> str: ...

    async def whoami(self) -> str:
        """Return the username on the executor's target.

        For LocalExecutor this is `$USER`; for SSHExecutor this issues
        `whoami` against the remote (cached after the first call) so the
        CLUSTER's user is detected — not the local $USER. This generalises
        the LRZ "Invalid user id: hli" failure: rohan happens to share the
        WSL username `hli`, but LRZ's user is `di35dob` etc. Callers that
        construct argv with `-u <user>` (squeue/sacct) or `~user/...`
        path expansions MUST go through this helper, not `os.environ`.
        """
        ...


class LocalExecutor:
    """Runs argv locally via asyncio.create_subprocess_exec, returns stdout."""

    async def run(self, argv: list[str], timeout: float = 15.0) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cancelled = False
        # Catch ALL exceptions (TimeoutError + CancelledError) so subprocess
        # is killed+reaped on cancel; otherwise the next tick keeps stacking
        # ssh procs that the OS owns but python no longer awaits.
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException:
            cancelled = True
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.shield(proc.wait())
                except BaseException:
                    pass
            raise
        if proc.returncode != 0 and not cancelled:
            raise RuntimeError(
                f"command {argv!r} failed rc={proc.returncode}: {err.decode(errors='replace')[:500]}"
            )
        return out.decode(errors="replace")

    async def whoami(self) -> str:
        """Return the local username. For LocalExecutor this is just $USER."""
        return os.environ.get("USER", "")


# ──────────────────────────────────────────────────────────────────────
# SSH ControlMaster lifecycle registry
# ──────────────────────────────────────────────────────────────────────
#
# Every SSHExecutor instance registers itself here on construction.  At
# process shutdown (atexit + SIGTERM/SIGINT), `_cleanup_all_masters` runs
# `ssh -O exit <host>` on each registered executor, then unlinks the
# socket file and rmdir's the socket dir if empty.  This is the safety
# net that makes ControlPersist short-lived: a `ControlPersist=60` mux
# would self-clean within a minute anyway, but the explicit `-O exit`
# means a clean shutdown collects sockets immediately rather than
# leaving wedged-socket potholes for the *next* rohanboard run.
#
# The registry uses a threading.Lock (not asyncio) because shutdown
# handlers run outside the event loop.
_REGISTRY_LOCK = threading.Lock()
_REGISTERED_EXECUTORS: list["SSHExecutor"] = []
_SHUTDOWN_INSTALLED = False


def _install_shutdown_hooks() -> None:
    """Install atexit + SIGTERM/SIGINT cleanup once per process."""
    global _SHUTDOWN_INSTALLED
    if _SHUTDOWN_INSTALLED:
        return
    _SHUTDOWN_INSTALLED = True
    atexit.register(_cleanup_all_masters)
    # Chain rather than replace: Textual installs its own SIGINT handler,
    # and we want our cleanup to run *in addition* to whatever else is
    # registered.  We snapshot the previous handler and call it after.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
        except (ValueError, OSError):
            # Not in main thread; signal API unavailable.  atexit still covers us.
            return

        def _handler(signum, frame, _prev=prev):  # noqa: ANN001
            _cleanup_all_masters()
            if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                try:
                    _prev(signum, frame)
                except SystemExit:
                    raise
                except Exception:
                    log.debug("previous %s handler raised", signum, exc_info=True)
            # If the previous handler was the default, raise the appropriate
            # exit so the process actually terminates after cleanup.
            elif _prev == signal.SIG_DFL:
                # Re-raise via default behaviour.
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not in main thread; skip.
            pass


def _cleanup_all_masters() -> None:
    """Run `ssh -O exit` on every registered SSHExecutor's mux, unlink
    sockets, and rmdir empty socket dirs.  Safe to call multiple times."""
    with _REGISTRY_LOCK:
        executors = list(_REGISTERED_EXECUTORS)
        _REGISTERED_EXECUTORS.clear()
    socket_dirs: set[str] = set()
    for ex in executors:
        try:
            ex._shutdown_master()
        except Exception:
            log.debug("error shutting down master for %s", ex.host, exc_info=True)
        sock_dir = ex._socket_dir
        if sock_dir:
            socket_dirs.add(sock_dir)
    for d in socket_dirs:
        try:
            # Only remove if empty — don't clobber sockets owned by another
            # rohanboard process that may share /tmp/rohanboard-ssh-cm/.
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass


class SSHExecutor:
    """Runs argv on a remote host via ssh, with ControlMaster reuse.

    The first call to `.run` warms the ControlMaster (cold ~1-2s); subsequent
    calls reuse the master socket (~50ms). Idle masters self-clean after
    `ControlPersist=60` seconds so a wedged mux can never outlive the
    rohanboard process by more than a minute. The `bash -lc` wrapper
    ensures login-shell env vars (e.g. `$MCMLSCRATCH` on LRZ via
    `/etc/profile.d/mcml_scratch.sh`) are available — set
    `use_login_shell=False` to run argv directly without the shell.

    Defaults are chosen for non-interactive monitoring:
      - BatchMode=yes  — never prompt for password / passphrase
      - ServerAliveInterval=30, ServerAliveCountMax=3 — kill dead masters
      - ControlMaster=auto, ControlPersist=60 — share connection
      - ControlPath=/tmp/rohanboard-ssh-cm/cm-%r@%h:%p — per-(user,host,port)

    Lifecycle bindings (since 2026-05-05):
      - Pre-flight `ssh -O check` (3 s timeout) on every `.run`; if the mux
        is wedged the stale socket is unlinked and a fresh master created.
      - At-most-one in-flight probe per host (`asyncio.Semaphore(1)`); a
        hanging probe blocks the next dispatch instead of accumulating
        stuck `ssh` children behind a dead socket.
      - `atexit` + SIGTERM/SIGINT handler runs `ssh -O exit` and removes
        the socket; the master dies with the python process.
    """

    # ServerAliveInterval=20 / Count=3 → dead-server detection in ~60 s
    # (was 30/3 = 90 s). LRZ kills idle TCP sessions silently; with the
    # old 90 s window the client kept thinking the mux was alive while
    # the server side had GC'd it, producing the "another probe is
    # already in flight" wedge until the rest of the chain timed out.
    # ConnectTimeout=10 bounds the cold-handshake (ProxyJump-via-
    # lrzlogin path) so a fully unreachable cluster fails fast rather
    # than blocking the master-warm budget for 30 s.
    _DEFAULT_OPTS: tuple[str, ...] = (
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10",
        "-o", "ControlMaster=auto",
    )

    def __init__(
        self,
        host: str,
        control_path: str | None = None,
        persist: str = "60",
        use_login_shell: bool = True,
    ) -> None:
        self.host = host
        self.persist = persist
        self.use_login_shell = use_login_shell
        self._socket_dir: str | None = None
        if control_path is None:
            # Use a rohanboard-specific socket dir to avoid colliding with the
            # user's other ssh sessions (e.g. their hand-launched `ssh lrz`
            # ProxyJump'd through `lrzlogin`). A 0-byte stale socket left by an
            # ad-hoc client at the OpenSSH default `~/.ssh/cm-%r@%h:%p` will
            # wedge any subsequent BatchMode=yes connection on the SAME path,
            # leaving rohanboard with hung scontrol/squeue calls forever. The
            # dedicated dir lives under /tmp so it auto-clears on reboot.
            sock_dir = "/tmp/rohanboard-ssh-cm"
            os.makedirs(sock_dir, exist_ok=True)
            control_path = f"{sock_dir}/cm-%r@%h:%p"
            self._socket_dir = sock_dir
        else:
            # If caller passed an explicit path, infer its dir for cleanup
            # only when it's a regular directory (not a `%`-templated parent).
            parent = os.path.dirname(control_path)
            if parent and "%" not in parent and os.path.isdir(parent):
                self._socket_dir = parent
        self.control_path = control_path
        self._extra_opts: tuple[str, ...] = (
            "-o", f"ControlPath={control_path}",
            "-o", f"ControlPersist={persist}",
        )
        # Master-warm lock: refresh fans out 4-5 ssh calls in parallel; without
        # serialization they ALL race ControlMaster=auto creation, none win,
        # and on slow links (LRZ ProxyJump via lrzlogin) every one of them
        # hangs in connection setup forever. The lock makes the FIRST call
        # warm the master alone (~3-5s); the rest wait, then reuse the socket.
        self._master_lock: asyncio.Lock | None = None
        self._master_ready: bool = False
        # In-flight semaphore: at most ONE probe at a time per host.  Without
        # this, when the mux wedges (lrz mux dying mid-flight on 2026-05-05),
        # the render loop fires a fresh ssh probe every tick — they all hang
        # in n_tty_read on the dead socket and accumulate (1,259 stuck ssh
        # children in 24 min observed).  With the semaphore, a hanging probe
        # blocks the next dispatch; the caller times out cleanly and surfaces
        # "stale" rather than stacking dispatches.
        self._inflight: asyncio.Semaphore | None = None

        # Cached remote username — populated lazily on first `whoami()` call.
        # Generalises the LRZ user-resolution bug: `os.environ["USER"]` is
        # the WSL user (hli), which is wrong for any cluster whose login
        # name differs (LRZ → di35dob). Callers that need `-u <user>` or
        # `~user/...` MUST go through `await executor.whoami()`.
        self._remote_user: str | None = None
        self._whoami_lock: asyncio.Lock | None = None

        # Register for at-shutdown cleanup.
        with _REGISTRY_LOCK:
            _REGISTERED_EXECUTORS.append(self)
        _install_shutdown_hooks()

    # ────────────────────────────────────────────────────────────────────
    # mux lifecycle (sync — runs from event loop AND shutdown handlers)
    # ────────────────────────────────────────────────────────────────────

    def _resolved_control_path(self) -> str:
        """Resolve the literal socket path for `ssh -O <op>` calls.

        OpenSSH expands `%r/%h/%p` from the connect-time host/port/user, so
        for our purposes substituting host into `%h` is enough — `%r` is
        the local user (already expanded by ssh against $USER) and `%p` is
        always 22 unless overridden in ssh_config (we don't override).

        We don't need to read the actual socket path; `ssh -O check <host>`
        with `-S <ControlPath>` and the *same* token string ssh used to
        create it will reach the same socket.  So we just pass the literal
        token-bearing path to `-S`.
        """
        return self.control_path

    def _mux_ctl_argv(self, op: str) -> list[str]:
        """argv for `ssh -O <op> <host>` against the same ControlPath."""
        return [
            "ssh",
            "-o", f"ControlPath={self.control_path}",
            "-O", op,
            self.host,
        ]

    def _mux_check_blocking(self, timeout: float = 3.0) -> bool:
        """Synchronous `ssh -O check`.  Returns True iff the mux is alive.

        Used by the pre-flight in `.run` (wrapped via asyncio.to_thread to
        avoid blocking the event loop) and by `_shutdown_master` (called
        from atexit, no event loop available).
        """
        try:
            res = subprocess.run(
                self._mux_ctl_argv("check"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
        return res.returncode == 0

    def _unlink_wedged_socket(self) -> None:
        """Remove the literal socket file on disk (best-effort).

        Only safe when `_mux_check_blocking` returned False — otherwise we'd
        be stomping on a live master.  The path may contain unexpanded `%`
        tokens; that's fine, os.unlink will just ENOENT them harmlessly.
        """
        # Best-effort glob: substitute %h with the host so the unlink hits the
        # actual file ssh created.  %r and %p come from the ssh-time user/port;
        # we don't have those without re-shelling, so try the templated path
        # first and fall back to a glob.
        candidates = [self.control_path.replace("%h", self.host)]
        if "%" in candidates[0]:
            import glob
            # Replace remaining tokens with `*` for a glob match.
            pattern = candidates[0]
            for tok in ("%r", "%p", "%C", "%i", "%L", "%l", "%n", "%u"):
                pattern = pattern.replace(tok, "*")
            candidates.extend(glob.glob(pattern))
        for c in candidates:
            try:
                os.unlink(c)
                log.debug("unlinked wedged ssh mux socket %s", c)
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("could not unlink %s", c, exc_info=True)

    def _shutdown_master(self) -> None:
        """Run `ssh -O exit` on this executor's mux (best-effort).

        Called from atexit / SIGTERM / SIGINT handlers — must be sync, must
        not raise, and must complete quickly so the process actually exits.
        """
        if not self._master_ready:
            return
        try:
            subprocess.run(
                self._mux_ctl_argv("exit"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except Exception:
            log.debug("ssh -O exit %s failed", self.host, exc_info=True)
        # Unlink whatever socket may remain.  ssh -O exit *should* clean it
        # up itself, but if the mux was already dead the socket file lingers.
        self._unlink_wedged_socket()
        self._master_ready = False

    def _mark_mux_dead(self) -> None:
        """Tear down the mux state so the NEXT call re-warms from scratch.

        Called when the running ssh subprocess timed out / was cancelled in
        a way that suggests the underlying TCP / mux is broken (slow LRZ
        kill, network blip during a multi-second collect). Without this,
        the next call's `_warm_master` would see `_master_ready=True`,
        run `ssh -O check` against a dead socket (which may itself hang
        beyond its 3 s timeout), and waste another tick.

        Best-effort + sync: kicks off `ssh -O exit` in the background
        (via the `asyncio.to_thread` wrapper if a loop is running, or
        the synchronous subprocess otherwise) and unlinks the socket
        immediately. Either way, by the time the next `_warm_master`
        runs, `_master_ready` is False — that's the load-bearing flip.
        """
        if not self._master_ready and not os.path.exists(
                self.control_path.replace("%h", self.host)):
            return
        self._master_ready = False
        # Best-effort ssh -O exit; bound to 2s so a hung mux doesn't
        # block the cleanup. Failure is OK — the unlink below catches
        # the leftover socket file.
        try:
            subprocess.run(
                self._mux_ctl_argv("exit"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        except Exception:
            log.debug("ssh -O exit during mark_dead failed for %s",
                      self.host, exc_info=True)
        self._unlink_wedged_socket()

    # ────────────────────────────────────────────────────────────────────
    # cmd construction
    # ────────────────────────────────────────────────────────────────────

    def _build_cmd(self, argv: list[str]) -> list[str]:
        # OpenSSH joins remote argv with a single space and ships the result
        # as ONE shell command line — quoting in the local argv list is lost
        # on the remote. So we shlex-quote each piece and pass the whole
        # remote command as a single string. This way `bash -lc 'scontrol
        # show node --all'` arrives intact instead of degrading to
        # `bash -lc scontrol show node --all` (which bash parses as `-c
        # scontrol` with positional args, returning the help banner).
        if self.use_login_shell:
            inner = " ".join(shlex.quote(a) for a in argv)
            remote_cmd = f"bash -lc {shlex.quote(inner)}"
        else:
            remote_cmd = " ".join(shlex.quote(a) for a in argv)
        return [
            "ssh", *self._DEFAULT_OPTS, *self._extra_opts, self.host,
            remote_cmd,
        ]

    # ────────────────────────────────────────────────────────────────────
    # warm + run
    # ────────────────────────────────────────────────────────────────────

    async def _warm_master_inner(self, timeout: float) -> None:
        """Inner master-warm body. Caller wraps in `asyncio.shield` to
        protect from `run_worker(exclusive=True)` cancellation while a
        slow ProxyJump connection (LRZ ~10.5 s cold) is in flight."""
        # Lazily build the lock — `__init__` runs outside the event loop.
        if self._master_lock is None:
            self._master_lock = asyncio.Lock()
        async with self._master_lock:
            # If we believe the master is up, double-check with -O check.
            # `ssh -O check` is a tiny round-trip on the existing socket; if
            # the mux died (its python parent killed, or ControlPersist
            # expired), this returns non-zero and we tear down.
            if self._master_ready:
                alive = await asyncio.to_thread(self._mux_check_blocking, 3.0)
                if alive:
                    return
                log.info("ssh mux for %s is wedged; tearing down and re-warming", self.host)
                self._master_ready = False
                self._unlink_wedged_socket()
            # Cheap probe through the master path. The `echo` exercises the
            # same options as real calls; if a prior process left the master
            # warm we get a fast reuse.
            cmd = self._build_cmd(["echo", "_rb_ssh_warm_"])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except BaseException:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.shield(proc.wait())
                    except BaseException:
                        pass
                raise
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ssh {self.host} master-warm failed rc={proc.returncode}: "
                    f"{err.decode(errors='replace')[:500]}"
                )
            self._master_ready = True

    async def _warm_master(self, timeout: float) -> None:
        """Serialize ControlMaster creation. First refresher creates the
        master; the others wait and then reuse the socket.

        Pre-flight: if `_master_ready` is set but the mux on disk is dead
        (e.g. ssh server died, network blip, ControlPersist expired between
        ticks), we wipe state and re-warm from scratch.

        Cancellation policy (issue #3): the master-warm path is wrapped in
        `asyncio.shield` so a `run_worker(exclusive=True)` cancellation
        from the next refresh tick can't tear it down mid-handshake (LRZ
        ProxyJump cold = ~10.5 s; tick interval = 5 s). The shield is
        bounded by the same `timeout` budget, so a permanently-stuck warm
        eventually surfaces as a timeout to the caller — without
        accumulating zombie children behind the per-host semaphore.
        """
        try:
            await asyncio.shield(self._warm_master_inner(timeout))
        except asyncio.TimeoutError:
            # If the inner timed out, mark the mux dead so the NEXT call
            # re-warms from scratch instead of trusting a half-baked state.
            self._mark_mux_dead()
            raise RuntimeError(
                f"ssh {self.host} master-warm timed out after {timeout}s"
            )

    async def run(self, argv: list[str], timeout: float = 15.0) -> str:
        # Per-host in-flight cap: at most one ssh probe in flight per host.
        # A hanging probe blocks the next dispatch (caller hits its own
        # `wait_for` and surfaces "stale") instead of stacking up.
        if self._inflight is None:
            self._inflight = asyncio.Semaphore(1)
        # Bound how long we'll wait FOR the semaphore by the same `timeout`
        # the caller wants; if the slot doesn't open in time, fail fast
        # rather than queueing behind a wedged predecessor forever.
        try:
            await asyncio.wait_for(self._inflight.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"ssh {self.host}: another probe is already in flight "
                f"(timeout={timeout}s waiting for semaphore)"
            )
        try:
            # Master-warm budget gets the larger of (timeout, 30s) to cover
            # the cold ProxyJump path on a slow link without forcing every
            # collector to bump its per-call timeout.
            await self._warm_master(timeout=max(timeout, 30.0))
            cmd = self._build_cmd(argv)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            cancelled = False
            # Catch ALL exceptions (including asyncio.CancelledError from
            # `run_worker(exclusive=True)` cancelling us, and TimeoutError
            # from the wait_for) — must kill+reap the subprocess in every
            # path or we leak ssh procs that hang on a slow remote and the
            # next tick spawns yet another one (the slurm-quota stack-up).
            #
            # NOTE on per-call TimeoutError: we do NOT mark the mux dead
            # here. A timed-out call could just mean the cluster is slow,
            # not that the mux is broken — marking dead would force a
            # re-warm on every tick under sustained load (10+ s warm vs
            # 15 s caller budget = perpetual warm-loop). The warm-master
            # path already detects + recovers a genuinely-dead mux on
            # the next call's pre-flight check.
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except BaseException:
                cancelled = True
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    # Shield the wait so a subsequent cancellation can't skip
                    # the reap and leave a zombie behind.
                    try:
                        await asyncio.shield(proc.wait())
                    except BaseException:
                        pass
                raise
            # Suppress rc=255 when the subprocess was killed by us: OpenSSH
            # exits 255 when the channel is torn down mid-flight (kill-by-
            # parent counts), and surfacing that as "ssh failed rc=255"
            # caused user-visible spurious errors during the cancel storm
            # (`run_worker(exclusive=True)` cancelling overlong ticks).
            if proc.returncode != 0 and not cancelled:
                raise RuntimeError(
                    f"ssh {self.host} {argv!r} failed rc={proc.returncode}: "
                    f"{err.decode(errors='replace')[:500]}"
                )
            return out.decode(errors="replace")
        finally:
            self._inflight.release()

    async def whoami(self) -> str:
        """Return the REMOTE username on this ssh host (cached).

        First call issues `whoami` against the remote; subsequent calls
        return the cached value. Cleared automatically when the master is
        torn down (so a different user could in principle reconnect, but
        in practice the ssh config pins the user per host).

        The cache survives across mux re-warms within a single
        SSHExecutor lifetime — `whoami` against the same host always
        returns the same string.
        """
        if self._remote_user is not None:
            return self._remote_user
        if self._whoami_lock is None:
            self._whoami_lock = asyncio.Lock()
        async with self._whoami_lock:
            if self._remote_user is not None:
                return self._remote_user
            # Use a short timeout — `whoami` is a no-op shell builtin once
            # the mux is warm. If the master isn't warm yet, .run will
            # warm it; that path is already bounded by max(timeout, 30s).
            try:
                out = await self.run(["whoami"], timeout=10.0)
            except RuntimeError:
                # If whoami fails (rare; would need the remote shell itself
                # to be broken), fall back to local $USER. Better than
                # raising — callers can still proceed and the resulting
                # `-u <user>` will produce a clean "Invalid user id" rather
                # than a hard crash.
                self._remote_user = os.environ.get("USER", "")
                return self._remote_user
            self._remote_user = out.strip()
            return self._remote_user

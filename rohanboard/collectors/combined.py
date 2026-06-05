"""Combined collector — one ssh channel per refresh tick.

Coalesces mount-fetch + df-fanout + quota + squeue + sacct + scontrol
into ONE remote shell invocation that runs sub-commands in parallel via
`&` + `wait`, then emits sectioned output the client splits.

Why: rohan's sshd `MaxSessions 10` (default) caps concurrent SSH channels
per asyncssh connection. The naive per-collector parallel pattern fired
30+ channels per refresh tick — the first ~10 succeeded, the rest got
silently rejected by sshd (asyncssh raises, gather drops the exception).
The Storage tab's auto-discovery branch alone produces 33 df channels
on rohan (25 SSD + 8 HDD per-node mounts).

This collector replaces that fanout with a single bash invocation that
parallelizes the sub-commands inside the remote shell and serializes the
output back through one channel. Wall time stays close to the slowest
sub-command (typically the multi-path df, ~300 ms warm).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

from ..exec import Executor


# Printable-ASCII sentinels — readable in logs and unlikely to clash with
# any output from the wrapped commands.
SECTION_DELIM = "===ROHANBOARD_SECTION:"
SECTION_END = "===ROHANBOARD_SECTION_END==="


@dataclass
class CombinedRaw:
    """Raw text per collector, indexed by section name. Empty string when
    the sub-command failed or produced no output (callers handle that the
    same as 'no data this tick')."""
    mounts: str = ""
    quota: str = ""
    df: str = ""
    squeue: str = ""
    # Bundle-2 B2.1: TWO sacct queries per tick — see fetch_combined
    # docstring. `sacct_self` is `-u $USER --starttime=<starttime_self>`
    # (long-history, few rows); `sacct_all` is `--starttime=<starttime_all>`
    # (short window, all users). Both run in parallel inside the same
    # bash invocation — still ONE ssh round-trip per tick.
    sacct_self: str = ""
    sacct_all: str = ""
    nodes: str = ""
    dssusrinfo: str = ""
    # Bundle-1 Sub-fix-3: REMOTE whoami piggybacks on the combined tick
    # so the first useful frame is one ssh round-trip away, not two.
    # Pre-fix `on_mount` did a serial `resolve_whoami` (~10 s on cold
    # LRZ ProxyJump) BEFORE kicking the first refresh, doubling the
    # cold-start time. Stripped of any trailing newline by callers.
    whoami: str = ""


# Section key → list of shell command strings to run in the remote bash.
# Each value is a SHELL FRAGMENT, not an argv list — callers are
# responsible for `shlex.quote`ing per-arg as needed. df globs (e.g.
# `/cluster/*`) MUST be left unquoted so the remote shell expands them.
SectionMap = dict[str, str]


# Per-sub-command wall-clock cap (seconds). Each section body runs under
# `timeout`, so ONE slow/hung command (a wedged NFS quota/df, a stuck
# scontrol against a busy controller) degrades to an empty section instead
# of blocking the shared `wait` and wedging the WHOLE tick. Must stay BELOW
# the App's per-tick budget: rohan's master refresh interval is 5 s and
# `_refresh_all` is `@work(exclusive=True)`, so a tick not done in ~5 s is
# cancelled by the next one — capping each command at 4 s lets the fast
# sections still dump within that window while a hang is killed (LRZ's 15 s
# interval has even more slack). Warm commands run in <0.1 s and cold
# autofs/scontrol well under 4 s on both rohan + LRZ (measured 2026-06-05),
# so legit commands are never caught. `timeout` is GNU coreutils, confirmed
# present on both login nodes (rohan 8.30 / LRZ 8.32). This is the
# defense-in-depth backstop behind the `quota -l` fix: `-l` removes today's
# known hanger, this prevents any FUTURE one from re-freezing the board.
PER_CMD_TIMEOUT_SECS = 4


def build_script(sections: SectionMap, cmd_timeout: int = PER_CMD_TIMEOUT_SECS) -> str:
    """Build the bash script that runs every section in parallel + emits
    sectioned output. Pure function — no I/O. Tested in isolation.

    Each section body is wrapped in `timeout -k 1 <cmd_timeout> bash -c
    <body>` so a single hanging command can't hold the shared `wait`. The
    inner `bash -c` re-parses the body, so globs (`df /cluster/*`),
    shlex-quoted args (squeue/sacct), and the dssusrinfo subshell all expand
    exactly as they did unwrapped. `timeout` exits 124 on expiry, which the
    existing `|| true` absorbs so `wait` always sees success.
    """
    parallel_lines = []
    dump_lines = []
    for name in sections:
        # Each sub-shell writes to a tempfile; stderr is dropped (we only
        # care about stdout for parsing). `|| true` keeps `wait` from
        # seeing a nonzero exit code from any one sub-command — we want
        # all sections collected even if some failed (incl. timeout's 124).
        # `timeout -k 1 N`: SIGTERM at N s, SIGKILL 1 s later if ignored.
        body = f"timeout -k 1 {cmd_timeout} bash -c {shlex.quote(sections[name])}"
        parallel_lines.append(
            f'( {body} > "$T/{name}" 2>/dev/null || true ) &'
        )
        dump_lines.append(
            f'echo "{SECTION_DELIM}{name}"; '
            f'cat "$T/{name}" 2>/dev/null || true; '
            f'echo "{SECTION_END}"'
        )
    return (
        "set -u\n"
        "T=$(mktemp -d -t rohanboard.XXXXXX)\n"
        "trap 'rm -rf \"$T\"' EXIT\n"
        "\n"
        + "\n".join(parallel_lines)
        + "\nwait\n\n"
        + "\n".join(dump_lines)
        + "\n"
    )


def parse_combined(text: str) -> CombinedRaw:
    """Split multi-section output into per-section text.

    Tolerates missing sentinels (returns whatever was collected up to
    that point). Strips the trailing newline injected by `echo`."""
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(SECTION_DELIM):
            if current_key is not None:
                sections[current_key] = current_lines
            current_key = line[len(SECTION_DELIM):].strip()
            current_lines = []
        elif line == SECTION_END:
            if current_key is not None:
                sections[current_key] = current_lines
                current_key = None
                current_lines = []
        elif current_key is not None:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = current_lines
    return CombinedRaw(
        mounts="\n".join(sections.get("mounts", [])),
        quota="\n".join(sections.get("quota", [])),
        df="\n".join(sections.get("df", [])),
        squeue="\n".join(sections.get("squeue", [])),
        sacct_self="\n".join(sections.get("sacct_self", [])),
        sacct_all="\n".join(sections.get("sacct_all", [])),
        nodes="\n".join(sections.get("nodes", [])),
        dssusrinfo="\n".join(sections.get("dssusrinfo", [])),
        whoami="\n".join(sections.get("whoami", [])),
    )


async def fetch_combined(
    executor: Executor,
    *,
    quota_user: str | None,
    df_explicit_paths: list[str],
    df_prefix_globs: list[str],
    squeue_format: str,
    squeue_users: list[str] | None,
    sacct_format: str,
    sacct_starttime_self: str,
    sacct_starttime_all: str,
    sacct_user: str | None,
    dssusrinfo_subcommands: list[str] | None = None,
    timeout: float = 25.0,
) -> CombinedRaw:
    """Run the combined script in one ssh channel and parse the result.

    Sub-commands skipped when their config knob is empty — e.g. no
    `quota_user` → quota section is empty. Failures inside individual
    sub-commands are absorbed (`|| true` in the script + empty section
    returned), so a single broken command doesn't fail the whole tick.
    """
    sections: SectionMap = {
        "mounts": "cat /proc/mounts",
        "nodes": "scontrol show node --all",
        # Bundle-1 Sub-fix-3: REMOTE whoami piggybacks on every tick so
        # the cold-start window doesn't double up (pre-fix `on_mount`
        # ran a serial `resolve_whoami` BEFORE the first refresh — two
        # ssh round-trips in series on a cold ProxyJump).
        "whoami": "whoami",
    }
    if quota_user:
        # `-l` / --local-only: query ONLY local filesystems, skipping NFS.
        # Plain `quota -u <user>` probes EVERY mounted fs — including NFS —
        # by issuing an rquotad RPC per NFS server. rohan's login node has
        # many NFS mounts (/canis/*, /hamsa, /sirion, /menegroth, …) and at
        # least one server's rquotad never answers, so the call hangs >125 s.
        # Because the quota sub-command runs inside this script's `wait`, that
        # hang blocks the WHOLE combined tick; the App's 5 s exclusive refresh
        # interval then cancels the in-flight fetch, so every post-cold-start
        # tick is lost and the board freezes on its first (whoami-less, hence
        # quota-less) snapshot — home never appears in Storage. rohan's home
        # (/rhome) is a LOCAL fs, so `-l` returns its quota (incl. the 300 GiB
        # soft limit) in ~30 ms. (2026-06-05 rohan home-missing incident.)
        sections["quota"] = f"quota -u {shlex.quote(quota_user)} -l"
    # df accepts a mix of explicit paths and shell globs; the remote
    # bash expands the globs. Quote explicit paths to handle spaces;
    # globs are left raw.
    df_args = [shlex.quote(p) for p in df_explicit_paths] + list(df_prefix_globs)
    if df_args:
        sections["df"] = f"df -B1 {' '.join(df_args)}"
    # squeue / sacct: optional user filter — same logic as the existing
    # fetch_jobs / fetch_recent_jobs (callers resolve "self" → $USER).
    squeue_argv = ["squeue", "-h", "-O", squeue_format]
    if squeue_users:
        squeue_argv += ["-u", ",".join(squeue_users)]
    sections["squeue"] = " ".join(shlex.quote(a) for a in squeue_argv)

    # Bundle-2 B2.1: two sacct queries per tick, both inside the same
    # bash pipeline so they run in parallel (server-side) and still cost
    # one ssh round-trip. `sacct_self` requires a resolved user; on cold
    # start (sacct_user=None until whoami lands) the self section is
    # skipped — next tick picks it up. `sacct_all` has no -u filter and
    # always runs.
    if sacct_user:
        sacct_self_argv = [
            "sacct", "-X", "-P", "-n",
            f"--starttime={sacct_starttime_self}",
            "-u", sacct_user,
            "-o", sacct_format,
        ]
        sections["sacct_self"] = " ".join(shlex.quote(a) for a in sacct_self_argv)
    sacct_all_argv = [
        "sacct", "-X", "-P", "-n",
        f"--starttime={sacct_starttime_all}",
        "-a",
        "-o", sacct_format,
    ]
    sections["sacct_all"] = " ".join(shlex.quote(a) for a in sacct_all_argv)

    # dssusrinfo: a list of subcommands (e.g. ["dsshome", "container_usage"]).
    # We concatenate them inside the section so a single dssusrinfo block
    # carries all the data the parsers need. Each subcommand prints its own
    # star-bordered block, so the parsers can split by the in-block header.
    if dssusrinfo_subcommands:
        # Dedupe while preserving order.
        seen_sub: set[str] = set()
        ordered_subs: list[str] = []
        for sub in dssusrinfo_subcommands:
            if sub not in seen_sub:
                seen_sub.add(sub)
                ordered_subs.append(sub)
        # Wrap in an inner subshell so the outer `> "$T/dssusrinfo"`
        # redirect captures BOTH commands' stdout — `( a; b ) > FILE`
        # redirects the whole subshell, whereas `a; b > FILE` (which is
        # what build_script's template produces if we hand it a raw `;`)
        # only redirects the LAST command. Bug observed live 2026-05-10
        # when the home block was silently dropped.
        sections["dssusrinfo"] = (
            "( " + "; ".join(f"dssusrinfo {shlex.quote(sub)}" for sub in ordered_subs) + " )"
        )

    script = build_script(sections)
    rc, stdout, stderr = await executor.run(["bash", "-c", script], timeout=timeout)
    if rc != 0:
        # `set -u` fires on undefined vars; otherwise rc==0 because every
        # sub-command is `|| true`. A nonzero rc means the wrapper script
        # itself failed (e.g. mktemp on a full /tmp).
        raise RuntimeError(
            f"combined collector failed rc={rc}: stderr={stderr[:200]!r}"
        )
    return parse_combined(stdout)

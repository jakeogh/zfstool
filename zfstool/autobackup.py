#!/usr/bin/env python3

"""zfs-autobackup jobs, run under one lock with a log per run, checked for
what actually arrived, and sent only into a target this machine has claimed.

What a job backs up is zfs-autobackup's choice: autobackup:<job> on the
source datasets, snapshots named <job>-<time>, each landing at the job's
target followed by the dataset's path. A job's target root carries
zfstool:source, the pools it holds by name and GUID. A run sends only when
that claim names every pool the job selects with the GUID that pool has
here; a reinstalled machine has a new pool GUID, and another machine given
the same name has other pools, so either is refused rather than sent over
a backup it does not own. Only `claim` writes the claim, and only after
checking that everything already in the root shares a snapshot with this
machine.

The outcome of the last run or check is kept in STATE for the shell prompt:
a line with its time, a line with "ok" or a summary, then one line per
problem.
"""

import fcntl
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import BinaryIO

import click
from asserttool import ic
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tvic
from eprint import eprint

PREFIX = "autobackup:"
CLAIM_PROPERTY = "zfstool:source"
CONFIG = Path("/etc/zfstool/autobackup.toml")
LOCK = Path("/run/lock/zfstool-autobackup.lock")
LOG_DIR = Path("/var/log/zfstool-autobackup")
LOG_NAME = re.compile(r"^(\d{8}-\d{6})\.log$")
STATE = Path("/var/lib/zfstool/autobackup.state")
DEFAULT_KEEP_LOGS = "24,1h1d,1d30d,30d1y"
JOB_KEYS = frozenset({"target", "ssh_target", "strip_path", "keep_source", "keep_target", "extra"})
# would let a send destroy, overwrite or roll back what a target holds
DESTRUCTIVE_OPTIONS = frozenset({"--force", "--destroy-incompatible", "--destroy-missing", "--rollback"})
# zfstool finds a job's snapshots by zfs-autobackup's default <job>-<time>
NAMING_OPTIONS = frozenset({"--snapshot-format", "--property-format", "--hold-format"})
# Passed on every run. A received dataset is never mounted on the target
# host, where its mountpoint would land on that host's own tree, and a
# dataset that is itself a received backup is never sent on, which keeps
# what is selected here the same as what the checks below look at.
ALWAYS_OPTIONS = ("--clear-mountpoint", "--exclude-received")
CRON_PATHS = (
    Path("/etc/crontab"),
    Path("/etc/cron.d"),
    Path("/etc/cron.hourly"),
    Path("/etc/cron.daily"),
    Path("/etc/cron.weekly"),
    Path("/etc/cron.monthly"),
    Path("/var/spool/cron"),
)
TIME_UNITS = {"s": 1, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}


class TargetError(RuntimeError):
    """A command on the target host, or here, failed: unreachable, refused,
    or the dataset is not there."""

    def __init__(self, argv: list[str], stderr: str) -> None:
        self.stderr = stderr.strip()
        last = self.stderr.splitlines()[-1] if self.stderr else "no output"
        super().__init__(f"{shlex.join(argv)}: {last}")


@dataclass(frozen=True)
class Job:
    name: str
    target: str
    ssh_target: None | str = None
    strip_path: int = 0
    keep_source: None | str = None
    keep_target: None | str = None
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    jobs: dict[str, Job]
    keep_logs: str


@dataclass(frozen=True)
class Snapshot:
    name: str
    guid: str
    creation: int
    userrefs: int
    used: int


@dataclass(frozen=True)
class Result:
    dataset: str
    backup_name: str
    status: str
    target: None | str
    target_pool: None | str
    newest_common: None | str
    age: None | int
    newest_point: None | str
    oldest_point: None | str
    point_count: int
    pending: int
    target_mounted: bool
    target_readonly: bool
    recent_points: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Problem:
    job: str
    kind: str
    detail: str


# --- configuration ---------------------------------------------------------


def parse_schedule(text: str) -> tuple[int, list[tuple[int, int]]]:
    """zfs-autobackup's thinning schedule: a bare number keeps that many of
    the newest, and each <period><unit><ttl><unit> keeps the oldest one in
    every period for as long as it is younger than ttl. Units: s, min, h,
    d, w, m (30 days), y (365 days)."""
    newest = 0
    rules: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            newest = int(part)
            continue
        match = re.fullmatch(r"(\d+)([a-z]+)(\d+)([a-z]+)", part)
        if match is None or match[2] not in TIME_UNITS or match[4] not in TIME_UNITS:
            raise click.ClickException(f"thinning schedule {text!r}: cannot read {part!r}")
        rules.append((int(match[1]) * TIME_UNITS[match[2]], int(match[3]) * TIME_UNITS[match[4]]))
    return newest, rules


def thin(items: list[tuple[Path, int]], schedule: str, now: int) -> set[Path]:
    """Which of items (each with its time) the schedule keeps."""
    newest, rules = parse_schedule(schedule)
    ordered = sorted(items, key=lambda item: item[1])
    keep = {item for item, _ in ordered[-newest:]} if newest else set()
    seen: dict[int, set[int]] = {period: set() for period, _ in rules}
    for item, when in ordered:
        for period, ttl in rules:
            if now - when <= ttl and (block := when // period) not in seen[period]:
                seen[period].add(block)
                keep.add(item)
    return keep


def load_config() -> Config:
    path = CONFIG
    if not path.is_file():
        return Config(jobs={}, keep_logs=DEFAULT_KEEP_LOGS)
    parsed = tomllib.loads(path.read_text(encoding="utf8"))
    unknown = set(parsed) - {"jobs", "keep_logs"}
    if unknown:
        raise click.ClickException(f"{path}: unknown keys {sorted(unknown)}")
    keep_logs = parsed.get("keep_logs", DEFAULT_KEEP_LOGS)
    parse_schedule(keep_logs)
    jobs: dict[str, Job] = {}
    for name, entry in parsed.get("jobs", {}).items():
        unknown = set(entry) - JOB_KEYS
        if unknown:
            raise click.ClickException(f"{path}: job {name}: unknown keys {sorted(unknown)}")
        if "target" not in entry:
            raise click.ClickException(f"{path}: job {name} has no target")
        extra = tuple(entry.get("extra", []))
        options = {_arg.split("=", 1)[0] for _arg in extra}
        if options & DESTRUCTIVE_OPTIONS:
            raise click.ClickException(
                f"{path}: job {name}: {sorted(options & DESTRUCTIVE_OPTIONS)} would let a "
                f"send destroy, overwrite or roll back what the target holds; zfstool refuses them"
            )
        if options & NAMING_OPTIONS:
            raise click.ClickException(
                f"{path}: job {name}: {sorted(options & NAMING_OPTIONS)}: zfstool finds a "
                f"job's snapshots and holds by zfs-autobackup's default names"
            )
        if "/" not in entry["target"]:
            raise click.ClickException(f"{path}: job {name}: the target is a dataset below a pool, not a pool")
        jobs[name] = Job(
            name=name,
            target=entry["target"],
            ssh_target=entry.get("ssh_target"),
            strip_path=entry.get("strip_path", 0),
            keep_source=entry.get("keep_source"),
            keep_target=entry.get("keep_target"),
            extra=extra,
        )
    return Config(jobs=jobs, keep_logs=keep_logs)


# --- commands on either side -------------------------------------------------


def on_target(ssh_target: None | str, *argv: str) -> str:
    """argv where the target is: over ssh, never stopping to ask for a
    password, or here."""
    command = list(argv)
    if ssh_target:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh_target, shlex.join(argv)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise TargetError(command, result.stderr)
    return result.stdout


def snapshot_index(root: str, ssh_target: None | str) -> dict[str, list[Snapshot]]:
    output = on_target(
        ssh_target, "zfs", "list", "-Hp", "-t", "snapshot", "-o", "name,guid,creation,userrefs,used", "-r", root
    )
    index: dict[str, list[Snapshot]] = {}
    for line in output.splitlines():
        if not line:
            continue
        name, guid, creation, userrefs, used = line.split("\t")
        dataset, _, snapshot = name.partition("@")
        index.setdefault(dataset, []).append(
            Snapshot(snapshot, guid, int(creation), 0 if userrefs == "-" else int(userrefs), int(used))
        )
    for rows in index.values():
        rows.sort(key=lambda _row: _row.creation)
    return index


def dataset_state(root: str, ssh_target: None | str) -> dict[str, tuple[bool, bool]]:
    output = on_target(ssh_target, "zfs", "list", "-H", "-o", "name,mounted,readonly", "-t", "filesystem,volume", "-r", root)
    state: dict[str, tuple[bool, bool]] = {}
    for line in output.splitlines():
        if line:
            name, mounted, readonly = line.split("\t")
            state[name] = (mounted == "yes", readonly == "on")
    return state


def hold_tags(snapshots: list[str], ssh_target: None | str) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for offset in range(0, len(snapshots), 50):
        for line in on_target(ssh_target, "zfs", "holds", "-H", *snapshots[offset : offset + 50]).splitlines():
            if line:
                fields = line.split("\t")
                tags.setdefault(fields[0], []).append(fields[1])
    return tags


def scrub_line(pool: str, ssh_target: None | str) -> str:
    for line in on_target(ssh_target, "zpool", "status", pool).splitlines():
        if line.strip().startswith("scan:"):
            return line.strip()
    return "scan: no scrub recorded"


# --- what is selected --------------------------------------------------------


def property_map() -> dict[str, dict[str, tuple[str, str]]]:
    output = subprocess.run(
        ["zfs", "get", "-H", "-o", "name,property,value,source", "-t", "filesystem,volume", "all"],
        capture_output=True, text=True, check=True,
    ).stdout
    mapping: dict[str, dict[str, tuple[str, str]]] = {}
    for line in output.splitlines():
        if not line:
            continue
        dataset, prop, value, source = line.split("\t")
        if prop.startswith(PREFIX):
            mapping.setdefault(dataset, {})[prop[len(PREFIX) :]] = (value, source)
    return mapping


def is_received(mapping: dict[str, dict[str, tuple[str, str]]], dataset: str, name: str) -> bool:
    source = mapping[dataset][name][1]
    if source == "received":
        return True
    if source.startswith("inherited from "):
        origin = mapping.get(source.removeprefix("inherited from "), {}).get(name)
        return origin is not None and origin[1] == "received"
    return False


def is_selected(value: str, source: str) -> bool:
    if value == "true":
        return True
    if value == "child":  # a local "child" leaves the dataset itself out
        return source != "local"
    return False


def selected_pairs(mapping: dict[str, dict[str, tuple[str, str]]]) -> list[tuple[str, str]]:
    """(dataset, job) for every dataset a job here would send: a selection
    that arrived with a received backup belongs to the machine it came from."""
    return [
        (dataset, name)
        for dataset in sorted(mapping)
        for name, (value, source) in sorted(mapping[dataset].items())
        if is_selected(value, source) and not is_received(mapping, dataset, name)
    ]


def pool_guids(pools: list[str] | None = None) -> dict[str, str]:
    output = subprocess.run(
        ["zpool", "list", "-H", "-o", "name,guid", *(pools or [])], capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("\t") for line in output.splitlines() if line)


def target_dataset(dataset: str, job: Job) -> str:
    remainder = dataset.split("/")[job.strip_path :]
    if not remainder:
        raise click.ClickException(f"job {job.name}: strip_path {job.strip_path} strips all of {dataset}")
    return "/".join([job.target, *remainder])


# --- claims ------------------------------------------------------------------


def parse_claim(value: str) -> dict[str, str]:
    if value in ("", "-"):
        return {}
    return dict(_pair.split("=", 1) for _pair in value.split(","))


def format_claim(claim: dict[str, str]) -> str:
    return ",".join(f"{_pool}={_guid}" for _pool, _guid in sorted(claim.items()))


def read_claim(job: Job) -> None | dict[str, str]:
    """The claim on the job's root; None when the root does not exist."""
    try:
        value = on_target(job.ssh_target, "zfs", "get", "-H", "-o", "value", CLAIM_PROPERTY, job.target)
    except TargetError as error:
        if "dataset does not exist" in error.stderr:
            return None
        raise
    return parse_claim(value.strip())


def claim_refusal(job: Job, pools: dict[str, str], claim: None | dict[str, str]) -> None | Problem:
    if claim is None:
        return Problem(job.name, "unclaimed", f"{job.target} does not exist; zfstool autobackup claim {job.name} creates it")
    if not claim:
        return Problem(job.name, "unclaimed", f"{job.target} carries no claim; zfstool autobackup claim {job.name} checks and claims it")
    for pool, guid in sorted(pools.items()):
        if pool not in claim:
            return Problem(
                job.name, "unclaimed",
                f"{job.target} is claimed for {format_claim(claim)}, without pool {pool}; zfstool "
                f"autobackup claim {job.name} adds it when every claimed pool is on this machine",
            )
        if claim[pool] != guid:
            return Problem(
                job.name, "foreign",
                f"{job.target} holds pool {pool} with GUID {claim[pool]}, and this machine's {pool} is "
                f"{guid}: another machine's backup, or this one reinstalled",
            )
    return None


# --- verification ------------------------------------------------------------


def stale_threshold(creations: list[int], max_age: None | int, stale_factor: int) -> None | int:
    if max_age:
        return max_age
    intervals = [_later - _earlier for _earlier, _later in zip(creations[-11:], creations[-10:], strict=False)]
    if len(intervals) < 2:
        return None
    return int(median(intervals)) * stale_factor


def verify(pairs: list[tuple[str, str]], job: Job, *, max_age: None | int = None, stale_factor: int = 3) -> list[Result]:
    """Each selected dataset against its copy under the job's target, by
    snapshot GUID. Only snapshots named <job>-... count on the source: another
    job's or a hand-made one is not what this job is late in sending."""
    own = f"{job.name}-"
    source_index: dict[str, list[Snapshot]] = {}
    for pool in sorted({_dataset.split("/")[0] for _dataset, _ in pairs}):
        source_index.update(snapshot_index(pool, None))
    held = [f"{_ds}@{_s.name}" for _ds, _rows in source_index.items() for _s in _rows if _s.userrefs]
    holds = hold_tags(held, None) if held else {}
    target_pool = job.target.split("/")[0]
    target_index = snapshot_index(target_pool, job.ssh_target)
    target_state = dataset_state(target_pool, job.ssh_target)
    hold_tag = f"zfs_autobackup:{job.name}"
    now = int(time.time())

    results: list[Result] = []
    for dataset, _ in pairs:
        target = target_dataset(dataset, job)
        mounted, readonly = target_state.get(target, (False, True))
        source_rows = [_s for _s in source_index.get(dataset, []) if _s.name.startswith(own)]
        target_rows = target_index.get(target, [])
        base = dict(
            dataset=dataset, backup_name=job.name, target=target, target_pool=target_pool,
            target_mounted=mounted, target_readonly=readonly,
        )
        if not target_rows:
            results.append(Result(
                **base, status="missing", newest_common=None, age=None, newest_point=None,
                oldest_point=None, point_count=0, pending=len(source_rows), recent_points=(),
            ))
            continue
        points = tuple((_s.name, _s.creation) for _s in target_rows)
        target_guids = {_s.guid for _s in target_rows}
        common = [_s for _s in source_rows if _s.guid in target_guids]
        if not common:
            results.append(Result(
                **base, status="diverged", newest_common=None, age=None, newest_point=target_rows[-1].name,
                oldest_point=target_rows[0].name, point_count=len(target_rows), pending=len(source_rows),
                recent_points=points,
            ))
            continue
        newest = common[-1]
        age = now - newest.creation
        pending = sum(1 for _s in source_rows if _s.creation > newest.creation)
        orphaned = [
            _s.name for _s in source_rows
            if _s.name != newest.name and hold_tag in holds.get(f"{dataset}@{_s.name}", [])
        ]
        threshold = stale_threshold([_s.creation for _s in source_rows], max_age, stale_factor)
        if orphaned:
            status = "hold-orphan"
        elif pending and threshold and age > threshold:
            status = "stale"
        else:
            status = "ok"
        results.append(Result(
            **base, status=status, newest_common=newest.name, age=age, newest_point=target_rows[-1].name,
            oldest_point=target_rows[0].name, point_count=len(target_rows), pending=pending, recent_points=points,
        ))
    return results


def target_unsafe(result: Result) -> bool:
    return bool(result.target) and (result.target_mounted or not result.target_readonly)


def target_flags(result: Result) -> str:
    if not result.target:
        return "-"
    return ",".join(["ro" if result.target_readonly else "rw"] + (["mounted"] if result.target_mounted else []))


def check(config: Config, mapping: dict[str, dict[str, tuple[str, str]]]) -> tuple[list[Problem], list[Result]]:
    """Every job's claim and every selected dataset's backup."""
    pairs = selected_pairs(mapping)
    problems: list[Problem] = []
    results: list[Result] = []
    for name in sorted({_name for _, _name in pairs} - set(config.jobs)):
        datasets = [_ds for _ds, _name in pairs if _name == name]
        problems.append(Problem(name, "no job", f"selected by {', '.join(datasets)}, and {CONFIG} has no job {name}"))
    for name, job in sorted(config.jobs.items()):
        job_pairs = [_pair for _pair in pairs if _pair[1] == name]
        if not job_pairs:
            problems.append(Problem(name, "selects nothing", f"no dataset here has {PREFIX}{name} set"))
            continue
        try:
            refusal = claim_refusal(job, pool_guids(sorted({_ds.split("/")[0] for _ds, _ in job_pairs})), read_claim(job))
            if refusal:
                problems.append(refusal)
                continue
            job_results = verify(job_pairs, job)
        except TargetError as error:
            problems.append(Problem(name, "unreachable", str(error)))
            continue
        results += job_results
        for result in job_results:
            if result.status != "ok":
                problems.append(Problem(name, result.status, f"{result.dataset} -> {result.target}"))
            if target_unsafe(result):
                problems.append(Problem(name, "unsafe", f"{result.target} is {target_flags(result)}"))
    return problems, results


def write_state(problems: list[Problem]) -> None:
    kinds = sorted({f"{_p.job} {_p.kind}" for _p in problems})
    summary = "ok" if not problems else ", ".join(kinds)
    if len(summary) > 60:
        summary = summary[:57] + "..."
    lines = [str(int(time.time())), summary] + [f"{_p.job}\t{_p.kind}\t{_p.detail}" for _p in problems]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    staged = STATE.with_name(STATE.name + ".new")
    staged.write_text("\n".join(lines) + "\n", encoding="utf8")
    staged.replace(STATE)


def report(problems: list[Problem]) -> None:
    for problem in problems:
        print(f"{problem.job}\t{problem.kind}\t{problem.detail}", flush=True)
    if not problems:
        print("ok", flush=True)


# --- running -----------------------------------------------------------------


def job_argv(job: Job, *, test: bool, interactive: bool) -> list[str]:
    argv = ["zfs-autobackup", *ALWAYS_OPTIONS]
    if job.ssh_target:
        argv += ["--ssh-target", job.ssh_target]
    if job.strip_path:
        argv += ["--strip-path", str(job.strip_path)]
    if job.keep_source:
        argv += ["--keep-source", job.keep_source]
    if job.keep_target:
        argv += ["--keep-target", job.keep_target]
    # progress redraws one line with carriage returns: for a terminal only
    argv += [_arg for _arg in job.extra if interactive or _arg != "--progress"]
    if test:
        argv.append("--test")
    return argv + [job.name, job.target]


def stream(argv: list[str], log: None | BinaryIO, show: bool) -> int:
    """Run argv with its output going to the log as it comes, and to the
    terminal as well when there is one."""
    if log is not None:
        log.write(f"$ {shlex.join(argv)}\n".encode())
        log.flush()
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=os.environ | {"PYTHONUNBUFFERED": "1"}
    )
    assert process.stdout is not None
    while chunk := os.read(process.stdout.fileno(), 65536):
        if log is not None:
            log.write(chunk)
            log.flush()
        if show:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    return process.wait()


def take_lock() -> None | int:
    """The run lock, held until the process exits; None when an earlier run
    still holds it. A first send of a whole machine outlasts the hour
    between runs, and two zfs-autobackup runs of one job snapshot, send and
    thin over each other."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
    return descriptor


def prune_logs(schedule: str, current: Path) -> None:
    logs = []
    for path in LOG_DIR.iterdir():
        match = LOG_NAME.match(path.name)
        if match and path != current:
            logs.append((path, int(time.mktime(time.strptime(match[1], "%Y%m%d-%H%M%S")))))
    keep = thin(logs, schedule, int(time.time()))
    for path, _ in logs:
        if path not in keep:
            path.unlink()


def format_age(age: None | int) -> str:
    if age is None:
        return "-"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def table_widths(rows: list[list[str]]) -> list[int]:
    return [max(len(_row[_i]) for _row in rows) for _i in range(len(rows[0]))]


def table_line(cells: list[str], widths: list[int]) -> str:
    return "  ".join(_cell.ljust(widths[_i]) if _i == 0 else _cell.rjust(widths[_i]) for _i, _cell in enumerate(cells))


def schedule_lines() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in CRON_PATHS:
        if not path.exists():
            continue
        for candidate in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if not candidate.is_file():
                continue
            for index, line in enumerate(candidate.read_text(encoding="utf8", errors="replace").splitlines(), start=1):
                if not line.strip().startswith("#") and "autobackup" in line:
                    hits.append((candidate, index, line.strip()))
    return hits


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if value < 1024 or unit == "P":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}P"


def range_reclaim(dataset: str, first: str, last: str, ssh_target: None | str) -> int:
    for line in on_target(ssh_target, "zfs", "destroy", "-nvp", f"{dataset}@{first}%{last}").splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == "reclaim":
            return int(fields[1])
    return 0


def require_root(what: str) -> None:
    if os.geteuid() != 0:
        raise click.ClickException(f"{what} needs root")


# --- cli -----------------------------------------------------------------------


# not named autobackup: the package re-exports it, and that name would hide
# this module as an attribute of the package
@click.group("autobackup", no_args_is_help=True, cls=AHGroup)
@click_add_options(click_global_options)
@click.pass_context
def autobackup_cli(ctx: click.Context, *, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)


@autobackup_cli.command()
@click.option("--backup-name", type=str)
@click.option("--test", is_flag=True, help="zfs-autobackup --test: show what would be done, change nothing")
@click_add_options(click_global_options)
@click.pass_context
def run(ctx: click.Context, *, backup_name: None | str, test: bool, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    """Run the jobs, each only into a target it has claimed, under one lock,
    logging to LOG_DIR; then check every job and record the outcome for the
    prompt. With no jobs configured there is nothing to do."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    if not test:
        require_root("run")
    config = load_config()
    if not config.jobs:
        return
    if backup_name and backup_name not in config.jobs:
        raise click.ClickException(f"{CONFIG} has no job {backup_name}")
    names = [backup_name] if backup_name else sorted(config.jobs)
    interactive = sys.stdout.isatty()

    if test:
        failures = send(config, names, property_map(), log=None, log_path=None, test=True, interactive=interactive)
        report(failures)
        ctx.exit(1 if failures else 0)

    lock = take_lock()
    if lock is None:
        # From cron this is the hourly run finding the last one still sending,
        # which is expected of a first send; a run that never finishes stops
        # updating STATE, and the prompt reports the outcome as overdue.
        if interactive:
            raise click.ClickException(f"an earlier run still holds {LOCK}: {LOCK.read_text(encoding='utf8').strip()}")
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / time.strftime("%Y%m%d-%H%M%S.log")
        prune_logs(config.keep_logs, log_path)
        with log_path.open("ab") as log:
            mapping = property_map()
            failures = send(config, names, mapping, log=log, log_path=log_path, test=False, interactive=interactive)
            checked, _ = check(config, mapping)
            # a failure and the check's view of the same job both stand
            reported = {(_f.job, _f.kind, _f.detail) for _f in failures}
            problems = failures + [_p for _p in checked if (_p.job, _p.kind, _p.detail) not in reported]
            write_state(problems)
            log.write(("\n".join(f"{_p.job}\t{_p.kind}\t{_p.detail}" for _p in problems) or "ok").encode() + b"\n")
    finally:
        os.close(lock)
    if interactive:
        report(problems)
    ctx.exit(1 if problems else 0)


def send(
    config: Config, names: list[str], mapping: dict[str, dict[str, tuple[str, str]]], *,
    log: None | BinaryIO, log_path: None | Path, test: bool, interactive: bool,
) -> list[Problem]:
    """Each named job, sent only into a target it has claimed."""
    pairs = selected_pairs(mapping)
    failures: list[Problem] = []
    for name in names:
        job = config.jobs[name]
        job_pools = sorted({_ds.split("/")[0] for _ds, _name in pairs if _name == name})
        if not job_pools:
            failures.append(Problem(name, "selects nothing", f"no dataset here has {PREFIX}{name} set"))
            continue
        try:
            refusal = claim_refusal(job, pool_guids(job_pools), read_claim(job))
        except TargetError as error:
            failures.append(Problem(name, "unreachable", str(error)))
            continue
        if refusal:
            failures.append(refusal)
            continue
        code = stream(job_argv(job, test=test, interactive=interactive), log, show=interactive or test)
        if code:
            failures.append(Problem(name, "failed", f"zfs-autobackup exited {code}" + (f"; {log_path}" if log_path else "")))
    return failures


@autobackup_cli.command("check")
@click_add_options(click_global_options)
@click.pass_context
def check_command(ctx: click.Context, *, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    """Check every job's claim and every selected dataset's backup without
    sending anything; as root, record the outcome for the prompt."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    config = load_config()
    if not config.jobs:
        return
    problems, _ = check(config, property_map())
    if os.geteuid() == 0:
        write_state(problems)
    report(problems)
    ctx.exit(1 if problems else 0)


@autobackup_cli.command()
@click.argument("backup_name")
@click_add_options(click_global_options)
@click.pass_context
def claim(ctx: click.Context, *, backup_name: str, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    """Claim the job's target root for this machine's pools, creating it
    below its existing parent when it is not there. An existing root is
    claimed only when it shows it is this machine's: every pool an existing
    claim names is here with the same GUID, and, for a root with no claim,
    every dataset in it is the copy of one selected here and shares a
    snapshot with it."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    require_root("claim")
    config = load_config()
    job = config.jobs.get(backup_name)
    if job is None:
        raise click.ClickException(f"{CONFIG} has no job {backup_name}")
    pairs = [_pair for _pair in selected_pairs(property_map()) if _pair[1] == backup_name]
    if not pairs:
        raise click.ClickException(f"no dataset here has {PREFIX}{backup_name} set")
    pools = pool_guids(sorted({_ds.split("/")[0] for _ds, _ in pairs}))
    current = read_claim(job)

    if current is None:
        parent = job.target.rsplit("/", 1)[0]
        try:
            on_target(job.ssh_target, "zfs", "list", "-H", "-o", "name", parent)
        except TargetError as error:
            raise click.ClickException(f"{parent}, where {job.target} belongs, is not usable: {error}") from None
        on_target(
            job.ssh_target, "zfs", "create", "-o", "canmount=off", "-o", "readonly=on",
            "-o", f"{CLAIM_PROPERTY}={format_claim(pools)}", job.target,
        )
        print(f"created {job.target}, claimed for {format_claim(pools)}", flush=True)
        return

    if current:
        here = pool_guids()
        foreign = [f"{_p}={_g}" for _p, _g in sorted(current.items()) if here.get(_p) != _g]
        if foreign:
            raise click.ClickException(
                f"{job.target} is claimed for {format_claim(current)}; {', '.join(foreign)} is not "
                f"a pool on this machine: another machine's backup, or this one reinstalled"
            )
    else:
        expected = {target_dataset(_ds, job) for _ds, _ in pairs}
        allowed = expected | {
            "/".join(_t.split("/")[:_n]) for _t in expected for _n in range(len(job.target.split("/")) + 1, len(_t.split("/")))
        }
        present = set(dataset_state(job.target, job.ssh_target)) - {job.target}
        stray = sorted(present - allowed)
        if stray:
            raise click.ClickException(
                f"{job.target} carries no claim and holds datasets that are not copies of any "
                f"selected here: {', '.join(stray)}"
            )
    diverged = [_r for _r in verify(pairs, job) if _r.status == "diverged"]
    if diverged:
        raise click.ClickException(
            "shares no snapshot with its source: " + ", ".join(f"{_r.target} ({_r.dataset})" for _r in diverged)
        )
    merged = current | pools
    if merged == current:
        print(f"{job.target} is already claimed for {format_claim(current)}", flush=True)
        return
    on_target(job.ssh_target, "zfs", "set", f"{CLAIM_PROPERTY}={format_claim(merged)}", job.target)
    print(f"claimed {job.target} for {format_claim(merged)}", flush=True)


@autobackup_cli.command()
@click.option("--backup-name", type=str)
@click_add_options(click_global_options)
@click.pass_context
def status(ctx: click.Context, *, backup_name: None | str, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    """Selection properties, jobs, schedule and the last recorded outcome."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    mapping = property_map()
    eprint("=== autobackup properties ===")
    for dataset in sorted(mapping):
        for name, (value, source) in sorted(mapping[dataset].items()):
            if backup_name and name != backup_name:
                continue
            received = " (received)" if is_received(mapping, dataset, name) else ""
            if dict_output:
                print({dataset: {"backup_name": name, "value": value, "source": source, "selected": is_selected(value, source), "received": bool(received)}}, flush=True)
            else:
                print(f"{dataset}\t{name}\t{value}\t{source}{received}", flush=True)
    eprint(f"=== jobs ({CONFIG}) ===")
    config = load_config()
    for name, job in sorted(config.jobs.items()):
        if backup_name and name != backup_name:
            continue
        if dict_output:
            print({name: asdict(job)}, flush=True)
        else:
            print(f"{name}\t{job.ssh_target or 'local'}\t{job.target}\tstrip={job.strip_path}", flush=True)
    eprint("=== schedule ===")
    for path, index, line in schedule_lines():
        print(f"{path}:{index}\t{line}", flush=True)
    eprint(f"=== last outcome ({STATE}) ===")
    if STATE.is_file():
        lines = STATE.read_text(encoding="utf8").splitlines()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(lines[0])))}\t{lines[1]}", flush=True)
        for line in lines[2:]:
            print(f"  {line}", flush=True)
    else:
        print("none recorded", flush=True)


@autobackup_cli.command()
@click.option("--backup-name", type=str)
@click.option("--verified", is_flag=True)
@click.option("--max-age", type=int)
@click.option("--stale-factor", type=int, default=3)
@click.option("--points", type=int, default=0)
@click_add_options(click_global_options)
@click.pass_context
def selected(
    ctx: click.Context, *, backup_name: None | str, verified: bool, max_age: None | int, stale_factor: int,
    points: int, verbose_inf: bool, dict_output: bool, verbose: bool = False,
) -> None:
    """The datasets each job sends; with --verified, the state of each one's
    backup, its target pool's last scrub, and exit 1 on any problem."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    mapping = property_map()
    pairs = [_pair for _pair in selected_pairs(mapping) if not backup_name or _pair[1] == backup_name]
    if not verified:
        for dataset, name in pairs:
            source = mapping[dataset][name][1]
            print({dataset: {"backup_name": name, "source": source}} if dict_output else f"{dataset}\t{name}\t{source}", flush=True)
        return

    config = load_config()
    problems: list[Problem] = []
    results: list[Result] = []
    for name in sorted({_name for _, _name in pairs}):
        job = config.jobs.get(name)
        job_pairs = [_pair for _pair in pairs if _pair[1] == name]
        if job is None:
            problems.append(Problem(name, "no job", f"{CONFIG} has no job {name}"))
            continue
        try:
            refusal = claim_refusal(job, pool_guids(sorted({_ds.split("/")[0] for _ds, _ in job_pairs})), read_claim(job))
            if refusal:
                problems.append(refusal)
                continue
            results += verify(job_pairs, job, max_age=max_age, stale_factor=stale_factor)
        except TargetError as error:
            problems.append(Problem(name, "unreachable", str(error)))

    if dict_output:
        for result in results:
            print({result.dataset: asdict(result)}, flush=True)
    elif results:
        headers = ["dataset", "backup", "status", "newest", "age", "points", "pending", "target"]
        table = [headers] + [
            [_r.dataset, _r.backup_name, _r.status, _r.newest_point or "-", format_age(_r.age),
             str(_r.point_count), str(_r.pending), target_flags(_r)]
            for _r in results
        ]
        widths = table_widths(table)
        eprint(table_line(headers, widths))
        for result, row in zip(results, table[1:], strict=True):
            print(table_line(row, widths), flush=True)
            for snapshot, creation in result.recent_points[-points:] if points else ():
                eprint(f"    {snapshot}  {format_age(int(time.time()) - creation)}")
    for problem in problems:
        print(f"{problem.job}\t{problem.kind}\t{problem.detail}", flush=True)

    for pool, ssh_target in sorted({(_r.target_pool, config.jobs[_r.backup_name].ssh_target) for _r in results if _r.target_pool}, key=str):
        eprint(f"=== {pool} {scrub_line(pool, ssh_target)} ===")

    if problems or any(_r.status != "ok" or target_unsafe(_r) for _r in results):
        ctx.exit(1)


@autobackup_cli.command()
@click.option("--backup-name", type=str)
@click_add_options(click_global_options)
@click.pass_context
def unselected(ctx: click.Context, *, backup_name: None | str, verbose_inf: bool, dict_output: bool, verbose: bool = False) -> None:
    """Datasets no job here sends, and why."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    mapping = property_map()
    covered = {_ds for _ds, _name in selected_pairs(mapping) if not backup_name or _name == backup_name}
    datasets = subprocess.run(
        ["zfs", "list", "-H", "-o", "name", "-t", "filesystem,volume"], capture_output=True, text=True, check=True
    ).stdout.split()
    for dataset in datasets:
        if dataset in covered:
            continue
        properties = mapping.get(dataset, {})
        names = [_n for _n in sorted(properties) if not backup_name or _n == backup_name]
        received = [_n for _n in names if is_received(mapping, dataset, _n)]
        excluded = [_n for _n in names if _n not in received and properties[_n][0] in {"false", "child"}]
        if received:
            reason = f"received:{','.join(received)}"
        elif excluded:
            reason = f"excluded:{','.join(excluded)}"
        else:
            reason = "unconfigured"
        print({dataset: {"reason": reason}} if dict_output else f"{dataset}\t{reason}", flush=True)


@autobackup_cli.command()
@click.option("--backup-name", type=str)
@click.option("--source", "on_source", is_flag=True)
@click.option("--thresholds", type=str, default="1,7,30,90,365")
@click.option("--exact", is_flag=True)
@click.option("--all", "show_all", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def usage(
    ctx: click.Context, *, backup_name: None | str, on_source: bool, thresholds: str, exact: bool,
    show_all: bool, verbose_inf: bool, dict_output: bool, verbose: bool = False,
) -> None:
    """Space held by snapshots older than each threshold, on the targets or
    with --source on this machine."""
    tvic(ctx=ctx, verbose=verbose, verbose_inf=verbose_inf, ic=ic)
    days = sorted({int(_day) for _day in thresholds.split(",")})
    config = load_config()
    pairs = [_pair for _pair in selected_pairs(property_map()) if not backup_name or _pair[1] == backup_name]
    index: dict[str, dict[str, list[Snapshot]]] = {}
    now = int(time.time())
    rendered: list[tuple[str, int, int, int, dict[int, tuple[int, int]]]] = []
    for dataset, name in pairs:
        if on_source:
            examined, ssh_target, prefix = dataset, None, ""
        else:
            job = config.jobs.get(name)
            if job is None:
                continue
            examined, ssh_target, prefix = target_dataset(dataset, job), job.ssh_target, f"{job.target}/"
        key = f"{ssh_target or ''}:{examined.split('/')[0]}"
        if key not in index:
            index[key] = snapshot_index(examined.split("/")[0], ssh_target)
        rows = index[key].get(examined, [])
        if not rows:
            continue
        total = sum(_s.used for _s in rows)
        span = (now - rows[0].creation) // 86400
        buckets: dict[int, tuple[int, int]] = {}
        for day in days:
            older = [_s for _s in rows if _s.creation < now - day * 86400]
            if not older:
                buckets[day] = (0, 0)
                continue
            reclaim = range_reclaim(examined, rows[0].name, older[-1].name, ssh_target) if exact else sum(_s.used for _s in older)
            buckets[day] = (len(older), reclaim)
        if dict_output:
            print({examined: {"snapshots": len(rows), "used_by_snapshots": total, "span_days": span,
                              "older_than": {f"{_d}d": {"count": _c, "reclaim": _r} for _d, (_c, _r) in buckets.items()}}}, flush=True)
            continue
        rendered.append((examined.removeprefix(prefix), len(rows), span, total, buckets))
    if dict_output:
        return
    shown = sorted((_row for _row in rendered if show_all or _row[3]), key=lambda _row: _row[3], reverse=True)
    if not shown:
        return
    headers = ["dataset", "snaps", "span", "total"] + [f">{_day}d" for _day in days]
    table = [headers] + [
        [label, str(count), f"{span}d", format_bytes(total)] + [format_bytes(buckets[_day][1]) for _day in days]
        for label, count, span, total, buckets in shown
    ]
    widths = table_widths(table)
    eprint(table_line(headers, widths))
    for row in table[1:]:
        print(table_line(row, widths), flush=True)
    hidden = len(rendered) - len(shown)
    if hidden:
        eprint(f"({hidden} dataset(s) with no snapshot space hidden, --all to show)")

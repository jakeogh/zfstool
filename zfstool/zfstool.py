#!/usr/bin/env python3

import os
import shutil
import time
import tomllib
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from signal import SIG_DFL
from signal import SIGPIPE
from signal import signal
from statistics import median

import click
import hs
from asserttool import ic
from asserttool import icp
from asserttool import maxone
from click_auto_help import AHGroup
from clicktool import click_add_options
from clicktool import click_global_options
from clicktool import tvic
from devicetool import get_block_device_size
from devicetool import path_is_block_special
from eprint import eprint
from itertool import grouper
from mounttool import block_special_path_is_mounted
from timestamptool import get_timestamp

signal(SIGPIPE, SIG_DFL)

_zfs = hs.Command("zfs")
_zpool = hs.Command("zpool")
_ssh = hs.Command("ssh")

ASHIFT_HELP = """9: 1<<9 == 512
10: 1<<10 == 1024
11: 1<<11 == 2048
12: 1<<12 == 4096
13: 1<<13 == 8192"""

RAID_LIST = [
    "disk",
    "mirror",
    "raidz1",
    "raidz2",
    "raidz3",
    "raidz10",
    "raidz50",
    "raidz60",
]


def zpool_is_imported(zpool: str) -> bool:
    for line in str(_zpool("list")).splitlines()[1:]:
        _pool = line.strip().split(" ")[0]
        icp(_pool)
        if _pool == zpool:
            return True
    return False


@click.group(no_args_is_help=True, cls=AHGroup)
@click_add_options(click_global_options)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )


@cli.command()
@click_add_options(click_global_options)
@click.pass_context
def zfs_check_mountpoints(
    ctx: click.Context,
    *,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    mountpoints = str(_zfs("get", "mountpoint"))
    ic(mountpoints)

    for line in mountpoints.splitlines()[1:]:
        line = " ".join(line.split())
        ic(line)
        zfs_path = line.split(" mountpoint ", maxsplit=1)[0]
        mountpoint = line.split(" mountpoint ")[1]
        if mountpoint.startswith("none"):
            continue
        if mountpoint.startswith("-"):  # snapshot
            assert "@" in zfs_path
            continue
        assert mountpoint.startswith("/")
        mountpoint = mountpoint.split(" ")[0]
        ic(zfs_path, mountpoint)
        assert zfs_path == mountpoint[1:]


@cli.command()
@click.argument(
    "devices",
    required=True,
    nargs=-1,
    type=click.Path(
        exists=False,
        dir_okay=False,
        file_okay=True,
        allow_dash=False,
        path_type=Path,
    ),
)
@click.option("--force", is_flag=True, required=False)
@click.option(
    "--raid",
    is_flag=False,
    required=True,
    type=click.Choice(RAID_LIST),
)
@click.option(
    "--raid-group-size",
    is_flag=False,
    required=True,
    type=int,
)
@click.option(
    "--pool-name",
    is_flag=False,
    required=True,
    type=str,
)
@click.option(
    "--mount-point",
    is_flag=False,
    required=True,
    type=click.Path(
        exists=True,
        dir_okay=True,
        file_okay=False,
        allow_dash=False,
        path_type=Path,
    ),
)
@click_add_options(click_global_options)
@click.pass_context
def write_zfs_root_filesystem_on_devices(
    ctx: click.Context,
    *,
    devices: tuple[Path, ...],
    force: bool,
    raid: str,
    raid_group_size: int,
    pool_name: str,
    mount_point: Path,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )
    devices = tuple(Path(_device) for _device in devices)

    # https://raw.githubusercontent.com/ryao/zfs-overlay/master/zfs-install
    hs.Command("modprobe")("zfs")

    for device in devices:
        assert path_is_block_special(device, symlink_ok=True)
        assert not block_special_path_is_mounted(device)
        if not device.name.startswith("nvme"):
            assert not device.name[-1].isdigit()

    assert len(devices) >= raid_group_size
    assert len(pool_name) > 2

    vdev: list[str] = []
    if len(devices) == 1:
        assert raid == "disk"
        vdev = [devices[0].as_posix()]
    elif len(devices) == 2:
        assert raid == "mirror"
        vdev = ["mirror", devices[0].as_posix(), devices[1].as_posix()]
    else:  # striped mirror raid10
        assert raid == "mirror"
        assert len(devices) % 2 == 0
        for group in grouper(devices, raid_group_size):
            vdev.append("mirror")
            vdev.extend(Path(_).as_posix() for _ in group)
            eprint("vdev:", vdev)
    assert vdev

    zpool_create_args = ["create", "-f"]
    for feature in (
        "async_destroy",
        "blake3",
        "block_cloning",
        "bookmarks",
        "bookmark_v2",
        "bookmark_written",
        "device_rebuild",
        "embedded_data",
        "empty_bpobj",
        "enabled_txg",
        "encryption",
        "extensible_dataset",
        "head_errlog",
        "spacemap_histogram",
        "spacemap_v2",
        "zpool_checkpoint",
        "zstd_compress",
    ):
        zpool_create_args += ["-o", f"feature@{feature}=enabled"]
    zpool_create_args += ["-o", "cachefile=/tmp/zpool.cache"]
    for prop in (
        "atime=off",
        "compression=zstd",
        "copies=1",
        "xattr=sa",
        "sharesmb=off",
        "sharenfs=off",
        "checksum=blake3",
        "dedup=off",
        "utf8only=off",
    ):
        zpool_create_args += ["-O", prop]
    zpool_create_args += ["-m", "none", "-R", mount_point.as_posix(), pool_name]
    zpool_create_args += vdev

    icp(zpool_create_args)
    _zpool(*zpool_create_args, _fg=True)

    _zfs("create", "-o", "mountpoint=none", f"{pool_name}/ROOT", _fg=True)
    _zfs("create", "-o", "mountpoint=/", f"{pool_name}/ROOT/gentoo", _fg=True)
    _zpool("set", f"bootfs={pool_name}/ROOT/gentoo", pool_name, _fg=True)

    zfs_config_dir = mount_point / "etc" / "zfs"
    os.makedirs(zfs_config_dir, exist_ok=True)
    shutil.copy2("/tmp/zpool.cache", zfs_config_dir / "zpool.cache")


@cli.command()
@click.argument(
    "devices",
    required=True,
    nargs=-1,
    type=str,
)
@click.option("--force", is_flag=True, required=False)
@click.option("--simulate", is_flag=True, required=False)
@click.option("--skip-checks", is_flag=True, required=False)
@click.option(
    "--raid",
    is_flag=False,
    required=True,
    type=click.Choice(RAID_LIST),
)
@click.option(
    "--raid-group-size",
    is_flag=False,
    required=True,
    type=int,
)
@click.option(
    "--pool-name",
    is_flag=False,
    required=True,
    type=str,
)
@click.option(
    "--ashift",
    is_flag=False,
    required=False,
    type=int,
    help=ASHIFT_HELP,
)
@click.option("--encrypt", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def create_zfs_pool(
    ctx: click.Context,
    *,
    devices: tuple[str, ...],
    force: bool,
    simulate: bool,
    skip_checks: bool,
    raid: str,
    raid_group_size: int,
    pool_name: str,
    ashift: None | int,
    verbose_inf: bool,
    dict_output: bool,
    encrypt: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    _devices: tuple[Path, ...] = tuple(Path(_device) for _device in devices)

    if ashift:
        assert 9 <= ashift <= 16
        eprint(f"using block size: {1 << ashift} (ashift={ashift})")

    if simulate:
        skip_checks = True

    # https://raw.githubusercontent.com/ryao/zfs-overlay/master/zfs-install
    hs.Command("modprobe")("zfs")

    for device in _devices:
        if not skip_checks:
            assert path_is_block_special(device, symlink_ok=True)
            assert not block_special_path_is_mounted(device)
        if not (
            device.name.startswith("nvme")
            or device.name.startswith("mmcblk")
            or device.name.startswith("wwn-")
        ):
            assert not device.name[-1].isdigit()

    if not skip_checks:
        first_device_size = get_block_device_size(_devices[0])
        for device in _devices:
            assert get_block_device_size(device) == first_device_size

    assert raid_group_size >= 1
    assert len(_devices) >= raid_group_size
    assert len(pool_name) > 2

    vdev: list[str] = []
    if len(_devices) == 1:
        assert raid == "disk"
        vdev = [_devices[0].as_posix()]
    if len(_devices) > 1:
        if raid == "raidz3":
            assert len(_devices) % 2 == 0
        assert raid in {"mirror", "raidz3"}
        assert raid_group_size >= 2
    if len(_devices) == 2:
        assert raid == "mirror"
        vdev = ["mirror", _devices[0].as_posix(), _devices[1].as_posix()]
    if len(_devices) > 2:
        if raid_group_size == 2:  # striped mirror raid10
            for pair in grouper(_devices, 2):
                vdev.append("mirror")
                vdev.extend(Path(_).as_posix() for _ in pair)
                eprint("vdev:", vdev)
        elif raid_group_size == 4:
            raise NotImplementedError(
                "raid_group_size=4: undecided between a 4x mirror and a 2x2 mirror"
            )
        elif raid_group_size in {8, 16}:
            assert raid in {"raidz3", "mirror"}
            vdev = [raid] + [device.as_posix() for device in _devices]
            eprint("vdev:", vdev)
        else:
            if raid != "mirror":
                raise ValueError(
                    f"unknown mode: raid={raid} raid_group_size={raid_group_size}"
                )
            vdev = ["mirror"] + [device.as_posix() for device in _devices]
            eprint("vdev:", vdev)
    assert vdev

    zpool_create_args = ["create"]
    for feature in (
        "async_destroy",
        "blake3",
        "block_cloning",
        "bookmarks",
        "bookmark_v2",
        "device_rebuild",
        "embedded_data",
        "empty_bpobj",
        "enabled_txg",
        "extensible_dataset",
        "head_errlog",
        "spacemap_histogram",
        "spacemap_v2",
        "zpool_checkpoint",
        "large_dnode",
        "large_blocks",
        "zstd_compress",
    ):
        zpool_create_args += ["-o", f"feature@{feature}=enabled"]
    if ashift:
        zpool_create_args += ["-o", f"ashift={ashift}"]
    zpool_create_args += ["-o", "listsnapshots=on"]

    if encrypt:
        zpool_create_args += ["-o", "feature@encryption=enabled"]
        # keylocation=prompt: zpool create prompts for the passphrase itself
        for prop in (
            "encryption=aes-256-gcm",
            "keyformat=passphrase",
            "keylocation=prompt",
            "pbkdf2iters=560000",
            "checksum=blake3",
        ):
            zpool_create_args += ["-O", prop]
    else:
        zpool_create_args += ["-O", "checksum=fletcher4"]

    for prop in (
        "atime=off",
        "compression=zstd",
        "copies=1",
        "xattr=off",
        "sharesmb=off",
        "sharenfs=off",
        "dedup=off",
        "utf8only=off",
        "mountpoint=none",
        "setuid=off",
    ):
        zpool_create_args += ["-O", prop]

    zpool_create_args.append(pool_name)
    zpool_create_args += vdev

    icp(zpool_create_args)
    if not simulate:
        _zpool(*zpool_create_args, _fg=True)


@cli.command()
@click.argument("pool", required=True, nargs=1)
@click.argument("name", required=True, nargs=1)
@click.option(
    "--simulate",
    is_flag=True,
)
@click_add_options(click_global_options)
@click.pass_context
def zfs_filesystem_destroy(
    ctx: click.Context,
    pool: str,
    name: str,
    simulate: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    assert "/" not in pool
    assert not name.startswith("/")
    assert len(pool.split()) == 1
    assert len(name.split()) == 1
    assert len(name) > 2
    destroy_command = _zfs.rebake("destroy", f"{pool}/{name}")
    if simulate:
        print(destroy_command)
        return
    destroy_command(_fg=True)


@cli.command()
@click.argument("pool", required=True, nargs=1)
@click.argument("name", required=True, nargs=1)
@click.option(
    "--simulate",
    is_flag=True,
)
@click.option(
    "--encrypt",
    is_flag=True,
)
@click.option(
    "--nfs-subnet",
    type=str,
)
@click.option(
    "--exec",
    "exe",
    is_flag=True,
)
@click.option(
    "--nomount",
    is_flag=True,
)
@click.option(
    "--reservation",
    type=str,
)
@click_add_options(click_global_options)
@click.pass_context
def create_zfs_filesystem(
    ctx: click.Context,
    pool: str,
    name: str,
    simulate: bool,
    encrypt: bool,
    nfs_subnet: str,
    exe: bool,
    nomount: bool,
    verbose_inf: bool,
    reservation: str,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    assert "/" not in pool
    assert not name.startswith("/")
    assert len(pool.split()) == 1
    assert len(name.split()) == 1
    assert len(name) > 1

    zfs_create_args = ["create", "-o", "setuid=off", "-o", "devices=off"]
    if encrypt:
        for prop in (
            "encryption=aes-256-gcm",
            "keyformat=passphrase",
            "keylocation=prompt",
        ):
            zfs_create_args += ["-o", prop]

    zfs_create_args += ["-o", "exec=on" if exe else "exec=off"]

    if reservation:
        zfs_create_args += ["-o", f"reservation={reservation}"]

    if not nomount:
        zfs_create_args += ["-o", f"mountpoint=/{pool}/{name}"]

    zfs_create_args.append(f"{pool}/{name}")

    if verbose or simulate:
        ic(zfs_create_args)

    if not simulate:
        _zfs(*zfs_create_args, _fg=True)

    if nfs_subnet:
        ctx.invoke(
            zfs_set_sharenfs,
            pool=pool,
            name=name,
            subnet=nfs_subnet,
            simulate=simulate,
        )


@cli.command()
@click.argument("path", required=True, nargs=1)
@click.option(
    "--simulate",
    is_flag=True,
)
@click_add_options(click_global_options)
@click.pass_context
def create_zfs_filesystem_snapshot(
    ctx: click.Context,
    *,
    path: str,
    simulate: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    assert not path.startswith("/")
    assert len(path.split()) == 1
    assert len(path) > 3

    timestamp = str(int(float(get_timestamp())))
    snapshot_command = _zfs.rebake("snapshot", f"{path}@__{timestamp}")

    if verbose or simulate:
        ic(snapshot_command)

    if not simulate:
        snapshot_command()


@cli.command()
@click.argument("pool", required=True, nargs=1)
@click.argument("name", required=True, nargs=1)
@click.argument("subnet", required=True, nargs=1)
@click.option(
    "--no-root-write",
    is_flag=True,
)
@click.option(
    "--off",
    is_flag=True,
)
@click.option(
    "--simulate",
    is_flag=True,
)
@click_add_options(click_global_options)
@click.pass_context
def zfs_set_sharenfs(
    ctx: click.Context,
    *,
    pool: str,
    name: str,
    subnet: str,
    off: bool,
    no_root_write: bool,
    simulate: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )
    maxone([off, no_root_write])

    filesystem = f"{pool}/{name}"

    assert not filesystem.startswith("/")
    assert len(filesystem.split()) == 1
    assert len(filesystem) > 2

    if verbose:
        eprint(str(_zfs("get", "sharenfs", filesystem)))

    if off:
        disable_nfs_command = _zfs.rebake("set", "sharenfs=off", filesystem)
        if simulate:
            print(disable_nfs_command)
        else:
            disable_nfs_command()
        return

    sharenfs_list = [
        "sync",
        "wdelay",
        "hide",
        "crossmnt",
        "secure",
        "no_all_squash",
        "no_subtree_check",
        "secure_locks",
        "mountpoint",
        "anonuid=65534",
        "anongid=65534",
        "sec=sys",
    ]
    # these cause zfs set sharenfs= to fail: acl, no_pnfs

    assert "/" in subnet
    sharenfs_list.append(f"rw={subnet}")

    if no_root_write:
        sharenfs_list.append("root_squash")
    else:
        sharenfs_list.append("no_root_squash")

    sharenfs_line = f"sharenfs={','.join(sharenfs_list)}"
    ic(sharenfs_line)

    zfs_command = _zfs.rebake("set", sharenfs_line, filesystem)
    if simulate:
        print({None: zfs_command} if dict_output else zfs_command, flush=True)
        return
    zfs_command(_fg=True)


_zfs_autobackup = hs.Command("zfs-autobackup")

AUTOBACKUP_PREFIX = "autobackup:"

AUTOBACKUP_CONFIG = Path("/etc/zfstool/autobackup.toml")

CRON_PATHS = (
    Path("/etc/crontab"),
    Path("/etc/cron.d"),
    Path("/etc/cron.hourly"),
    Path("/etc/cron.daily"),
    Path("/etc/cron.weekly"),
    Path("/etc/cron.monthly"),
    Path("/var/spool/cron"),
)


@dataclass(frozen=True)
class AutobackupJob:
    target_path: str
    ssh_target: None | str = None
    strip_path: int = 0
    keep_source: None | str = None
    keep_target: None | str = None
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutobackupResult:
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


def autobackup_jobs(config: Path = AUTOBACKUP_CONFIG) -> dict[str, AutobackupJob]:
    if not config.is_file():
        return {}
    parsed = tomllib.loads(config.read_text(encoding="utf8"))
    jobs: dict[str, AutobackupJob] = {}
    for name, entry in parsed.get("jobs", {}).items():
        assert "target" in entry
        jobs[name] = AutobackupJob(
            target_path=entry["target"],
            ssh_target=entry.get("ssh_target"),
            strip_path=entry.get("strip_path", 0),
            keep_source=entry.get("keep_source"),
            keep_target=entry.get("keep_target"),
            extra=tuple(entry.get("extra", [])),
        )
    return jobs


def autobackup_job_args(name: str, job: AutobackupJob, test: bool) -> list[str]:
    args = [name, job.target_path]
    if job.ssh_target:
        args += ["--ssh-target", job.ssh_target]
    if job.strip_path:
        args += ["--strip-path", str(job.strip_path)]
    if job.keep_source:
        args += ["--keep-source", job.keep_source]
    if job.keep_target:
        args += ["--keep-target", job.keep_target]
    args += [_arg for _arg in job.extra if not (test and _arg == "--progress")]
    if test:
        args.append("--test")
    return args


def zfs_dataset_list() -> list[str]:
    output = str(_zfs("list", "-H", "-o", "name", "-t", "filesystem,volume"))
    return [_line for _line in output.splitlines() if _line]


def autobackup_property_map() -> dict[str, dict[str, tuple[str, str]]]:
    output = str(
        _zfs(
            "get",
            "-H",
            "-o",
            "name,property,value,source",
            "-t",
            "filesystem,volume",
            "all",
        )
    )
    mapping: dict[str, dict[str, tuple[str, str]]] = {}
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        assert len(fields) == 4
        dataset, prop, value, source = fields
        if not prop.startswith(AUTOBACKUP_PREFIX):
            continue
        backup_name = prop[len(AUTOBACKUP_PREFIX) :]
        mapping.setdefault(dataset, {})[backup_name] = (value, source)
    return mapping


def autobackup_is_selected(value: str, source: str) -> bool:
    if value == "true":
        return True
    if value == "child":  # a local "child" excludes the dataset itself
        return source != "local"
    return False


def autobackup_schedule_lines() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in CRON_PATHS:
        if not path.exists():
            continue
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            content = candidate.read_text(encoding="utf8", errors="replace")
            for index, line in enumerate(content.splitlines(), start=1):
                if line.strip().startswith("#"):
                    continue
                if "autobackup" in line:
                    hits.append((candidate, index, line.strip()))
    return hits


def autobackup_format_age(age: None | int) -> str:
    if age is None:
        return "-"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def autobackup_target_unsafe(result: AutobackupResult) -> bool:
    if not result.target:
        return False
    return result.target_mounted or not result.target_readonly


def autobackup_target_state(result: AutobackupResult) -> str:
    if not result.target:
        return "-"
    flags = ["ro" if result.target_readonly else "rw"]
    if result.target_mounted:
        flags.append("mounted")
    return ",".join(flags)


def autobackup_target_dataset(dataset: str, job: AutobackupJob) -> str:
    remainder = dataset.split("/")[job.strip_path :]
    assert remainder
    return "/".join([job.target_path] + remainder)


def zfs_snapshot_index(
    root: str,
    ssh_target: None | str,
    verbose: bool,
) -> dict[str, list[tuple[str, str, int, int, int]]]:
    args = [
        "list",
        "-Hp",
        "-t",
        "snapshot",
        "-o",
        "name,guid,creation,userrefs,used",
        "-r",
        root,
    ]
    if ssh_target:
        command = _ssh.rebake(ssh_target, "zfs", *args)
    else:
        command = _zfs.rebake(*args)
    if verbose:
        icp(command)

    index: dict[str, list[tuple[str, str, int, int, int]]] = {}
    for line in str(command()).splitlines():
        if not line:
            continue
        fields = line.split("\t")
        assert len(fields) == 5
        name, guid, creation, userrefs, used = fields
        dataset, _, snapshot = name.partition("@")
        assert snapshot
        index.setdefault(dataset, []).append(
            (
                snapshot,
                guid,
                int(creation),
                0 if userrefs == "-" else int(userrefs),
                int(used),
            )
        )
    for rows in index.values():
        rows.sort(key=lambda _row: _row[2])
    return index


def zfs_dataset_state(
    root: str,
    ssh_target: None | str,
    verbose: bool,
) -> dict[str, tuple[bool, bool]]:
    args = [
        "list",
        "-H",
        "-o",
        "name,mounted,readonly",
        "-t",
        "filesystem,volume",
        "-r",
        root,
    ]
    if ssh_target:
        command = _ssh.rebake(ssh_target, "zfs", *args)
    else:
        command = _zfs.rebake(*args)
    if verbose:
        icp(command)

    state: dict[str, tuple[bool, bool]] = {}
    for line in str(command()).splitlines():
        if not line:
            continue
        fields = line.split("\t")
        assert len(fields) == 3
        name, mounted, readonly = fields
        state[name] = (mounted == "yes", readonly == "on")
    return state


def zfs_hold_tags(
    snapshots: list[str],
    ssh_target: None | str,
    verbose: bool,
) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for offset in range(0, len(snapshots), 50):
        chunk = snapshots[offset : offset + 50]
        if ssh_target:
            command = _ssh.rebake(ssh_target, "zfs", "holds", "-H", *chunk)
        else:
            command = _zfs.rebake("holds", "-H", *chunk)
        if verbose:
            icp(command)
        for line in str(command()).splitlines():
            if not line:
                continue
            fields = line.split("\t")
            assert len(fields) >= 2
            tags.setdefault(fields[0], []).append(fields[1])
    return tags


def autobackup_stale_threshold(
    creations: list[int],
    max_age: None | int,
    stale_factor: int,
) -> None | int:
    if max_age:
        return max_age
    if len(creations) < 3:
        return None
    intervals = [
        _later - _earlier
        for _earlier, _later in zip(creations[-11:], creations[-10:], strict=False)
    ]
    if not intervals:
        return None
    return int(median(intervals)) * stale_factor


def autobackup_verify(
    *,
    pairs: list[tuple[str, str]],
    jobs: dict[str, AutobackupJob],
    max_age: None | int,
    stale_factor: int,
    verbose: bool,
) -> list[AutobackupResult]:
    if verbose:
        ic(jobs)

    source_pools = sorted({_dataset.split("/")[0] for _dataset, _ in pairs})
    source_index: dict[str, list[tuple[str, str, int, int, int]]] = {}
    for pool in source_pools:
        source_index.update(zfs_snapshot_index(pool, None, verbose))

    target_index: dict[str, dict[str, list[tuple[str, str, int, int, int]]]] = {}
    target_state: dict[str, dict[str, tuple[bool, bool]]] = {}

    held = [
        f"{_dataset}@{_row[0]}"
        for _dataset, _rows in source_index.items()
        for _row in _rows
        if _row[3] > 0
    ]
    source_holds = zfs_hold_tags(held, None, verbose) if held else {}

    now = int(time.time())
    results: list[AutobackupResult] = []

    for dataset, name in pairs:
        job = jobs.get(name)
        if not job:
            results.append(
                AutobackupResult(
                    dataset=dataset,
                    backup_name=name,
                    status="unverifiable",
                    target=None,
                    target_pool=None,
                    newest_common=None,
                    age=None,
                    newest_point=None,
                    oldest_point=None,
                    point_count=0,
                    pending=0,
                    target_mounted=False,
                    target_readonly=True,
                    recent_points=(),
                )
            )
            continue

        target_dataset = autobackup_target_dataset(dataset, job)
        target_pool = target_dataset.split("/")[0]
        key = f"{job.ssh_target or ''}:{target_pool}"
        if key not in target_index:
            target_index[key] = zfs_snapshot_index(
                target_pool, job.ssh_target, verbose
            )
            target_state[key] = zfs_dataset_state(
                target_pool, job.ssh_target, verbose
            )
        mounted, readonly = target_state[key].get(target_dataset, (False, True))

        source_rows = source_index.get(dataset, [])
        target_rows = target_index[key].get(target_dataset, [])

        if not target_rows:
            results.append(
                AutobackupResult(
                    dataset=dataset,
                    backup_name=name,
                    status="missing",
                    target=target_dataset,
                    target_pool=target_pool,
                    newest_common=None,
                    age=None,
                    newest_point=None,
                    oldest_point=None,
                    point_count=0,
                    pending=len(source_rows),
                    target_mounted=mounted,
                    target_readonly=readonly,
                    recent_points=(),
                )
            )
            continue

        target_guids = {_row[1] for _row in target_rows}
        common = [_row for _row in source_rows if _row[1] in target_guids]

        if not common:
            results.append(
                AutobackupResult(
                    dataset=dataset,
                    backup_name=name,
                    status="diverged",
                    target=target_dataset,
                    target_pool=target_pool,
                    newest_common=None,
                    age=None,
                    newest_point=target_rows[-1][0],
                    oldest_point=target_rows[0][0],
                    point_count=len(target_rows),
                    pending=len(source_rows),
                    target_mounted=mounted,
                    target_readonly=readonly,
                    recent_points=tuple((_row[0], _row[2]) for _row in target_rows),
                )
            )
            continue

        newest = common[-1]
        age = now - newest[2]
        pending = sum(1 for _row in source_rows if _row[2] > newest[2])

        hold_tag = f"zfs_autobackup:{name}"
        orphaned = [
            _row[0]
            for _row in source_rows
            if _row[0] != newest[0]
            and hold_tag in source_holds.get(f"{dataset}@{_row[0]}", [])
        ]

        threshold = autobackup_stale_threshold(
            [_row[2] for _row in source_rows], max_age, stale_factor
        )

        if orphaned:
            status = "hold-orphan"
        elif pending and threshold and age > threshold:
            status = "stale"
        else:
            status = "ok"

        results.append(
            AutobackupResult(
                dataset=dataset,
                backup_name=name,
                status=status,
                target=target_dataset,
                target_pool=target_pool,
                newest_common=newest[0],
                age=age,
                newest_point=target_rows[-1][0],
                oldest_point=target_rows[0][0],
                point_count=len(target_rows),
                pending=pending,
                target_mounted=mounted,
                target_readonly=readonly,
                recent_points=tuple((_row[0], _row[2]) for _row in target_rows),
            )
        )

    return results


def autobackup_scrub_line(
    pool: str,
    ssh_target: None | str,
    verbose: bool,
) -> str:
    if ssh_target:
        command = _ssh.rebake(ssh_target, "zpool", "status", pool)
    else:
        command = _zpool.rebake("status", pool)
    if verbose:
        icp(command)
    for line in str(command()).splitlines():
        stripped = line.strip()
        if stripped.startswith("scan:"):
            return stripped
    return "scan: unknown"


@cli.group(no_args_is_help=True, cls=AHGroup)
@click_add_options(click_global_options)
@click.pass_context
def autobackup(
    ctx: click.Context,
    *,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click_add_options(click_global_options)
@click.pass_context
def status(
    ctx: click.Context,
    *,
    backup_name: None | str,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    mapping = autobackup_property_map()

    eprint("=== autobackup properties ===")
    for dataset in sorted(mapping):
        for _name in sorted(mapping[dataset]):
            if backup_name and _name != backup_name:
                continue
            value, source = mapping[dataset][_name]
            selected_state = autobackup_is_selected(value, source)
            if dict_output:
                print(
                    {
                        dataset: {
                            "backup_name": _name,
                            "value": value,
                            "source": source,
                            "selected": selected_state,
                        }
                    },
                    flush=True,
                )
            else:
                print(f"{dataset}\t{_name}\t{value}\t{source}", flush=True)

    eprint(f"=== jobs ({AUTOBACKUP_CONFIG}) ===")
    jobs = autobackup_jobs()
    for _name in sorted(jobs):
        if backup_name and _name != backup_name:
            continue
        job = jobs[_name]
        if dict_output:
            print({_name: asdict(job)}, flush=True)
        else:
            print(
                f"{_name}\t{job.ssh_target or 'local'}\t{job.target_path}\t"
                f"strip={job.strip_path}",
                flush=True,
            )

    eprint("=== schedule ===")
    for path, index, line in autobackup_schedule_lines():
        if dict_output:
            print({path.as_posix(): {"line": index, "text": line}}, flush=True)
        else:
            print(f"{path}:{index}\t{line}", flush=True)


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click.option("--test", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def run(
    ctx: click.Context,
    *,
    backup_name: None | str,
    test: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    if not test:
        assert os.geteuid() == 0

    jobs = autobackup_jobs()
    assert jobs

    if backup_name:
        assert backup_name in jobs
        names = [backup_name]
    else:
        names = sorted(jobs)

    for _name in names:
        command = _zfs_autobackup.rebake(
            *autobackup_job_args(_name, jobs[_name], test)
        )
        icp(command)
        if not test:
            command(_fg=True)
            continue
        for line in command(_iter=True, _err_to_out=True):
            for segment in line.replace("\r", "\n").splitlines():
                if segment:
                    print(f"--test: {segment.rstrip()}", flush=True)


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click.option("--verified", is_flag=True)
@click.option("--target-path", is_flag=False, required=False, type=str)
@click.option("--ssh-target", is_flag=False, required=False, type=str)
@click.option("--strip-path", is_flag=False, required=False, type=int, default=0)
@click.option("--max-age", is_flag=False, required=False, type=int)
@click.option("--stale-factor", is_flag=False, required=False, type=int, default=3)
@click.option("--points", is_flag=False, required=False, type=int, default=0)
@click_add_options(click_global_options)
@click.pass_context
def selected(
    ctx: click.Context,
    *,
    backup_name: None | str,
    verified: bool,
    target_path: None | str,
    ssh_target: None | str,
    strip_path: int,
    max_age: None | int,
    stale_factor: int,
    points: int,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    mapping = autobackup_property_map()

    pairs: list[tuple[str, str]] = []
    for dataset in sorted(mapping):
        for _name in sorted(mapping[dataset]):
            if backup_name and _name != backup_name:
                continue
            value, source = mapping[dataset][_name]
            if not autobackup_is_selected(value, source):
                continue
            pairs.append((dataset, _name))

    if not verified:
        for dataset, _name in pairs:
            source = mapping[dataset][_name][1]
            if dict_output:
                print(
                    {dataset: {"backup_name": _name, "source": source}},
                    flush=True,
                )
            else:
                print(f"{dataset}\t{_name}\t{source}", flush=True)
        return

    if target_path:
        assert backup_name
        jobs = {
            backup_name: AutobackupJob(
                target_path=target_path,
                ssh_target=ssh_target,
                strip_path=strip_path,
            )
        }
    else:
        jobs = autobackup_jobs()

    results = autobackup_verify(
        pairs=pairs,
        jobs=jobs,
        max_age=max_age,
        stale_factor=stale_factor,
        verbose=verbose,
    )

    for result in results:
        if dict_output:
            print({result.dataset: asdict(result)}, flush=True)
        else:
            print(
                "\t".join(
                    [
                        result.dataset,
                        result.backup_name,
                        result.status,
                        result.newest_point or "-",
                        autobackup_format_age(result.age),
                        str(result.point_count),
                        f"pending={result.pending}",
                        autobackup_target_state(result),
                    ]
                ),
                flush=True,
            )
        if points and result.recent_points:
            for _snapshot, _creation in result.recent_points[-points:]:
                eprint(
                    f"    {_snapshot}\t"
                    f"{autobackup_format_age(int(time.time()) - _creation)}"
                )

    for pool in sorted({_r.target_pool for _r in results if _r.target_pool}):
        job = next(_j for _j in jobs.values() if _j.target_path.startswith(pool))
        eprint(f"=== {pool} {autobackup_scrub_line(pool, job.ssh_target, verbose)} ===")

    if any(_r.status != "ok" or autobackup_target_unsafe(_r) for _r in results):
        ctx.exit(1)


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click_add_options(click_global_options)
@click.pass_context
def unselected(
    ctx: click.Context,
    *,
    backup_name: None | str,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    mapping = autobackup_property_map()

    for dataset in zfs_dataset_list():
        properties = mapping.get(dataset, {})
        if backup_name:
            names = [backup_name] if backup_name in properties else []
        else:
            names = sorted(properties)

        covered = [
            _name for _name in names if autobackup_is_selected(*properties[_name])
        ]
        if covered:
            continue

        excluded_by = [
            _name for _name in names if properties[_name][0] in {"false", "child"}
        ]
        reason = f"excluded:{','.join(excluded_by)}" if excluded_by else "unconfigured"

        if dict_output:
            print({dataset: {"reason": reason}}, flush=True)
        else:
            print(f"{dataset}\t{reason}", flush=True)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if value < 1024 or unit == "P":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}P"


def zfs_range_reclaim(
    dataset: str,
    first: str,
    last: str,
    ssh_target: None | str,
    verbose: bool,
) -> int:
    target = f"{dataset}@{first}%{last}"
    if ssh_target:
        command = _ssh.rebake(ssh_target, "zfs", "destroy", "-nvp", target)
    else:
        command = _zfs.rebake("destroy", "-nvp", target)
    if verbose:
        icp(command)
    for line in str(command()).splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == "reclaim":
            return int(fields[1])
    return 0


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click.option("--source", "on_source", is_flag=True)
@click.option("--thresholds", is_flag=False, type=str, default="1,7,30,90,365")
@click.option("--exact", is_flag=True)
@click.option("--all", "show_all", is_flag=True)
@click_add_options(click_global_options)
@click.pass_context
def usage(
    ctx: click.Context,
    *,
    backup_name: None | str,
    on_source: bool,
    thresholds: str,
    exact: bool,
    show_all: bool,
    verbose_inf: bool,
    dict_output: bool,
    verbose: bool = False,
) -> None:
    tty, verbose = tvic(
        ctx=ctx,
        verbose=verbose,
        verbose_inf=verbose_inf,
        ic=ic,
    )

    days = sorted({int(_day) for _day in thresholds.split(",")})
    assert days

    mapping = autobackup_property_map()
    jobs = autobackup_jobs()

    pairs: list[tuple[str, str]] = []
    for dataset in sorted(mapping):
        for _name in sorted(mapping[dataset]):
            if backup_name and _name != backup_name:
                continue
            value, source = mapping[dataset][_name]
            if not autobackup_is_selected(value, source):
                continue
            pairs.append((dataset, _name))

    index: dict[str, dict[str, list[tuple[str, str, int, int, int]]]] = {}
    now = int(time.time())
    rendered: list[tuple[str, int, int, int, dict[int, tuple[int, int]]]] = []

    for dataset, _name in pairs:
        if on_source:
            examined = dataset
            ssh_target = None
            prefix = ""
        else:
            job = jobs.get(_name)
            if not job:
                continue
            examined = autobackup_target_dataset(dataset, job)
            ssh_target = job.ssh_target
            prefix = f"{job.target_path}/"

        pool = examined.split("/")[0]
        key = f"{ssh_target or ''}:{pool}"
        if key not in index:
            index[key] = zfs_snapshot_index(pool, ssh_target, verbose)

        rows = index[key].get(examined, [])
        if not rows:
            continue

        total = sum(_row[4] for _row in rows)
        span = (now - rows[0][2]) // 86400
        buckets: dict[int, tuple[int, int]] = {}

        for day in days:
            cutoff = now - (day * 86400)
            older = [_row for _row in rows if _row[2] < cutoff]
            if not older:
                buckets[day] = (0, 0)
                continue
            if exact:
                reclaim = zfs_range_reclaim(
                    examined, rows[0][0], older[-1][0], ssh_target, verbose
                )
            else:
                reclaim = sum(_row[4] for _row in older)
            buckets[day] = (len(older), reclaim)

        if dict_output:
            print(
                {
                    examined: {
                        "snapshots": len(rows),
                        "used_by_snapshots": total,
                        "span_days": span,
                        "older_than": {
                            f"{_day}d": {"count": _c, "reclaim": _r}
                            for _day, (_c, _r) in buckets.items()
                        },
                    }
                },
                flush=True,
            )
            continue

        label = examined[len(prefix) :] if examined.startswith(prefix) else examined
        rendered.append((label, len(rows), span, total, buckets))

    if dict_output:
        return

    shown = [_row for _row in rendered if show_all or _row[3]]
    shown.sort(key=lambda _row: _row[3], reverse=True)
    if not shown:
        return

    headers = ["dataset", "snaps", "span", "total"] + [f">{_day}d" for _day in days]
    table = [headers]
    for label, count, span, total, buckets in shown:
        table.append(
            [label, str(count), f"{span}d", format_bytes(total)]
            + [format_bytes(buckets[_day][1]) for _day in days]
        )

    widths = [max(len(_row[_i]) for _row in table) for _i in range(len(headers))]
    eprint(
        "  ".join(
            _cell.ljust(widths[_i]) if _i == 0 else _cell.rjust(widths[_i])
            for _i, _cell in enumerate(headers)
        )
    )
    for row in table[1:]:
        print(
            "  ".join(
                _cell.ljust(widths[_i]) if _i == 0 else _cell.rjust(widths[_i])
                for _i, _cell in enumerate(row)
            ),
            flush=True,
        )

    hidden = len(rendered) - len(shown)
    if hidden:
        eprint(f"({hidden} dataset(s) with no snapshot space hidden, --all to show)")

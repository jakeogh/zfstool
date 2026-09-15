#!/usr/bin/env python3

import os
import shutil
import time
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


AUTOBACKUP_PREFIX = "autobackup:"

CRON_PATHS = (
    Path("/etc/crontab"),
    Path("/etc/cron.d"),
    Path("/etc/cron.hourly"),
    Path("/etc/cron.daily"),
    Path("/etc/cron.weekly"),
    Path("/etc/cron.monthly"),
    Path("/var/spool/cron"),
)


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
                if "zfs-autobackup" in line:
                    hits.append((candidate, index, line.strip()))
    return hits


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
            selected = autobackup_is_selected(value, source)
            if dict_output:
                print(
                    {
                        dataset: {
                            "backup_name": _name,
                            "value": value,
                            "source": source,
                            "selected": selected,
                        }
                    },
                    flush=True,
                )
            else:
                print(f"{dataset}\t{_name}\t{value}\t{source}", flush=True)

    eprint("=== schedule ===")
    for path, index, line in autobackup_schedule_lines():
        if dict_output:
            print({path.as_posix(): {"line": index, "text": line}}, flush=True)
        else:
            print(f"{path}:{index}\t{line}", flush=True)


@autobackup.command()
@click.option("--backup-name", is_flag=False, required=False, type=str)
@click.option("--verified", is_flag=True)
@click.option("--target-path", is_flag=False, required=False, type=str)
@click.option("--ssh-target", is_flag=False, required=False, type=str)
@click.option("--strip-path", is_flag=False, required=False, type=int)
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
    strip_path: None | int,
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

    override = AutobackupTarget(
        target_path=target_path,
        ssh_target=ssh_target,
        strip_path=strip_path if strip_path is not None else 0,
    )
    results = autobackup_verify(
        pairs=pairs,
        override=override if target_path else None,
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
                        result.newest_common or "-",
                        autobackup_format_age(result.age),
                        str(result.point_count),
                        f"pending={result.pending}",
                    ]
                ),
                flush=True,
            )
        if points and result.recent_points:
            for name, creation in result.recent_points[-points:]:
                eprint(f"    {name}\t{autobackup_format_age(int(time.time()) - creation)}")

    for pool in sorted({_r.target_pool for _r in results if _r.target_pool}):
        eprint(f"=== {pool} {autobackup_scrub_line(pool, ssh_target, verbose)} ===")

    if any(_r.status != "ok" for _r in results):
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
            _name
            for _name in names
            if autobackup_is_selected(*properties[_name])
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


_ssh = hs.Command("ssh")

AUTOBACKUP_VALUE_OPTIONS = frozenset(
    {
        "--ssh-target",
        "--ssh-source",
        "--ssh-config",
        "--strip-path",
        "--keep-source",
        "--keep-target",
        "--filter-properties",
        "--set-properties",
        "--min-change",
        "--snapshot-format",
        "--property-format",
        "--hold-format",
        "--send-pipe",
        "--recv-pipe",
        "--exclude-received",
        "--destroy-missing",
        "--buffer",
    }
)


@dataclass(frozen=True)
class AutobackupTarget:
    target_path: None | str
    ssh_target: None | str
    strip_path: int


@dataclass(frozen=True)
class AutobackupResult:
    dataset: str
    backup_name: str
    status: str
    target: None | str
    target_pool: None | str
    newest_common: None | str
    age: None | int
    oldest_common: None | str
    point_count: int
    pending: int
    recent_points: tuple[tuple[str, int], ...]


def autobackup_format_age(age: None | int) -> str:
    if age is None:
        return "-"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def autobackup_parse_cron() -> dict[str, AutobackupTarget]:
    targets: dict[str, AutobackupTarget] = {}
    for _path, _index, line in autobackup_schedule_lines():
        if line.startswith("#"):
            continue
        tokens = line.split()
        if "zfs-autobackup" not in " ".join(tokens):
            continue
        start = 0
        for index, token in enumerate(tokens):
            if token.endswith("zfs-autobackup"):
                start = index + 1
                break
        tokens = tokens[start:]

        positional: list[str] = []
        ssh_target: None | str = None
        strip_path = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("-"):
                key, _, inline = token.partition("=")
                if inline:
                    value = inline
                elif key in AUTOBACKUP_VALUE_OPTIONS:
                    index += 1
                    value = tokens[index] if index < len(tokens) else ""
                else:
                    value = ""
                if key == "--ssh-target":
                    ssh_target = value
                elif key == "--strip-path":
                    strip_path = int(value)
            else:
                positional.append(token)
            index += 1

        if len(positional) < 2:
            continue
        targets[positional[0]] = AutobackupTarget(
            target_path=positional[1],
            ssh_target=ssh_target,
            strip_path=strip_path,
        )
    return targets


def autobackup_target_dataset(dataset: str, target: AutobackupTarget) -> str:
    assert target.target_path
    remainder = dataset.split("/")[target.strip_path :]
    assert remainder
    return "/".join([target.target_path] + remainder)


def zfs_snapshot_index(
    root: str,
    ssh_target: None | str,
    verbose: bool,
) -> dict[str, list[tuple[str, str, int, int]]]:
    args = [
        "list",
        "-Hp",
        "-t",
        "snapshot",
        "-o",
        "name,guid,creation,userrefs",
        "-r",
        root,
    ]
    if ssh_target:
        command = _ssh.rebake(ssh_target, "zfs", *args)
    else:
        command = _zfs.rebake(*args)
    if verbose:
        icp(command)

    index: dict[str, list[tuple[str, str, int, int]]] = {}
    for line in str(command()).splitlines():
        if not line:
            continue
        fields = line.split("\t")
        assert len(fields) == 4
        name, guid, creation, userrefs = fields
        dataset, _, snapshot = name.partition("@")
        assert snapshot
        index.setdefault(dataset, []).append(
            (snapshot, guid, int(creation), 0 if userrefs == "-" else int(userrefs))
        )
    for rows in index.values():
        rows.sort(key=lambda _row: _row[2])
    return index


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
    override: None | AutobackupTarget,
    max_age: None | int,
    stale_factor: int,
    verbose: bool,
) -> list[AutobackupResult]:
    cron_targets = {} if override else autobackup_parse_cron()
    if verbose:
        ic(cron_targets)

    source_pools = sorted({_dataset.split("/")[0] for _dataset, _ in pairs})
    source_index: dict[str, list[tuple[str, str, int, int]]] = {}
    for pool in source_pools:
        source_index.update(zfs_snapshot_index(pool, None, verbose))

    target_index: dict[str, dict[str, list[tuple[str, str, int, int]]]] = {}
    target_holds: dict[str, dict[str, list[str]]] = {}

    held = [
        f"{_dataset}@{_row[0]}"
        for _dataset, _rows in source_index.items()
        for _row in _rows
        if _row[3] > 0
    ]
    source_holds = zfs_hold_tags(held, None, verbose) if held else {}
    if verbose:
        ic(source_holds)

    now = int(time.time())
    results: list[AutobackupResult] = []

    for dataset, name in pairs:
        target = override if override else cron_targets.get(name)
        if not target or not target.target_path:
            results.append(
                AutobackupResult(
                    dataset=dataset,
                    backup_name=name,
                    status="unverifiable",
                    target=None,
                    target_pool=None,
                    newest_common=None,
                    age=None,
                    oldest_common=None,
                    point_count=0,
                    pending=0,
                    recent_points=(),
                )
            )
            continue

        target_dataset = autobackup_target_dataset(dataset, target)
        target_pool = target_dataset.split("/")[0]
        key = f"{target.ssh_target or ''}:{target_pool}"
        if key not in target_index:
            target_index[key] = zfs_snapshot_index(
                target_pool, target.ssh_target, verbose
            )
            target_held = [
                f"{_dataset}@{_row[0]}"
                for _dataset, _rows in target_index[key].items()
                for _row in _rows
                if _row[3] > 0
            ]
            target_holds[key] = (
                zfs_hold_tags(target_held, target.ssh_target, verbose)
                if target_held
                else {}
            )

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
                    oldest_common=None,
                    point_count=0,
                    pending=len(source_rows),
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
                    oldest_common=None,
                    point_count=0,
                    pending=len(source_rows),
                    recent_points=(),
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
        elif threshold and age > threshold:
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
                oldest_common=common[0][0],
                point_count=len(common),
                pending=pending,
                recent_points=tuple((_row[0], _row[2]) for _row in common),
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

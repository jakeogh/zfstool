#!/usr/bin/env python3

import os
import shutil
from pathlib import Path
from signal import SIG_DFL
from signal import SIGPIPE
from signal import signal

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

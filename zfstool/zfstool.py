#!/usr/bin/env python3
# -*- coding: utf8 -*-

# flake8: noqa           # flake8 has no per file settings :(
# pylint: disable=C0111  # docstrings are always outdated and wrong
# pylint: disable=C0114  #      Missing module docstring (missing-module-docstring)
# pylint: disable=W0511  # todo is encouraged
# pylint: disable=C0301  # line too long
# pylint: disable=R0902  # too many instance attributes
# pylint: disable=C0302  # too many lines in module
# pylint: disable=C0103  # single letter var names, func name too descriptive
# pylint: disable=R0911  # too many return statements
# pylint: disable=R0912  # too many branches
# pylint: disable=R0915  # too many statements
# pylint: disable=R0913  # too many arguments
# pylint: disable=R1702  # too many nested blocks
# pylint: disable=R0914  # too many local variables
# pylint: disable=R0903  # too few public methods
# pylint: disable=E1101  # no member for base
# pylint: disable=W0201  # attribute defined outside __init__
# pylint: disable=R0916  # Too many boolean expressions in if statement
# pylint: disable=C0305  # Trailing newlines editor should fix automatically, pointless warning
# pylint: disable=C0413  # TEMP isort issue [wrong-import-position] Import "from pathlib import Path" should be placed at the top of the module [C0413]


import os
import sys
import time
from signal import SIG_DFL
from signal import SIGPIPE
from signal import signal

import click
import sh

signal(SIGPIPE, SIG_DFL)
from pathlib import Path
from typing import ByteString
from typing import Generator
from typing import Iterable
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

from asserttool import eprint
from asserttool import ic
from asserttool import maxone
from asserttool import nevd
from blocktool import get_block_device_size
from blocktool import path_is_block_special
from inputtool import passphrase_prompt
from itertool import grouper
from mounttool import block_special_path_is_mounted
from run_command import run_command
from timetool import get_timestamp

ASHIFT_HELP = '''9: 1<<9 == 512
10: 1<<10 == 1024
11: 1<<11 == 2048
12: 1<<12 == 4096
13: 1<<13 == 8192'''


RAID_LIST = ['disk', 'mirror', 'raidz1', 'raidz2', 'raidz3', 'raidz10', 'raidz50', 'raidz60']


@click.group()
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
@click.pass_context
def cli(ctx,
        verbose: bool,
        debug: bool,
        ):

    null, end, verbose, debug = nevd(ctx=ctx,
                                     printn=False,
                                     ipython=False,
                                     verbose=verbose,
                                     debug=debug,)


@cli.command()
@click.option('--verbose', is_flag=True,)
@click.option('--debug', is_flag=True,)
@click.pass_context
def zfs_check_mountpoints(ctx,
                          *,
                          verbose: bool,
                          debug: bool,
                          ):

    mountpoints = sh.zfs.get('mountpoint')
    if verbose:
        ic(mountpoints)

    for line in mountpoints.splitlines()[1:]:
        line = ' '.join(line.split())
        if verbose:
            ic(line)
        zfs_path = line.split(' mountpoint ')[0]
        mountpoint = line.split(' mountpoint ')[1]
        if mountpoint.startswith('none'):
            continue
        if mountpoint.startswith('-'):  # snapshot
            assert '@' in zfs_path
            continue
        assert mountpoint.startswith('/')
        mountpoint = mountpoint.split(' ')[0]
        ic(zfs_path, mountpoint)
        assert zfs_path == mountpoint[1:]


@cli.command()
@click.argument('devices', required=True, nargs=-1)
@click.option('--force', is_flag=True, required=False)
@click.option('--raid', is_flag=False, required=True, type=click.Choice(RAID_LIST))
@click.option('--raid-group-size', is_flag=False, required=True, type=int)
@click.option('--pool-name', is_flag=False, required=True, type=str)
@click.option('--mount-point', is_flag=False, required=True, type=str)
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
def write_zfs_root_filesystem_on_devices(devices: Tuple[Path, ...],
                                         force: bool,
                                         raid: str,
                                         raid_group_size: int,
                                         pool_name: str,
                                         mount_point: str,
                                         verbose: bool,
                                         debug: bool,
                                         ):

    devices = tuple([Path(_device) for _device in devices])

    # https://raw.githubusercontent.com/ryao/zfs-overlay/master/zfs-install
    run_command("modprobe zfs || exit 1", verbose=True)

    for device in devices:
        assert path_is_block_special(device)
        assert not block_special_path_is_mounted(device, verbose=verbose, debug=debug,)
        if not Path(device).name.startswith('nvme'):
            assert not device.name[-1].isdigit()

    #assert raid_group_size >= 2
    assert len(devices) >= raid_group_size

    device_string = ''
    if len(devices) == 1:
        assert raid == 'disk'
        device_string = devices[0].as_posix()

    if len(devices) > 1:
        assert raid == 'mirror'
        assert len(devices) % 2 == 0

    if len(devices) == 2:
        assert raid == 'mirror'
        device_string = "mirror " + devices[0].as_posix() + ' ' + devices[1]

    # striped mirror raid10
    if len(devices) > 2:
        for pair in grouper(devices, raid_group_size):
            device_string = device_string + "mirror " + pair[0] + ' ' + pair[1] + ' '
            eprint("device_string:", device_string)
    assert device_string != ''

    assert len(pool_name) > 2

    zpool_command = """
    zpool create \
    -f \
    -o feature@async_destroy=enabled \
    -o feature@empty_bpobj=enabled \
    -o feature@lz4_compress=enabled \
    -o feature@spacemap_histogram=enabled \
    -o feature@extensible_dataset=enabled \
    -o feature@bookmarks=enabled \
    -o feature@enabled_txg=enabled \
    -o feature@embedded_data=enabled \
    -o cachefile='/tmp/zpool.cache'\
    -O atime=off \
    -O compression=lz4 \
    -O copies=1 \
    -O xattr=sa \
    -O sharesmb=off \
    -O sharenfs=off \
    -O checksum=fletcher4 \
    -O dedup=off \
    -O utf8only=off \
    -m none \
    -R """ + mount_point.as_posix() + ' ' + pool_name + ' ' + device_string

    run_command(zpool_command, verbose=True)

    # Workaround 0.6.4 regression
    #run_command("zfs umount /mnt/gentoo/rpool")
    #run_command("rmdir /mnt/gentoo/rpool")

    # Create rootfs
    run_command("zfs create -o mountpoint=none " + pool_name + "/ROOT", verbose=True)
    run_command("zfs create -o mountpoint=/ " + pool_name + "/ROOT/gentoo", verbose=True)

    # Create home directories
    #zfs create -o mountpoint=/home rpool/HOME
    #zfs create -o mountpoint=/root rpool/HOME/root

    # Create portage directories
    #zfs create -o mountpoint=none -o setuid=off rpool/GENTOO
    #zfs create -o mountpoint=/usr/portage -o atime=off rpool/GENTOO/portage
    #zfs create -o mountpoint=/usr/portage/distfiles rpool/GENTOO/distfiles

    # Create portage build directory
    #run_command("zfs create -o mountpoint=/var/tmp/portage -o compression=lz4 -o sync=disabled rpool/GENTOO/build-dir")

    # Create optional packages directory
    #zfs create -o mountpoint=/usr/portage/packages rpool/GENTOO/packages

    # Create optional ccache directory
    #zfs create -o mountpoint=/var/tmp/ccache -o compression=lz4 rpool/GENTOO/ccache

    # Set bootfs
    run_command("zpool set bootfs=" + pool_name + "/ROOT/gentoo " + pool_name, verbose=True)

    # Copy zpool.cache into chroot
    run_command("mkdir -p /mnt/gentoo/etc/zfs", verbose=True)
    run_command("cp /tmp/zpool.cache /mnt/gentoo/etc/zfs/zpool.cache", verbose=True)

    #print("done making zfs filesystem, here's what is mounted:")
    #run_command('mount')


@cli.command()
@click.argument('devices', required=True, nargs=-1)
@click.option('--force', is_flag=True, required=False)
@click.option('--simulate', is_flag=True, required=False)
@click.option('--skip-checks', is_flag=True, required=False)
@click.option('--raid', is_flag=False, required=True, type=click.Choice(RAID_LIST))
@click.option('--raid-group-size', is_flag=False, required=True, type=int)
@click.option('--pool-name', is_flag=False, required=True, type=str)
@click.option('--ashift', is_flag=False, required=True, type=int, help=ASHIFT_HELP)
@click.option('--encrypt', is_flag=True)
@click.option('--verbose', is_flag=True)
@click.option('--debug', is_flag=True)
def create_zfs_pool(devices,
                    force: bool,
                    simulate: bool,
                    skip_checks: bool,
                    raid: str,
                    raid_group_size: int,
                    pool_name: str,
                    ashift: int,
                    verbose: bool,
                    debug: bool,
                    encrypt: bool,
                    ):
    if verbose:
        ic()
    assert ashift >= 9
    assert ashift <= 16
    eprint("using block size: {} (ashift={})".format(1<<ashift, ashift) )

    if skip_checks:
        assert simulate

    # https://raw.githubusercontent.com/ryao/zfs-overlay/master/zfs-install
    run_command("modprobe zfs || exit 1")

    for device in devices:
        if not skip_checks:
            assert path_is_block_special(device, follow_symlinks=True)
            assert not block_special_path_is_mounted(device, verbose=verbose, debug=debug,)
        if not (Path(device).name.startswith('nvme') or Path(device).name.startswith('mmcblk')):
            assert not device[-1].isdigit()

    if not skip_checks:
        first_device_size = get_block_device_size(devices[0], verbose=verbose, debug=debug)
        for device in devices:
            assert get_block_device_size(device, verbose=verbose, debug=debug) == first_device_size

    assert raid_group_size >= 1
    assert len(devices) >= raid_group_size

    device_string = ''
    if len(devices) == 1:
        assert raid == 'disk'
        device_string = devices[0]

    if len(devices) > 1:
        assert raid in ['mirror', 'raidz3']
        assert len(devices) % 2 == 0
        assert raid_group_size >= 2

    if len(devices) == 2:
        assert raid == 'mirror'
        device_string = "mirror " + devices[0] + ' ' + devices[1]

    if len(devices) > 2:
        if raid_group_size == 2:  # striped mirror raid10
            for pair in grouper(devices, 2):
                device_string = device_string + "mirror " + pair[0] + ' ' + pair[1] + ' '
                eprint("device_string:", device_string)
        elif raid_group_size == 4:
            for quad in grouper(devices, 4):
                assert False # a 4x mirror? or a 2x2 mirror?
                device_string = device_string + "mirror " + quad[0] + ' ' + quad[1] + ' ' + quad[2] + ' ' + quad[3] + ' '
                eprint("device_string:", device_string)
        elif raid_group_size in [8, 16]:
            assert raid == 'raidz3'
            device_string = "raidz3"
            for device in devices:
                device_string += " " + device
            eprint("device_string:", device_string)
        else:
            print("unknown mode")
            quit(1)

    assert device_string != ''
    assert len(pool_name) > 2

    if encrypt:
        if not simulate:
            passphrase = passphrase_prompt("zpool", verbose=verbose, debug=debug,)
            passphrase = passphrase.decode('utf8')

    command = "zpool create"
    command += " -o feature@async_destroy=enabled"       # default   # Destroy filesystems asynchronously.
    command += " -o feature@empty_bpobj=enabled"         # default   # Snapshots use less space.
    command += " -o feature@lz4_compress=enabled"        # default   # (independent of the zfs compression flag)
    command += " -o feature@spacemap_histogram=enabled"  # default   # Spacemaps maintain space histograms.
    command += " -o feature@extensible_dataset=enabled"  # default   # Enhanced dataset functionality.
    command += " -o feature@bookmarks=enabled"           # default   # "zfs bookmark" command
    command += " -o feature@enabled_txg=enabled"         # default   # Record txg at which a feature is enabled
    command += " -o feature@embedded_data=enabled"       # default   # Blocks which compress very well use even less space.
    command += " -o feature@large_dnode=enabled"         # default   # Variable on-disk size of dnodes.
    command += " -o feature@large_blocks=enabled"        # default   # Support for blocks larger than 128KB.
    command += " -o ashift={}".format(ashift)            #           #
    command += " -o listsnapshots=on"

    if encrypt:
        command += " -o feature@encryption=enabled"
        command += " -O encryption=aes-256-gcm"
        command += " -O keyformat=passphrase"
        command += " -O keylocation=prompt"
        command += " -O pbkdf2iters=460000"

    command += " -O atime=off"                           #           # (dont write when reading)
    command += " -O compression=lz4"                     #           # (better than lzjb)
    command += " -O copies=1"                            #
    command += " -O xattr=off"                           #           # (sa is better than on)
    command += " -O sharesmb=off"                        #
    command += " -O sharenfs=off"                        #
    command += " -O checksum=fletcher4"                  # default
    command += " -O dedup=off"                           # default
    command += " -O utf8only=off"                        # default
    command += " -O mountpoint=none"                     # dont mount raw zpools
    command += " -O setuid=off"                          # only needed on rootfs
    command += ' ' + pool_name + ' ' + device_string

    ic(command)
    if not simulate:
        stdin = None
        if encrypt:
            stdin = passphrase
        run_command(command, verbose=True, expected_exit_status=0, stdin=stdin)


@cli.command()
@click.argument('pool', required=True, nargs=1)
@click.argument('name', required=True, nargs=1)
@click.option('--simulate', is_flag=True,)
@click.option('--encrypt', is_flag=True,)
@click.option('--nfs-subnet', type=str,)
@click.option('--exec', 'exe', is_flag=True,)
@click.option('--nomount', is_flag=True,)
@click.option('--reservation', type=str,)
@click.option('--verbose', type=str,)
@click.option('--debug', type=str,)
@click.pass_context
def create_zfs_filesystem(ctx,
                          pool: str,
                          name: str,
                          simulate: bool,
                          encrypt: bool,
                          nfs_subnet: str,
                          exe: bool,
                          nomount: bool,
                          verbose: bool,
                          debug: bool,
                          reservation: bool,
                          ) -> None:

    if verbose:
        ic()

    assert not pool.startswith('/')
    assert not name.startswith('/')
    assert len(pool.split()) == 1
    assert len(name.split()) == 1
    assert len(name) > 2

    # https://raw.githubusercontent.com/ryao/zfs-overlay/master/zfs-install
    #run_command("modprobe zfs || exit 1")

    command = "zfs create -o setuid=off -o devices=off"
    if encrypt:
        command += " -o encryption=aes-256-gcm"
        command += " -o keyformat=passphrase"
        command += " -o keylocation=prompt"
    if not exe:
        command += " -o exec=off"
    if reservation:
        command += " -o reservation=" + reservation

    if not nomount:
        command += " -o mountpoint=/" + pool + '/' + name

    command += ' ' + pool + '/' + name

    if verbose or simulate:
        ic(command)

    if not simulate:
        run_command(command, verbose=True, expected_exit_status=0)

    if nfs_subnet:
        ctx.invoke(zfs_set_sharenfs,
                   filesystem=pool + '/' + name,
                   subnet=nfs_subnet,
                   verbose=verbose,
                   debug=debug,
                   simulate=simulate,)


@cli.command()
@click.argument('path', required=True, nargs=1)
@click.option('--simulate', is_flag=True,)
@click.option('--verbose', type=str,)
@click.option('--debug', type=str,)
@click.pass_context
def create_zfs_filesystem_snapshot(ctx,
                                   path: str,
                                   simulate: bool,
                                   verbose: bool,
                                   debug: bool,
                                   ) -> None:

    if verbose:
        ic()

    assert not path.startswith('/')
    assert len(path.split()) == 1
    assert len(path) > 3

    timestamp = str(int(float(get_timestamp())))
    snapshot_path = path + "@__{timestamp}".format(timestamp=timestamp)
    command = sh.zfs.snapshot.bake(snapshot_path)

    if verbose or simulate:
        ic(command)

    if not simulate:
        command()


@cli.command()
@click.argument('filesystem', required=True, nargs=1)
@click.argument('subnet', required=True, nargs=1)
@click.option('--no-root-write', is_flag=True,)
@click.option('--off', is_flag=True,)
@click.option('--simulate', is_flag=True,)
@click.option('--verbose', is_flag=True,)
@click.option('--debug', is_flag=True,)
def zfs_set_sharenfs(filesystem: str,
                     subnet: str,
                     off: bool,
                     no_root_write: bool,
                     simulate: bool,
                     verbose: bool,
                     debug: bool,
                     ):

    maxone([off, no_root_write])

    assert not filesystem.startswith('/')
    assert len(filesystem.split()) == 1
    assert len(filesystem.split()) == 1
    assert len(filesystem) > 2

    if verbose:
        eprint(sh.zfs.get('sharenfs', filesystem))

    if off:
        disable_nfs_command = sh.zfs.set.bake('sharenfs=off', filesystem)
        if simulate:
            print(disable_nfs_command)
        else:
            disable_nfs_command()
        return

    sharenfs_list = ['sync',
                     'wdelay',
                     'hide',
                     'crossmnt',
                     'secure',
                     'no_all_squash',
                     'no_subtree_check',
                     'secure_locks',
                     'mountpoint',
                     'anonuid=65534',
                     'anongid=65534',
                     'sec=sys',]
    # these cause zfs set sharenfs= command to fail:
    # ['acl', 'no_pnfs']

    sharenfs_list.append('rw=' + subnet)

    if no_root_write:
        sharenfs_list.append('root_squash')
    else:
        sharenfs_list.append('no_root_squash')

    sharenfs_line = ','.join(sharenfs_list)
    if verbose:
        ic(sharenfs_line)

    #sharenfs_line = 'sharenfs=*(' + sharenfs_line + ')'
    sharenfs_line = 'sharenfs=' + sharenfs_line
    if verbose:
        ic(sharenfs_line)

    zfs_command = sh.zfs.set.bake(sharenfs_line, filesystem)
    if simulate:
        print(zfs_command)
    else:
        print(zfs_command())

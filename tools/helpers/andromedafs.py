# Copyright 2026 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
import shutil

import tools.config
import tools.helpers.lxc
import tools.helpers.mount
import tools.helpers.run

def mount_andromedafs(args, host_gid, host_uid, allow_other, disk_side, backing, path, andromeda_data):
    if shutil.which("andromedafs") is None:
        logging.warning("andromedafs not found in PATH")
        return

    disk_side = str(disk_side)
    backing = os.path.expandvars(os.path.expanduser(str(backing)))
    path = os.path.expandvars(os.path.expanduser(str(path)))

    tools.helpers.run.user(args, ["mkdir", "-p", path], check=False)

    opts = "backing={},disk_side={},host_uid={},host_gid={}".format(
        backing, disk_side, str(host_uid), str(host_gid)
    )

    cmd = ["andromedafs", path, "-o", opts]
    if allow_other:
        cmd.extend(["-o", "allow_other"])

    ret = tools.helpers.run.user(args, cmd, check=False)

    if ret != 0:
        logging.warning(f"andromedafs mount failed: {cmd}")
        return

    if disk_side == "host":
        host_side_dest = os.path.join(andromeda_data, "media/0/Linux")

        tools.helpers.run.user(args, ["mkdir", "-p", host_side_dest], check=False)
        tools.helpers.mount.bind(args, path, host_side_dest, create_folders=True, umount=False)

def umount_andromedafs(args, path, andromeda_data):
    path = os.path.expandvars(os.path.expanduser(str(path)))

    try:
        state = tools.helpers.lxc.status(args)
        if state in ("RUNNING", "FROZEN"):
            base_paths = [
                "/mnt/runtime/default/emulated/0",
                "/mnt/runtime/read/emulated/0",
                "/mnt/runtime/write/emulated/0",
                "/mnt/runtime/full/emulated/0",
                "/mnt/user/0/emulated/0",
            ]

            args.uid = 0
            args.gid = 0
            args.nolsm = None
            args.allcaps = None
            args.nocgroup = None
            args.context = None

            for base in base_paths:
                dst = base.rstrip("/") + "/Linux"
                args.COMMAND = ["umount", "-l", dst]
                tools.helpers.lxc.shell(args)
    except Exception as e:
        logging.warning(f"Failed to unmount guest Linux bind mounts: {e}")

    host_side_dest = os.path.join(andromeda_data, "media/0/Linux")
    if tools.helpers.mount.ismount(host_side_dest):
        tools.helpers.mount.umount_all(args, host_side_dest)

    if tools.helpers.mount.ismount(path):
        tools.helpers.mount.umount_all(args, path)

    if tools.helpers.mount.ismount(path):
        logging.warning(f"Failed to unmount andromedafs mount at: {path}")

def configure_andromedafs_guest(args):
    state = tools.helpers.lxc.status(args)
    if state not in ("RUNNING", "FROZEN"):
        logging.warning(f"Andromeda container is {state} (not RUNNING/FROZEN)")
        return

    base_paths = [
        "/mnt/runtime/default/emulated/0",
        "/mnt/runtime/read/emulated/0",
        "/mnt/runtime/write/emulated/0",
        "/mnt/runtime/full/emulated/0",
        "/mnt/user/0/emulated/0",
    ]

    src = "/data/media/0/Linux"

    args.uid = 0
    args.gid = 0
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None

    for base in base_paths:
        dst = base.rstrip("/") + "/Linux"

        args.COMMAND = ["mkdir", "-p", dst]
        tools.helpers.lxc.shell(args)

        args.COMMAND = ["mount", "--bind", src, dst]
        tools.helpers.lxc.shell(args)

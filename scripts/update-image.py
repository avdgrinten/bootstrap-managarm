#!/usr/bin/env python3

import argparse
import errno
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from os import path
from typing import Dict, List

elevation_method = None
verbose = False
MANAGARM_ROOT_PART_TYPE = "64212B3B-56A1-4DFB-971E-BC8CD027996A"
EFI_SYSTEM_PART_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
BIOS_BOOT_PART_TYPE = "21686148-6449-6E6F-744E-656564454649"
# Written by update-boot.py esp-contents.
ESP_STAMP_PATH = "managarm/update-boot.json"
# container.uid and container.gid of bootstrap-site.yml.
CONTAINER_BUILD_ID = 1000
global_mount_info = None
fuse2fs_process = None
sfdisk_command = None

tmpdirs = []

class MountInfo:
    def __init__(self, blockdev, mount_using, image, mountpoint, root_idx, efi_idx):
        self.blockdev = blockdev
        self.mount_using = mount_using
        self.image = image
        self.mountpoint = mountpoint
        self.root_idx = root_idx
        self.efi_idx = efi_idx


class Partition:
    def __init__(self, idx, type, uuid, size, start):
        self.idx = idx
        self.type = type
        self.uuid = uuid
        self.size = size
        self.start = start


# shlex.join is only available since Python 3.8
def join_command(args):
    return " ".join(shlex.quote(x) for x in args)


def run_elevated(cmd, stdin=None, capture_output=True):
    actual_cmd = [elevation_method] + cmd

    if elevation_method == "su":
        actual_cmd = [elevation_method, "-c", join_command(cmd)]

    if verbose:
        print(f'update-image: Running "{join_command(cmd)}" as root')

    res = subprocess.run(actual_cmd, capture_output=capture_output, input=stdin, encoding="utf-8")

    if res.returncode != 0:
        if capture_output:
            stderr = res.stderr.rstrip()
            raise RuntimeError(
                f'Command "{cmd[0]} ..." failed with code {res.returncode} and stderr:'
                f" {stderr}"
            )
        else:
            raise RuntimeError(f'Command "{cmd[0]} ..." failed with code {res.returncode}')

    return res.stdout.rstrip() if capture_output else ""


def run_regular(cmd, stdin=None, capture_output=True):
    if verbose:
        print(f'update-image: Running "{join_command(cmd)}"')

    res = subprocess.run(cmd, capture_output=capture_output, input=stdin, encoding="utf-8")

    if res.returncode != 0:
        if capture_output:
            stderr = res.stderr.rstrip()
            raise RuntimeError(
                f'Command "{cmd[0]} ..." failed with code {res.returncode} and stderr:'
                f" {stderr}"
            )
        else:
            raise RuntimeError(f'Command "{cmd[0]} ..." failed with code {res.returncode}')

    return res.stdout.rstrip() if capture_output else ""


def try_find_command_exec(command):
    i = shutil.which(command)
    if i:
        return i

    i = shutil.which(command, path="/usr/local/sbin:/usr/sbin:/sbin")
    if i:
        return i

    try:
        whereis_out = run_regular(["whereis", "-b", command]).split(" ")
        if len(whereis_out) > 1:
            if path.basename(whereis_out[1]) == command:
                return whereis_out[1]
            print(f"update-image: whereis returned a weird result: {whereis_out}")
    except RuntimeError:
        pass

    raise RuntimeError(f"Couldn't find {command} (tried looking in PATH and using whereis)")


def find_default_sysroot():
    # With sysroot_volume, xbstrap links the volume here. Tasks that run inside of the container
    # see the volume at @SYSROOT_DIR@ and have to pass it via --with-sysroot.
    if os.path.lexists("system-root.volume"):
        return "system-root.volume"
    return "system-root"


def check_if_running(pid):
    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.ESRCH:
            return False

    return True


def get_partition_list(image):
    image_needs_root = not os.access(image, os.R_OK, effective_ids=True)
    cmd = [sfdisk_command, "-d", image]
    out = run_elevated(cmd) if image_needs_root else run_regular(cmd)

    partitions = []

    for line in out.split("\n"):
        if line.startswith(image):
            name, params = line.split(" : ")
            toks = params.split(", ")
            type_param = [i for i in toks if i.startswith("type=")]
            uuid_param = [i for i in toks if i.startswith("uuid=")]
            size_param = [i for i in toks if i.startswith("size=")]
            start_param = [i for i in toks if i.startswith("start=")]

            if (
                len(type_param) != 1
                or len(uuid_param) != 1
                or len(size_param) != 1
                or len(start_param) != 1
            ):
                continue

            type = type_param[0][5:]
            uuid = uuid_param[0][5:]
            size = int(size_param[0][5:])
            start = int(start_param[0][6:])
            idx = name[len(image) :]

            partitions.append(Partition(idx, type, uuid, size, start))

    return partitions


def dev_for_mountpoint(mountpoint):
    mnt = json.loads(run_regular(["findmnt", "--json", mountpoint]))
    partdev = mnt["filesystems"][0]["source"]
    diskdev = "/dev/" + run_regular(["lsblk", "-no", "pkname", partdev])

    return (diskdev, partdev)


def get_partuuid(partitions, idx, what):
    for part in partitions:
        if part.idx == idx and part.uuid:
            # This also normalizes it to lowercase.
            return str(uuid.UUID(part.uuid))
    raise RuntimeError(f"Could not determine the PARTUUID of the {what} partition")


# Must happen before the root fs is synced or made, so that the file also lands on the image.
def write_partition_config(sysroot, root_partuuid, esp_partuuid):
    path = os.path.join(sysroot, "etc/managarm/partitions.toml")
    if verbose:
        print(f"update-image: Writing {path} (root PARTUUID {root_partuuid})")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("[root]\n")
        f.write(f'partuuid = "{root_partuuid}"\n')
        f.write("\n[esp]\n")
        f.write(f'partuuid = "{esp_partuuid}"\n')


class MountAction:
    def __init__(self, mount_using, image, mountpoint):
        self.mount_using = mount_using
        self.image = image
        self.mountpoint = mountpoint

    def run(self):
        global global_mount_info
        global fuse2fs_process

        partitions = get_partition_list(self.image)

        diskdev = self.image
        root_partition = None
        root_start = None
        efi_partition = None
        for part in partitions:
            if part.type == MANAGARM_ROOT_PART_TYPE:
                root_partition = part.idx
                root_start = part.start
            elif part.type == EFI_SYSTEM_PART_TYPE:
                efi_partition = part.idx
                # image_create creates a 256MiB ESP partition, while older images
                # have an ESP that's only 16MiB large.
                if part.size < (128 * 1024 * 1024 / 512):
                    raise RuntimeError("Your image's EFI partition is too small. "
                                       "Update the image_create tool and recreate the image...")

        if global_mount_info:
            valid = True

            # Does the image/device still exist?
            if not os.access(global_mount_info.blockdev, os.F_OK, effective_ids=True):
                print(
                    "update-image: Mount info exists, but refers to a nonexistent block"
                    " device"
                )
                valid = False

            # Is anything mounted at the mountpoint?
            findmnt_res = subprocess.run(
                ["findmnt", global_mount_info.mountpoint],
                stdout=subprocess.DEVNULL
            )
            if findmnt_res.returncode != 0:
                print(
                    "update-image: Mount info exists, but the mountpoint has nothing"
                    " mounted there"
                )
                valid = False

            if valid:
                print(
                    "update-image: The image appears to already be mounted (mount info"
                    " exists and seems to make sense)"
                )
                return
            else:
                print("update-image: Ignoring and removing stale mount info as it's not valid")
                try:
                    os.remove("update-image-mount-info")
                except FileNotFoundError:
                    pass
                global_mount_info = None

        if not efi_partition:
            raise RuntimeError("No suitable EFI system partition found")

        if not root_partition:
            raise RuntimeError("No suitable root partition found")

        if verbose:
            print(f"update-image: EFI partition is partition no. {efi_partition}")
            print(f"update-image: Root partition is partition no. {root_partition}")

        if self.mount_using == "loopback":
            diskdev = run_elevated(["losetup", "-Pf", "--show", self.image])
            if verbose:
                print(f'update-image: "{self.image}" mounted as "{diskdev}"')

        if self.mount_using == "block" or self.mount_using == "loopback":
            # Handle both /dev/sdXY and /dev/nvmeXpY etc correctly
            sep = ""
            if diskdev[-1].isdigit():
                sep = "p"

            try:
                os.mkdir(self.mountpoint)
            except FileExistsError:
                pass
            run_elevated(["mount", f"{diskdev}{sep}{root_partition}", self.mountpoint])
        elif self.mount_using == "guestfs":
            # guestfs requires us to specify all mountpoints at once.
            # Remap UIDs/GIDs to the calling user so that root-owned files in the image
            # (notably under /boot and on the FAT ESP) can be modified without elevation.
            args = [
                "guestmount",
                "--pid-file",
                "guestfs.pid",
                "-o",
                f"uid={os.getuid()}",
                "-o",
                f"gid={os.getgid()}",
                "-a",
                self.image,
                "-m",
                f"/dev/sda{root_partition}",
            ]

            args += ["-m", f"/dev/sda{efi_partition}:/boot"]

            args.append(self.mountpoint)

            try:
                os.mkdir(self.mountpoint)
            except FileExistsError:
                pass
            run_regular(args)
        elif self.mount_using == "fuse2fs":
            repair_root_fs(self.image, root_start)

            try:
                os.mkdir(self.mountpoint)
            except FileExistsError:
                pass
            fuse2fs_process = subprocess.Popen(
                [
                    try_find_command_exec("fuse2fs"),
                    "-f",
                    "-o",
                    f"offset={512 * root_start}",
                    self.image,
                    self.mountpoint,
                ]
            )
            while subprocess.run(["findmnt", self.mountpoint], stdout=subprocess.DEVNULL).returncode:
                if fuse2fs_process.poll() is not None:
                    raise RuntimeError(f"fuse2fs failed with code {fuse2fs_process.returncode}")
                time.sleep(0.1)
        else:
            raise RuntimeError(f"Unknown mount method {self.mount_using}")

        # fuse2fs cannot mount the FAT ESP; UpdateFsAction writes to it using mtools instead.
        if self.mount_using != "fuse2fs":
            efi_path = os.path.join(self.mountpoint, "boot")

            print(f"update-image: EFI partition exists, mounting to {efi_path}")
            if not os.path.isdir(efi_path):
                if self.mount_using == "block" or self.mount_using == "loopback":
                    run_elevated(["mkdir", efi_path])
                else:
                    raise RuntimeError(
                        "/boot does not exist on mountpoint (or is not a directory)"
                    )

        sep = ""
        if diskdev and diskdev[-1].isdigit():
            sep = "p"

        if self.mount_using == "block" or self.mount_using == "loopback":
            run_elevated(["mount", f"{diskdev}{sep}{efi_partition}", efi_path])

        global_mount_info = MountInfo(
            diskdev,
            self.mount_using,
            self.image,
            self.mountpoint,
            root_partition,
            efi_partition,
        )

        with open("update-image-mount-info", "w") as f:
            json.dump(global_mount_info.__dict__, f)

    def name(self):
        return "mount"

    def details(self):
        return f'mount ("{self.image}" to "{self.mountpoint}" using {self.mount_using})'


FsAction = Enum("FsAction", "CREATE_DIR ENSURE_LINKS INSTALL CP RSYNC")


# update-boot.py declares the revision of its command line interface; older versions do not.
def read_cli_revision(update_boot):
    with open(update_boot) as f:
        match = re.search(r"^CLI_REVISION[ \t]*=[ \t]*(.*)$", f.read(), re.MULTILINE)
    if not match:
        return 1
    value = match.group(1).rstrip()
    if not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"{update_boot}: CLI_REVISION is assigned {value!r}, not an integer")
    return int(value)


# update-boot.py esp-contents (CLI revision 2) generates all of the ESP, including its stamp.
def plan_esp_tree(arch, sysroot, scriptdir, bios_boot, update_boot, esp_dir):
    esp_contents_args = [
        sys.executable,
        update_boot,
        "esp-contents",
        "--root",
        sysroot,
        "--arch",
        arch.split('-')[0],
        "--esp-dir",
        esp_dir,
    ]
    if bios_boot:
        esp_contents_args.append("--bios-boot")
    subprocess.run(esp_contents_args, check=True)

    stamp = os.path.join(esp_dir, ESP_STAMP_PATH)
    for dirpath, _, filenames in os.walk(esp_dir):
        dest = os.path.normpath(os.path.join("boot", os.path.relpath(dirpath, esp_dir))) + "/"
        for filename in sorted(filenames):
            if os.path.join(dirpath, filename) != stamp:
                yield FsAction.CP, os.path.join(dirpath, filename), dest

    if arch == "x86_64-managarm":
        yield FsAction.CP, os.path.join(scriptdir, "grub.cfg"), "boot/grub/"

        yield (
            FsAction.CP,
            "tools/host-limine/share/limine/limine-bios.sys",
            "boot/",
        )

    # The stamp claims that the ESP is complete, so it goes last.
    yield FsAction.CP, stamp, os.path.dirname(os.path.join("boot", ESP_STAMP_PATH)) + "/"


# Sysroots whose update-boot.py predates CLI revision 2, or that have none,
# keep the ESP layout of their time until the stable repository catches up.
# Without update-boot.py in the sysroot, the one from extrafiles/ generates all of the ESP into esp_dir,
# and only limine.conf is taken from it.
def plan_legacy_esp(arch, sysroot, scriptdir, esp_dir):
    yield FsAction.INSTALL, "initrd.cpio", "boot/managarm", dict(ignore_sysroot=True)

    limine_conf = os.path.join(esp_dir, "limine.conf")
    update_boot = os.path.join(sysroot, "usr/managarm/bin/update-boot.py")
    if not os.path.exists(update_boot):
        # Sysroots from repositories that predate managarm-scripts, e.g., CI's stable images.
        print("update-image: The sysroot has no update-boot.py, using the one from extrafiles/")
        update_boot = os.path.join(scriptdir, "../extrafiles/managarm-scripts/update-boot.py")
    subprocess.run(
        [
            sys.executable,
            update_boot,
            "esp-contents",
            "--root",
            sysroot,
            "--arch",
            arch.split('-')[0],
            "--esp-dir",
            esp_dir,
        ],
        check=True,
    )

    if arch == "x86_64-managarm":
        yield FsAction.INSTALL, "usr/managarm/bin/eir-mb2", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-limine", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-uefi", "boot/managarm"
        yield FsAction.CP, os.path.join(scriptdir, "grub.cfg"), "boot/grub/"

        yield (
            FsAction.CP,
            os.path.join(sysroot, "usr/share/limine/BOOTX64.EFI"),
            "boot/EFI/BOOT",
        )

        yield (
            FsAction.CP,
            "tools/host-limine/share/limine/limine-bios.sys",
            "boot/",
        )

        yield FsAction.CP, limine_conf, "boot/"
    elif arch == "aarch64-managarm":
        yield FsAction.INSTALL, "usr/managarm/bin/eir-limine", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-linux.bin", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-uefi", "boot/managarm"
        yield (
            FsAction.CP,
            os.path.join(sysroot, "usr/share/limine/BOOTAA64.EFI"),
            "boot/EFI/BOOT",
        )

        yield FsAction.CP, limine_conf, "boot/"
    elif arch == "riscv64-managarm":
        yield FsAction.INSTALL, "usr/managarm/bin/eir-limine", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-linux.bin", "boot/managarm"
        yield FsAction.INSTALL, "usr/managarm/bin/eir-uefi", "boot/managarm"
        yield (
            FsAction.CP,
            os.path.join(sysroot, "usr/share/limine/BOOTRISCV64.EFI"),
            "boot/EFI/BOOT",
        )

        yield FsAction.CP, limine_conf, "boot/"


def generate_plan(arch, sysroot, scriptdir, bios_boot):
    tmpdir = tempfile.TemporaryDirectory()
    tmpdirs.append(tmpdir) # Prevent GC until script terminates.

    for x in [
        "root",
        "usr/bin",
        "usr/lib",
        "var",
        "dev",
        "proc",
        "run",
        "sys",
        "tmp",
        "home",
        "boot",
        "boot/EFI",
        "boot/EFI/BOOT",
        "boot/grub",
        "boot/managarm",
        "opt",
    ]:
        yield FsAction.CREATE_DIR, x

    yield FsAction.ENSURE_LINKS

    update_boot = os.path.join(sysroot, "usr/managarm/bin/update-boot.py")
    if os.path.exists(update_boot) and read_cli_revision(update_boot) >= 2:
        yield from plan_esp_tree(arch, sysroot, scriptdir, bios_boot, update_boot, tmpdir.name)
    else:
        yield from plan_legacy_esp(arch, sysroot, scriptdir, tmpdir.name)

    yield FsAction.RSYNC, "bin"
    yield FsAction.RSYNC, "lib"
    yield FsAction.RSYNC, "sbin"
    yield FsAction.RSYNC, "root/"
    yield FsAction.RSYNC, "usr/"
    yield FsAction.RSYNC, "etc/", ['machine-id', '.updated']
    yield FsAction.RSYNC, "var/"
    yield FsAction.RSYNC, "home/"
    yield FsAction.RSYNC, "opt/"


def ensure_sysroot_links(sysroot):
    for x in ["bin", "lib", "sbin"]:
        wanted_link = os.path.join(sysroot, x)
        if not os.path.islink(wanted_link):
            print(f"{wanted_link} is not a symlink, aborting...")
            raise RuntimeError("Broken sysroot, please fix this manually and rerun this script")


def iterate_plan(plan, callbacks):
    for _act in plan:
        kwargs = {}
        if isinstance(_act, tuple):
            args = list(_act)  # lists have extra operations
        else:
            args = [_act]  # single argument
        cmd = args.pop(0)
        if args and isinstance(args[-1], dict):
            kwargs = args.pop(-1)

        fn = callbacks.get(cmd, None)
        assert fn, f"action {cmd} not implemented"
        fn(*args, **kwargs)


def read_sysroot_volume_workspace(sysroot):
    # With sysroot_volume, xbstrap links the volume at <cbuildrt workspace>/volumes/<name>.
    workspace = os.path.dirname(os.path.dirname(os.path.realpath(sysroot)))
    try:
        with open(os.path.join(workspace, "workspace.json"), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def build_rsync_shift_args(sysroot):
    workspace = read_sysroot_volume_workspace(sysroot)
    if workspace is None:
        # Everything in the sysroot is owned by the current user.
        return ["--numeric-ids", f"--usermap={os.getuid()}:0", f"--groupmap={os.getgid()}:0"]

    # This needs root since a sysroot with real ownership has directories that we cannot read.
    # The trailing slash makes find follow the sysroot if it is a link.
    uids = set()
    gids = set()
    for line in run_elevated(["find", os.path.join(sysroot, ""), "-printf", "%U %G\n"]).split("\n"):
        uid, gid = line.split(" ")
        uids.add(int(uid))
        gids.add(int(gid))

    # Map everything outside of the expected range to nobody.
    def shift(i, subids, host_id):
        # cbuildrt maps the build user to the host user and not into the subordinate range.
        if i == host_id:
            return CONTAINER_BUILD_ID
        if i < subids["start"] or i >= subids["start"] + subids["count"]:
            return 65534 # nobody/nogroup.
        return i - subids["start"]

    usermap = ",".join(f"{u}:{shift(u, workspace['subUid'], os.getuid())}" for u in sorted(uids))
    groupmap = ",".join(f"{g}:{shift(g, workspace['subGid'], os.getgid())}" for g in sorted(gids))
    return ["--numeric-ids", f"--usermap={usermap}", f"--groupmap={groupmap}"]


class UpdateFsAction:
    def __init__(self, mountpoint, sysroot, arch):
        self.mountpoint = mountpoint
        self.sysroot = sysroot
        self.arch = arch

    def run(self):
        needs_root = False
        is_fuse2fs = False
        target_mntpoint = self.mountpoint
        scriptdir = os.path.dirname(sys.argv[0])

        if global_mount_info:
            partitions = get_partition_list(global_mount_info.image)
            root_idx = global_mount_info.root_idx
            efi_idx = global_mount_info.efi_idx
            is_fuse2fs = global_mount_info.mount_using == "fuse2fs"
            needs_root = global_mount_info.mount_using not in ("guestfs", "fuse2fs")
        else:
            # The root is whatever is mounted at the mountpoint; the ESP is on the same disk.
            diskdev, partdev = dev_for_mountpoint(self.mountpoint)
            partitions = get_partition_list(diskdev)
            root_idx = partdev[len(diskdev) :]
            efi_idx = None
            for part in partitions:
                if part.type == EFI_SYSTEM_PART_TYPE:
                    efi_idx = part.idx

            needs_root = not os.access(target_mntpoint, os.W_OK, effective_ids=True)

        write_partition_config(
            self.sysroot,
            get_partuuid(partitions, root_idx, "root"),
            get_partuuid(partitions, efi_idx, "EFI system"),
        )

        print(
            "update-image: Updating the image",
            "requires" if needs_root else "doesn't require",
            "root",
        )

        steps = [["set", "-ex"]]

        # Let rsync shift UIDs/GIDs. This is only possible when running as root.
        rsync_id_args = []
        if needs_root:
            rsync_id_args = build_rsync_shift_args(self.sysroot)
        elif is_fuse2fs:
            # No shift: fuse2fs runs in the same user namespace as rsync. rsync's temporary files
            # must be avoided since fuse2fs syncs the entire file system on each rename.
            rsync_id_args = ["--numeric-ids", "--inplace"]

        boot_dirs = []
        boot_files = {}

        def plan_create_dir(dir):
            if is_fuse2fs and _is_boot(dir):
                boot_dirs.append(dir)
                return
            steps.append(["mkdir", "-p", os.path.join(target_mntpoint, dir)])

        def plan_install(source, dest, ignore_sysroot=False):
            if not ignore_sysroot:
                source = os.path.join(self.sysroot, source)
            if is_fuse2fs and _is_boot(add_dest_basename(source, dest)):
                read_boot_file(boot_files, source, dest)
                return
            target = os.path.join(target_mntpoint, dest)

            steps.append(["install", source, target])

        def plan_rsync(dir, exclude_files=[]):
            source = os.path.join(self.sysroot, dir)
            target = os.path.join(target_mntpoint, dir)
            if os.path.islink(source):
                # rsync 3.5 cannot use an existing symlink as its destination
                # directory. Sync the link through its parent instead.
                target = os.path.dirname(target)
            command = ["rsync", "--checksum", "-a", "--delete"]
            command += rsync_id_args
            command += [source, target]
            if exclude_files:
                for f in exclude_files:
                    command.append(f"--exclude={os.path.join(f)}")
            steps.append(command)

        def plan_cp(source, dest):
            if is_fuse2fs and _is_boot(add_dest_basename(source, dest)):
                read_boot_file(boot_files, source, dest)
                return
            target = os.path.join(target_mntpoint, dest)
            steps.append(["cp", source, target])

        def plan_ensure_links():
            ensure_sysroot_links(self.sysroot)

        bios_boot = any(part.type.upper() == BIOS_BOOT_PART_TYPE for part in partitions)
        iterate_plan(
            generate_plan(self.arch, self.sysroot, scriptdir, bios_boot),
            {
                FsAction.CREATE_DIR: plan_create_dir,
                FsAction.ENSURE_LINKS: plan_ensure_links,
                FsAction.INSTALL: plan_install,
                FsAction.CP: plan_cp,
                FsAction.RSYNC: plan_rsync,
            },
        )

        print("update-image: Updating the file system...")

        commands = "\n".join([join_command(step) for step in steps])

        if needs_root:
            run_elevated(["bash"], commands, False)
        else:
            run_regular(["bash"], commands, False)

        if is_fuse2fs:
            efi_start = next(part.start for part in partitions if part.idx == efi_idx)
            populate_esp(global_mount_info.image, efi_start, boot_dirs, boot_files)

        print("update-image: Done")

    def name(self):
        return "update file system"

    def details(self):
        return f'update file system (from "{self.sysroot}" to "{self.mountpoint}")'


class UnmountAction:
    def __init__(self):
        pass

    def run(self):
        global global_mount_info

        if not global_mount_info:
            raise RuntimeError("File system not mounted")

        blockdev = global_mount_info.blockdev
        mount_using = global_mount_info.mount_using
        mountpoint = global_mount_info.mountpoint

        efi_path = os.path.join(mountpoint, "boot")
        if mount_using == "block" or mount_using == "loopback":
            run_elevated(["umount", "-l", efi_path])

        if mount_using == "loopback":
            run_elevated(["losetup", "-d", f"{blockdev}"])
            run_regular(["udevadm", "settle"])

        if mount_using == "block" or mount_using == "loopback":
            run_elevated(["umount", "-l", mountpoint])
        elif mount_using == "guestfs":
            run_regular(["guestunmount", mountpoint])

            with open("guestfs.pid", "r") as pidfile:
                pid = int(pidfile.read().rstrip())
                for _ in range(10):
                    if check_if_running(pid):
                        time.sleep(0.1)

                while check_if_running(pid):
                    time.sleep(1)
        elif mount_using == "fuse2fs":
            run_regular([try_find_command_exec("fusermount3"), "-u", mountpoint])

            # fuse2fs only flushes the file system after the unmount has returned.
            if fuse2fs_process.wait() != 0:
                raise RuntimeError(f"fuse2fs failed with code {fuse2fs_process.returncode}")
        else:
            raise RuntimeError(f"Unknown mount method {mount_using}")

        if mount_using == "loopback":
            while True:
                loopdevices = json.loads(run_elevated(["losetup", "--json"]))["loopdevices"]
                if len(list(filter(lambda x: x["name"] == blockdev, loopdevices))) == 0:
                    break
                time.sleep(.2)

        # Delete mount info
        os.remove("update-image-mount-info")
        global_mount_info = None

    def name(self):
        return "unmount"

    def details(self):
        if not global_mount_info:
            return "unmount (file system not mounted)"

        return (
            f'unmount ("{global_mount_info.mountpoint}" using' f" {global_mount_info.mount_using})"
        )


def add_dest_basename(src, dst):
    if dst[-1] == '/':
        dst += os.path.basename(src)
    return dst


def _is_boot(x):
    return (
        (x.startswith("boot/") or x.startswith("/boot/"))
        and (len(x) > 6 if x[0] == '/' else len(x) > 5)
    )


def logged_run(cmd, *args, **kwargs):
    print("update-image: running program: " + shlex.join(cmd))
    return subprocess.run(cmd, *args, **kwargs)


def logged_check_call(cmd, *args, **kwargs):
    return logged_run(cmd, check=True, *args, **kwargs)


def read_boot_file(boot_files, src, dst):
    # XXX(arsen): I assume the path provided is a dir
    dst = os.path.join(dst, os.path.basename(src))
    with open(src, "rb") as fsrc:
        boot_files[dst] = fsrc.read()


def repair_root_fs(image, start):
    # fuse2fs refuses file systems with errors, and guest writes routinely leave some behind.
    res = logged_run([e2fsck_command, "-fy", "{}?offset={}".format(image, 512 * start)])
    if res.returncode not in (0, 1):
        raise RuntimeError(
            f"e2fsck could not repair the root file system of {image} "
            f"(code {res.returncode}), recreate it using remake-image"
        )


def populate_esp(image, start, boot_dirs, boot_files):
    # manipulate it using mtools
    dosimg = "{}@@{}".format(image, start * 512)

    # ... and populate it, first with directories ...
    for dir in boot_dirs:
        if dir.startswith("/"):
            dir = dir[1:]
        dir = dir[len("boot") :]
        # mmd fails if the directory already exists.
        probe = logged_run(
            ["mdir", "-i", dosimg, f"::{dir}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode:
            logged_check_call(["mmd", "-i", dosimg, dir])
    # ... then with files
    for file, val in boot_files.items():
        if file.startswith("/"):
            file = file[1:]
        file = file[len("boot") :]
        logged_check_call(["mcopy", "-o", "-i", dosimg, "-", f"::{file}"], input=val)


@dataclass
class RemakeImageAction:
    """
    This action remakes the filesystems in the image directly from the sysroot.
    This approach turns out to be extremely quick and efficient, but does require
    modifying the host sysroot directory.
    """

    image: str
    mountpoint: str
    sysroot: str
    arch: str

    _boot_files: Dict[str, bytes] = field(default_factory=dict, init=False)
    _boot_dirs: List[str] = field(default_factory=list, init=False)

    def _create_dir(self, x):
        if _is_boot(x):
            self._boot_dirs.append(x)
        else:
            os.makedirs(os.path.join(self.sysroot, x), exist_ok=True)

    def _ensure_links(self):
        ensure_sysroot_links(self.sysroot)

    def _install(self, src, dst, ignore_sysroot=False):
        if not ignore_sysroot:
            src = os.path.join(self.sysroot, src)
        if _is_boot(add_dest_basename(src, dst)):
            read_boot_file(self._boot_files, src, dst)
            return

        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))

        dst = os.path.join(self.sysroot, dst)
        if os.path.samefile(src, dst):
            return

        installargs = ["install"]

        installargs += [src, dst]
        logged_check_call(installargs)

    def run(self):
        if not os.access(self.image, os.W_OK | os.R_OK):
            raise RuntimeError(f"{self.image} does not exist or is not r/w accessible")

        scriptdir = os.path.dirname(sys.argv[0])

        part_lines = (
            logged_run(
                ["partx", "-gbs", "--output", "start,size,type,uuid", self.image],
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .splitlines()
        )
        parts = [[x for x in part_line.split(" ") if x] for part_line in part_lines]
        if not parts:
            raise RuntimeError(f"couldn't find any partitions in {self.image}")

        def partuuid_of_type(wanted_type, what):
            partuuids = [partuuid for _, _, parttype, partuuid in parts
                         if parttype.upper() == wanted_type]
            if len(partuuids) != 1:
                raise RuntimeError(f"{self.image} does not have exactly one {what} partition")
            return str(uuid.UUID(partuuids[0]))

        write_partition_config(
            self.sysroot,
            partuuid_of_type(MANAGARM_ROOT_PART_TYPE, "root"),
            partuuid_of_type(EFI_SYSTEM_PART_TYPE, "EFI system"),
        )

        # start by correcting the sysroot slightly
        iterate_plan(
            generate_plan(
                self.arch,
                self.sysroot,
                scriptdir,
                any(parttype.upper() == BIOS_BOOT_PART_TYPE for _, _, parttype, _ in parts),
            ),
            {
                FsAction.CREATE_DIR: self._create_dir,
                FsAction.ENSURE_LINKS: self._ensure_links,
                FsAction.INSTALL: self._install,
                FsAction.CP: functools.partial(self._install, ignore_sysroot=True),
                FsAction.RSYNC: lambda *a, **kw: None,  # ignored
            },
        )

        def setup_rootfs(start, size):
            cmd = [
                mke2fs_command,
                "-Ft",
                "ext2",
                "-M",
                "/",
                "-L",
                "managarm_rootfs",
                "-d",
                self.sysroot,
                "-E",
                # XXX: is 512 correct?
                "offset={}".format(512 * start),
                self.image,
                "{}K".format(size // 1024),
            ]
            logged_check_call(cmd)

        def setup_esp(start, size):
            # image_create creates a 256MiB ESP partition, while older images
            # have an ESP that's only 16MiB large.
            if size < (128 * 1024 * 1024):
                raise RuntimeError("Your image's EFI partition is too small. "
                                   "Update the image_create tool and recreate the image...")

            # create the ESP
            cmd = [
                mkfsvfat_command,
                "-F",
                "32",
                "-n",
                "ESP",
                "-s",
                "2",
                "-S",
                "512",
                "--offset",
                str(start),
                self.image,
                str(size // 1024),
            ]
            logged_check_call(cmd)

            populate_esp(self.image, start, self._boot_dirs, self._boot_files)

        for start, size, parttype, _ in parts:
            setup_fn = {
                MANAGARM_ROOT_PART_TYPE: setup_rootfs,
                EFI_SYSTEM_PART_TYPE: setup_esp,
            }.get(
                parttype.upper(), lambda x, y: None
            )  # noop default

            setup_fn(int(start), int(size))

    def name(self):
        return "remake"

    def details(self):
        return f"remake {self.image} from scratch"


def make_action(name, mount_using, image, mountpoint, sysroot, arch):
    if name == "mount":
        return MountAction(mount_using, image, mountpoint)
    elif name == "update-fs":
        return UpdateFsAction(mountpoint, sysroot, arch)
    elif name == "unmount":
        return UnmountAction()
    elif name == "remake":
        return RemakeImageAction(image, mountpoint, sysroot, arch)
    else:
        raise RuntimeError(f"Unknown action {name}")


def determine_elevation_method(method):
    if method != "auto" and shutil.which(method):
        return method

    for i in ["sudo", "doas", "su"]:
        if shutil.which(i):
            return i

    raise RuntimeError("No method to elevate permissions found")


class Plan:
    def __init__(self, actions):
        self.actions = actions
        self.error_count = 0

    def print(self):
        print("update-image: Running the following plan:")
        for action in self.actions:
            print(f" - {action.name()}")

    def run(self):
        for action in self.actions:
            print(f"update-image: Running action {action.details()}")
            try:
                action.run()
            except Exception:
                self.error_count += 1
                print(f"update-image: Action {action.name()} failed with:")
                traceback.print_exc()


parser = argparse.ArgumentParser(description="Update a managarm installation")
parser.add_argument("-v", "--verbose", action="store_true", help="Be verbose")
parser.add_argument(
    "-m",
    "--mount-using",
    dest="mount_using",
    choices=["loopback", "guestfs", "block", "fuse2fs"],
    default="guestfs",
    help="How to mount the image (default: guestfs)",
)
parser.add_argument(
    "-i",
    "--with-image",
    dest="image_path",
    metavar="image",
    type=str,
    default="image",
    help="Path to image (default: image)",
)
parser.add_argument(
    "-M",
    "--with-mount-point",
    dest="mountpoint_path",
    metavar="mountpoint",
    type=str,
    default="mountpoint",
    help="Path to mount point (default: mountpoint)",
)
parser.add_argument(
    "-s",
    "--with-sysroot",
    dest="sysroot_path",
    metavar="sysroot",
    type=str,
    default=find_default_sysroot(),
    help="Path to system root (default: system-root.volume if it exists, otherwise system-root)",
)
parser.add_argument(
    "-e",
    "--elevate-with",
    dest="elevate_with",
    choices=["sudo", "doas", "su", "auto"],
    default="auto",
    help="How to run commands as root (required for loopback and block, default: auto)",
)
parser.add_argument(
    "-t",
    "--triple",
    dest="arch",
    choices=["x86_64-managarm", "aarch64-managarm", "riscv64-managarm"],
    default="x86_64-managarm",
    help="Target system triple (needed for the update-fs step, default: x86_64-managarm)",
)
parser.add_argument(
    "plan",
    metavar="action",
    nargs="*",
    choices=[[], "mount", "update-fs", "unmount", "remake"],
    help=(
        "Action to run (one of: mount, update-fs, unmount, remake).\n"
        "The default list of actions is [mount, update-fs, unmount]"
    ),
)

args = parser.parse_args()

action_list = ["mount", "update-fs", "unmount"]
if len(args.plan):
    action_list = args.plan

verbose = args.verbose
elevation_method = determine_elevation_method(args.elevate_with)

if verbose:
    print("update-image: Being extra verbose")
    print(f'update-image: Using "{elevation_method}" for permission elevation')

try:
    with open("update-image-mount-info", "r") as f:
        info = json.load(f)
        global_mount_info = MountInfo(
            info["blockdev"],
            info["mount_using"],
            info["image"],
            info["mountpoint"],
            info["root_idx"],
            info["efi_idx"],
        )
except (FileNotFoundError, json.JSONDecodeError):
    pass

sfdisk_command = try_find_command_exec("sfdisk")
mkfsvfat_command = try_find_command_exec("mkfs.vfat")
mke2fs_command = try_find_command_exec("mke2fs")
e2fsck_command = try_find_command_exec("e2fsck")

if verbose:
    print(f'update-image: Found sfdisk at "{sfdisk_command}"')

plan = Plan(
    [
        make_action(
            v,
            args.mount_using,
            args.image_path,
            args.mountpoint_path,
            args.sysroot_path,
            args.arch,
        )
        for v in action_list
    ]
)
plan.print()
plan.run()

print("update-image: Finished running plan.")
if plan.error_count > 0:
    print(f"update-image: {plan.error_count} step(s) failed.")
    sys.exit(1)

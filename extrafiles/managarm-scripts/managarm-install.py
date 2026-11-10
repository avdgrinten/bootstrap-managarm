#!/usr/bin/env python3

# Installs Managarm from an xbps repository that provision-sysroot.py sets up.
# The host (CI's image builds) and the target run this script, so it only uses the stdlib.

import argparse
import ctypes
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROVISION = os.path.join(SCRIPT_DIR, "provision-sysroot.py")

# The live image's accounts: the managarm user and root with their known passwords, SSH password logins.
LIVE_ACCOUNTS = [
    "--user", "managarm",
    "--groups", "managarm", "wheel,users",
    "--password-hash", "managarm", "WJg36EZUlJ96k",
    "--password-hash", "root", "4T1CNB2BgzJeI",
    "--sshd-password-auth",
]

BIOS_BOOT_TYPE = "21686148-6449-6E6F-744E-656564454649"
ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
ROOT_TYPE = "64212B3B-56A1-4DFB-971E-BC8CD027996A"
# The root takes the rest of the disk.
PART_SIZES = {BIOS_BOOT_TYPE: "16MiB", ESP_TYPE: "256MiB", ROOT_TYPE: "+"}
# What every installation gets, regardless of --package; base brings the kernel, the system, kmscon and these scripts.
# TODO: Remove limine, mtools and python once base includes them also in the stable repository.
BOOT_PACKAGES = ["base", "limine", "mtools", "python"]
DISK_TOOLS = {
    "mount": "util-linux",
    "sfdisk": "util-linux",
    "mke2fs": "e2fsprogs",
    "mkfs.vfat": "dosfstools",
    "mcopy": "mtools",
    "xbps-install": "xbps",
}


class InstallError(Exception):
    pass


def log(message):
    print(f"managarm-install: {message}", flush=True)


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def provision(root, *args):
    run([sys.executable, PROVISION, "apply", "--root", root, *args])


def xbps_tool(name):
    # On the build host, xbstrap's static xbps is the one that can install Managarm packages.
    host_tool = os.path.expanduser(f"~/.xbstrap/bin/{name}")
    if os.path.exists(host_tool):
        return host_tool
    tool = shutil.which(name)
    if not tool:
        raise InstallError(f"{name} not found")
    return tool


def populate_root(root, arch, channel, packages, account_args, repositories=()):
    # xbps-install takes the repository and its signing key from the root, so this goes first.
    provision(root, "--arch", arch, f"--xbps-{channel}")

    # The host's xbps may have been built with another sysconfdir, so name the root's config explicitly.
    conf_dir = os.path.abspath(os.path.join(root, "etc/xbps.d"))
    cmd = [xbps_tool("xbps-install"), "-y", "-S", "-U", "-r", root, "-C", conf_dir]
    for repository in repositories:
        cmd += ["-R", repository]
    # XBPS_TARGET_ARCH skips the package scripts even if it matches the native architecture, and -U
    # keeps the packages unconfigured, so that xbps-reconfigure configures them on the first boot.
    run(cmd + packages, env={**os.environ, "XBPS_TARGET_ARCH": arch})

    # After the packages, which may have created /root with mode 0755.
    # The packages' sysusers hooks only run on the first boot, but the accounts need groups such as wheel.
    provision(root, "--base-dirs", "--sysusers", *account_args)


def cmd_sysroot(args):
    # TODO: Remove limine once base includes it also in the stable repository.
    populate_root(args.root, args.arch, args.xbps_channel, ["base", "limine"], LIVE_ACCOUNTS)


# Partition nodes are numbered from zero, e.g. sda0 or nvme0n1p0.
def part_name(disk, idx):
    return f"{disk}p{idx}" if disk[-1].isdigit() else f"{disk}{idx}"


# Returns the disk that a partition belongs to, or None if the device is not a partition.
def disk_of_part(dev):
    for disk in os.listdir("/sys/block"):
        if dev == disk or not dev.startswith(disk):
            continue
        m = re.fullmatch(r"p?([0-9]+)", dev[len(disk):])
        if m and part_name(disk, int(m.group(1))) == dev:
            return disk
    return None


# Returns (type, uuid) of each of a disk's partitions, in the order in which they are numbered.
def partitions_of(disk):
    dump = run(["sfdisk", "-J", f"/dev/{disk}"], capture_output=True, text=True).stdout
    return [
        (part["type"].upper(), part.get("uuid", "").lower())
        for part in json.loads(dump)["partitiontable"]["partitions"]
    ]


def disk_bytes(disk):
    with open(f"/sys/block/{disk}/size") as f:
        return int(f.read()) * 512


def wait_for_node(name):
    for _ in range(100):
        if os.path.exists(f"/dev/{name}"):
            return True
        time.sleep(0.1)
    return False


def hash_password(password):
    # Python 3.13 removed the crypt module, so call libxcrypt (part of base) directly.
    for soname in ["libcrypt.so.2", "libcrypt.so.1"]:
        try:
            libcrypt = ctypes.CDLL(soname)
            break
        except OSError:
            continue
    else:
        raise InstallError("libcrypt not found, install libxcrypt or pass --password-hash")
    libcrypt.crypt_gensalt.restype = ctypes.c_char_p
    libcrypt.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
    libcrypt.crypt.restype = ctypes.c_char_p
    libcrypt.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

    # Pass the random bytes ourselves instead of relying on libxcrypt's own entropy source.
    rbytes = os.urandom(32)
    setting = libcrypt.crypt_gensalt(b"$y$", 0, rbytes, len(rbytes))
    if not setting:
        raise InstallError("crypt_gensalt() failed")
    hashed = libcrypt.crypt(password.encode(), setting)
    # libxcrypt reports failures with a hash that starts with "*".
    if not hashed or hashed.startswith(b"*"):
        raise InstallError("crypt() failed")
    return hashed.decode()


def ask_password(user):
    while True:
        password = getpass.getpass(f"Password for {user}: ")
        if not password:
            print("The password must not be empty.")
            continue
        if getpass.getpass(f"Password for {user} (again): ") == password:
            return password
        print("The passwords do not match.")


# update-boot.py reads this, both now and when it regenerates the boot files on the installed system.
def write_partition_config(root, root_partuuid, esp_partuuid):
    path = os.path.join(root, "etc/managarm/partitions.toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'[root]\npartuuid = "{root_partuuid}"\n\n[esp]\npartuuid = "{esp_partuuid}"\n')


def cmd_disk(args):
    target = args.disk.removeprefix("/dev/")
    root = args.mountpoint
    packages = args.packages + BOOT_PACKAGES
    # TODO: The aarch64 and riscv64 code paths are untested, including those of update-boot.py update-esp.
    arch = platform.machine()
    # Only x86_64 boots via BIOS, where Limine's stages need a partition of their own.
    part_types = [BIOS_BOOT_TYPE, ESP_TYPE, ROOT_TYPE] if arch == "x86_64" else [ESP_TYPE, ROOT_TYPE]

    if os.geteuid() != 0:
        raise InstallError("must be run as root")

    missing = sorted({package for tool, package in DISK_TOOLS.items() if not shutil.which(tool)})
    if missing:
        raise InstallError(f"missing tools, install these packages first: {' '.join(missing)}")

    if not os.path.exists(f"/dev/{target}"):
        raise InstallError(f"/dev/{target} does not exist")
    if not os.path.isdir(f"/sys/block/{target}"):
        raise InstallError(f"/dev/{target} is not a disk")
    if disk_of_part(target):
        raise InstallError(f"/dev/{target} is a partition, pass the whole disk")

    password_hash = args.password_hash
    if password_hash is None and not args.yes:
        password_hash = hash_password(ask_password(args.user))

    log(f"target: /dev/{target} ({disk_bytes(target) // (1 << 30)} GiB)")
    log(f"repository: {', '.join([args.xbps_channel, *args.repository])}")
    log(f"packages: {' '.join(packages)}")
    if password_hash is None:
        log(f"warning: {args.user} gets no password and cannot log in until one is set")

    if not args.yes:
        if args.keep_partitions:
            print(f"The ESP and root partitions of /dev/{target} will be reformatted.")
        else:
            print(f"ALL DATA ON /dev/{target} WILL BE DESTROYED.")
        if input("Type 'yes' to continue: ") != "yes":
            raise InstallError("aborted")

    target_parts = [part_name(target, idx) for idx in range(len(part_types))]
    target_esp, target_root = target_parts[-2:]

    if not args.keep_partitions:
        if os.path.exists(f"/dev/{target_parts[0]}"):
            raise InstallError(
                f"/dev/{target} already has partitions; Managarm can only re-read the partition table"
                " of a disk without partitions. Either rerun with --keep-partitions or wipe the disk"
                f" (wipefs -a /dev/{target}), reboot and rerun."
            )
        log(f"partitioning /dev/{target}")
        run(
            ["sfdisk", "--wipe", "always", "--wipe-partitions", "always", f"/dev/{target}"],
            input="label: gpt\n" + "".join(f"size={PART_SIZES[t]}, type={t}\n" for t in part_types),
            text=True,
        )

    for node in target_parts:
        if not wait_for_node(node):
            raise InstallError(f"/dev/{node} did not appear, reboot and rerun with --keep-partitions")
    parts = partitions_of(target)
    types = [part_type for part_type, _ in parts]
    if types != part_types:
        raise InstallError(f"unexpected partition layout on /dev/{target}: {' '.join(types)}")
    esp_partuuid = parts[-2][1]
    root_partuuid = parts[-1][1]

    log("creating file systems")
    run(["mkfs.vfat", "-F", "32", "-n", "ESP", f"/dev/{target_esp}"], stdout=subprocess.DEVNULL)
    run(["mke2fs", "-F", "-q", "-t", "ext2", "-E", "nodiscard", "-L", "managarm_rootfs", f"/dev/{target_root}"])

    log(f"mounting /dev/{target_root} on {root}")
    os.makedirs(root, exist_ok=True)
    run(["mount", "-t", "ext2", f"/dev/{target_root}", root])

    log("installing packages, this takes a while")
    account_args = ["--user", args.user, "--groups", args.user, "wheel,users"]
    if password_hash is not None:
        account_args += ["--password-hash", args.user, password_hash]
    populate_root(root, arch, args.xbps_channel, packages, account_args, args.repository)

    write_partition_config(root, root_partuuid, esp_partuuid)

    log("installing the boot files")
    # The installed update-boot.py picks the boot entries that match the installed packages.
    run([sys.executable, os.path.join(root, "usr/managarm/bin/update-boot.py"), "update-esp", "--root", root])

    # There is no umount(2) on Managarm yet; update-boot.py's final sync wrote everything back.
    log("done; remove the installation medium and reboot")


def main():
    parser = argparse.ArgumentParser(description="Install Managarm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    xbps_parser = argparse.ArgumentParser(add_help=False)
    xbps_group = xbps_parser.add_mutually_exclusive_group()
    xbps_group.add_argument(
        "--xbps-stable", dest="xbps_channel", action="store_const", const="stable",
        default="stable", help="Install from the stable xbps repository (default)",
    )
    xbps_group.add_argument(
        "--xbps-nightly", dest="xbps_channel", action="store_const", const="nightly",
        help="Install from the nightly xbps repository",
    )

    sysroot_parser = subparsers.add_parser(
        "sysroot", parents=[xbps_parser],
        help="Install base and limine into a directory on the build host, as for the live image",
    )
    sysroot_parser.add_argument(
        "--arch", default="x86_64", help="Target architecture (default: x86_64)"
    )
    sysroot_parser.add_argument("root", metavar="ROOT", help="Directory to install into")
    sysroot_parser.set_defaults(func=cmd_sysroot)

    disk_parser = subparsers.add_parser(
        "disk", parents=[xbps_parser],
        help="Install onto a disk from within a running Managarm system; ALL DATA ON IT IS DESTROYED",
    )
    disk_parser.add_argument(
        "-R", "--repository", action="append", default=[], metavar="URL",
        help="Additional xbps repository for this installation only; can be given multiple times",
    )
    disk_parser.add_argument(
        "-p", "--package", dest="packages", action="append", default=[], metavar="PKG",
        help="Package to install in addition to the ones needed to boot; can be given multiple times",
    )
    disk_parser.add_argument(
        "-m", "--mountpoint", default="/mnt/managarm-install", metavar="DIR",
        help="Where to mount the new root (default: /mnt/managarm-install)",
    )
    disk_parser.add_argument(
        "-k", "--keep-partitions", action="store_true",
        help="Reuse the partitions of a previous run instead of repartitioning",
    )
    disk_parser.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation or a password")
    disk_parser.add_argument("--user", default="managarm", help="Login user to create (default: managarm)")
    disk_parser.add_argument(
        "--password-hash", metavar="HASH",
        help="crypt(3) hash of the user's password; asked for interactively unless --yes is given",
    )
    disk_parser.add_argument("disk", metavar="DISK", help="Disk to install onto, e.g. /dev/nvme0n1")
    disk_parser.set_defaults(func=cmd_disk)

    args = parser.parse_args()
    try:
        args.func(args)
    except InstallError as e:
        print(f"managarm-install: error: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"managarm-install: error: {' '.join(e.cmd)} failed with status {e.returncode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

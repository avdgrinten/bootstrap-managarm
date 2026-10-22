#!/usr/bin/env python3

# Installs Managarm from an xbps repository that provision-sysroot.py sets up.
# The host (CI's image builds) and the target run this script, so it only uses the stdlib.

import argparse
import os
import shutil
import subprocess
import sys

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


class InstallError(Exception):
    pass


def run(cmd, **kwargs):
    return subprocess.run([str(arg) for arg in cmd], check=True, **kwargs)


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


def xbps_install(root, packages, *, sync, target_arch=None, repositories=()):
    # The host's xbps may have been built with another sysconfdir, so name the root's config explicitly.
    conf_dir = os.path.abspath(os.path.join(root, "etc/xbps.d"))
    cmd = [xbps_tool("xbps-install"), "-y", "-r", root, "-C", conf_dir]
    if sync:
        cmd.append("-S")
    for repository in repositories:
        cmd += ["-R", repository]
    env = dict(os.environ)
    if target_arch:
        env["XBPS_TARGET_ARCH"] = target_arch
    run(cmd + packages, env=env)


def cmd_sysroot(args):
    root = args.root

    # xbps-install takes the repository and its signing key from the root, so this goes first.
    provision(root, "--arch", args.arch, f"--xbps-{args.xbps_channel}")
    xbps_install(root, ["base", "limine"], sync=True, target_arch=args.arch)
    provision(root, *LIVE_ACCOUNTS)


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

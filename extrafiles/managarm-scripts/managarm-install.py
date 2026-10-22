#!/usr/bin/env python3

# Installs Managarm from an xbps repository that provision-sysroot.py sets up.
# The host (CI's image builds) and the target run this script, so it only uses the stdlib.

import argparse
import os
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


def provision(root, *args):
    subprocess.run([sys.executable, PROVISION, "apply", "--root", root, *args], check=True)


def cmd_sysroot(args):
    root = args.root

    # xbps-install takes the repository and its signing key from the root, so this goes first.
    provision(root, "--arch", args.arch, f"--xbps-{args.xbps_channel}")
    # The host's xbps may have been built with another sysconfdir, so name the root's config explicitly.
    conf_dir = os.path.abspath(os.path.join(root, "etc/xbps.d"))

    xbps_install = os.path.expanduser("~/.xbstrap/bin/xbps-install")
    env = {**os.environ, "XBPS_TARGET_ARCH": args.arch}

    subprocess.run(
        [xbps_install, "-y", "-S", "-r", root, "-C", conf_dir, "base", "limine"],
        env=env,
        check=True,
    )

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
    args.func(args)


if __name__ == "__main__":
    main()

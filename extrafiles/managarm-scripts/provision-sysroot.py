#!/usr/bin/env python3

# Provisions a Managarm root: base directories, users, sshd and the xbps repository.
# Each option only adds to what the packages ship, and rerunning with the same options is a no-op.
# The host (xbstrap, managarm-install.py sysroot) and the target run this script, so it only uses the stdlib.

import argparse
import os
import re
import subprocess
import sys
import time

ARCHES = ["x86_64", "aarch64", "riscv64"]
FIRST_UID = 1000
NAME_RE = re.compile(r"[a-z_][a-z0-9_-]*")

XBPS_REPOSITORIES = {
    "stable": {
        "x86_64": "https://repos.managarm.org/repos/amd64/",
        "aarch64": "https://repos.managarm.org/repos/aarch64/",
        "riscv64": "https://repos.managarm.org/repos/riscv64/",
    },
    "nightly": {
        "x86_64": "https://builds.managarm.org/projects/managarm/success/repo/packages/x86_64/",
        "aarch64": "https://builds.managarm.org/projects/managarm_aarch64/success/repo/packages/aarch64/",
        "riscv64": "https://builds.managarm.org/projects/managarm_riscv64/success/repo/packages/riscv64/",
    },
}

# Next to the script both in extrafiles/managarm-scripts and in /usr/managarm/lib/managarm-scripts; /usr/managarm/bin only has links.
XBPS_KEYS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "xbps-keys")

# Field counts of the colon-separated files in /etc, and which of their fields hold numeric ids.
TABLE_FIELDS = {"passwd": 7, "group": 4, "shadow": 9, "gshadow": 4}
TABLE_ID_FIELDS = {"passwd": [2, 3], "group": [2]}

# xbps can only ship directories by shipping files in them, which fails below mount points, and
# it creates directories with mode 0755.
BASE_DIRS = {
    "boot": 0o755,
    "dev": 0o755,
    "proc": 0o755,
    "root": 0o700,
    "run": 0o755,
    "sys": 0o755,
    "tmp": 0o1777,
}


class ProvisionError(Exception):
    pass


# Ownership can only be set when running as root; otherwise files keep the caller's ids.
def maybe_chown(path, uid, gid):
    if os.geteuid() == 0:
        os.chown(path, uid, gid, follow_symlinks=False)


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvisionError(f"{path} is not UTF-8 text")


def write_file(path, content, *, mode):
    data = content.encode("utf-8")
    # Leave up-to-date files alone so that rerunning does not touch mtimes.
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return
    except FileNotFoundError:
        pass

    try:
        st = os.stat(path)
        mode = st.st_mode & 0o7777
        owner = (st.st_uid, st.st_gid)
    except FileNotFoundError:
        owner = None

    make_dir(os.path.dirname(path), mode=0o755)
    tmp = path + ".provision-new"
    # The final mode applies before any content is written, and the content is durable before the
    # rename, so that a crash leaves either the old or the complete new file.
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        os.fchmod(f.fileno(), mode)
        if owner and os.geteuid() == 0:
            os.fchown(f.fileno(), *owner)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def make_dir(path, *, mode, owner=None):
    if os.path.isdir(path):
        return
    # Explicit modes, so that the caller's umask does not leak into the root.
    parent = os.path.dirname(path)
    if parent:
        make_dir(parent, mode=0o755)
    os.mkdir(path)
    os.chmod(path, mode)
    if owner:
        maybe_chown(path, *owner)


# A colon-separated file in /etc, such as passwd or shadow.
class Table:
    __slots__ = ("path", "rows")

    def __init__(self, root, name):
        self.path = os.path.join(root, "etc", name)
        try:
            lines = read_text(self.path).splitlines()
        except FileNotFoundError:
            raise ProvisionError(f"{self.path} does not exist")

        self.rows = []
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            row = line.split(":")
            # Rewriting the file is only safe for plain entries, not for comments or NIS "+" lines.
            if (
                len(row) != TABLE_FIELDS[name]
                or not row[0]
                or not all(row[i].isdigit() for i in TABLE_ID_FIELDS.get(name, []))
            ):
                raise ProvisionError(f"{self.path}:{n}: unsupported entry {line!r}")
            self.rows.append(row)

    def find(self, name):
        for row in self.rows:
            if row[0] == name:
                return row
        return None

    def save(self):
        write_file(self.path, "".join(":".join(row) + "\n" for row in self.rows), mode=0o644)


class Accounts:
    __slots__ = ("root", "passwd", "group", "shadow", "gshadow")

    def __init__(self, root):
        self.root = root
        self.passwd = Table(root, "passwd")
        self.group = Table(root, "group")
        self.shadow = Table(root, "shadow")
        self.gshadow = Table(root, "gshadow")

    def save(self):
        for table in [self.passwd, self.group, self.shadow, self.gshadow]:
            table.save()

    def get_user(self, name):
        row = self.passwd.find(name)
        if not row:
            raise ProvisionError(f"user {name!r} does not exist, pass --user {name}")
        return row

    def home_of(self, name):
        row = self.get_user(name)
        return os.path.join(self.root, row[5].lstrip("/")), (int(row[2]), int(row[3]))

    def add_user(self, name):
        row = self.passwd.find(name)
        created = row is None
        if created:
            # The user's group shares its id, so skip ids that are taken in either table.
            used = {int(row[2]) for row in self.passwd.rows} | {int(row[2]) for row in self.group.rows}
            uid = FIRST_UID
            while uid in used:
                uid += 1
            if self.group.find(name):
                raise ProvisionError(f"group {name!r} already exists")
            row = [name, "x", str(uid), str(uid), "", f"/home/{name}", "/bin/bash"]
            self.passwd.rows.append(row)

        # The tables are saved one after another; fill in the rows that an interrupted run did not write.
        gid = row[3]
        if not any(group[2] == gid for group in self.group.rows):
            self.group.rows.append([name, "x", gid, ""])
        if self.group.find(name) and not self.gshadow.find(name):
            self.gshadow.rows.append([name, "x", "", ""])
        if not self.shadow.find(name):
            # Locked until a password hash is set.
            self.shadow.rows.append([name, "!", str(days_since_epoch()), "0", "99999", "7", "", "", ""])

        if created:
            home, owner = self.home_of(name)
            make_dir(home, mode=0o755, owner=owner)
            # Packages may have created the home directory already.
            maybe_chown(home, *owner)

    def add_to_groups(self, name, groups):
        self.get_user(name)
        for group in groups:
            row = self.group.find(group)
            if not row:
                raise ProvisionError(f"group {group!r} does not exist")
            add_member(row, 3, name)
            row = self.gshadow.find(group)
            if row:
                add_member(row, 3, name)

    def set_password_hash(self, name, pw_hash):
        self.get_user(name)
        if not pw_hash or any(c in pw_hash for c in ":\n"):
            raise ProvisionError(f"invalid password hash for {name!r}")
        row = self.shadow.find(name)
        if not row:
            raise ProvisionError(f"user {name!r} has no entry in {self.shadow.path}")
        if row[1] != pw_hash:
            row[1] = pw_hash
            row[2] = str(days_since_epoch())


def add_member(row, idx, name):
    members = [m for m in row[idx].split(",") if m]
    if name not in members:
        members.append(name)
    row[idx] = ",".join(members)


def days_since_epoch():
    # Honor SOURCE_DATE_EPOCH so that image builds stay reproducible.
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return int(time.time()) // 86400
    if not epoch.isdigit():
        raise ProvisionError(f"invalid SOURCE_DATE_EPOCH {epoch!r}")
    return int(epoch) // 86400


def enable_sshd_password_auth(root):
    # The openssh package turns both off; sshd uses the first value it reads, and the drop-ins come first.
    write_file(
        os.path.join(root, "etc/ssh/sshd_config.d/50-password-auth.conf"),
        "# Written by provision-sysroot.py --sshd-password-auth.\n"
        "PasswordAuthentication yes\n"
        "KbdInteractiveAuthentication yes\n",
        mode=0o644,
    )


def set_xbps_repository(root, channel, arch):
    # Shares its name with the xbps package's default in /usr/share/xbps.d, which it overrides.
    write_file(
        os.path.join(root, "etc/xbps.d/00-repository-main.conf"),
        f"repository={XBPS_REPOSITORIES[channel][arch]}\n",
        mode=0o644,
    )
    # The keys sign both the stable and the nightly repositories; xbps looks them up by the fingerprint in the file name.
    keys = sorted(f for f in os.listdir(XBPS_KEYS_DIR) if f.endswith(".plist"))
    if not keys:
        raise ProvisionError(f"{XBPS_KEYS_DIR} does not contain any keys")
    for key in keys:
        write_file(os.path.join(root, "var/db/xbps/keys", key), read_text(os.path.join(XBPS_KEYS_DIR, key)), mode=0o644)


def make_base_dirs(root):
    for name, mode in BASE_DIRS.items():
        path = os.path.join(root, name)
        make_dir(path, mode=mode)
        # Packages that ship files below the directory may have created it already.
        if not os.path.ismount(path) and os.stat(path).st_mode & 0o7777 != mode:
            os.chmod(path, mode)


def check_name(name):
    if not NAME_RE.fullmatch(name):
        raise ProvisionError(f"invalid user or group name {name!r}")
    return name


def cmd_apply(args):
    if args.base_dirs:
        make_base_dirs(args.root)

    # Package installation runs after a provisioning pass that sets up xbps only, before /etc/passwd exists.
    if args.sysusers:
        subprocess.run(["systemd-sysusers", f"--root={args.root}"], check=True)

    if args.user or args.groups or args.password_hash:
        accounts = Accounts(args.root)
        for name in args.user:
            accounts.add_user(check_name(name))
        for name, groups in args.groups:
            accounts.add_to_groups(check_name(name), [check_name(g) for g in groups.split(",") if g])
        for name, pw_hash in args.password_hash:
            accounts.set_password_hash(check_name(name), pw_hash)
        accounts.save()

    if args.sshd_password_auth:
        enable_sshd_password_auth(args.root)
    if args.xbps_channel:
        set_xbps_repository(args.root, args.xbps_channel, args.arch)


def main():
    parser = argparse.ArgumentParser(description="Provision a Managarm root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser(
        "apply", help="Add base directories, users, sshd settings and the xbps repository"
    )
    apply_parser.add_argument("--root", required=True, help="Root of the installation")
    apply_parser.add_argument(
        "--base-dirs", action="store_true",
        help="Create the directories that packages cannot ship, such as mount points, and fix their modes",
    )
    apply_parser.add_argument(
        "--user", action="append", default=[], metavar="NAME",
        help="Create a login user with a locked password unless it exists",
    )
    apply_parser.add_argument(
        "--groups", action="append", default=[], nargs=2, metavar=("NAME", "GROUPS"),
        help="Add a user to a comma-separated list of existing groups",
    )
    apply_parser.add_argument(
        "--password-hash", action="append", default=[], nargs=2, metavar=("NAME", "HASH"),
        help="Set the crypt(3) password hash of a user (root included)",
    )
    apply_parser.add_argument(
        "--sshd-password-auth", action="store_true",
        help="Allow password logins over SSH",
    )
    apply_parser.add_argument(
        "--sysusers", action="store_true",
        help="Create system users and groups from sysusers.d files",
    )
    apply_parser.add_argument("--arch", choices=ARCHES, help="Target architecture (needed for the xbps repository)")
    xbps_group = apply_parser.add_mutually_exclusive_group()
    xbps_group.add_argument(
        "--xbps-stable", dest="xbps_channel", action="store_const", const="stable",
        help="Use the stable xbps repository and trust its signing key",
    )
    xbps_group.add_argument(
        "--xbps-nightly", dest="xbps_channel", action="store_const", const="nightly",
        help="Use the nightly xbps repository and trust its signing key",
    )
    apply_parser.set_defaults(func=cmd_apply)

    args = parser.parse_args()

    if args.command == "apply" and args.xbps_channel and not args.arch:
        apply_parser.error(f"--xbps-{args.xbps_channel} requires --arch")

    try:
        args.func(args)
    except (ProvisionError, OSError, subprocess.CalledProcessError) as e:
        print(f"provision-sysroot: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

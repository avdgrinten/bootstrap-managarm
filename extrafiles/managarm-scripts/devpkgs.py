#!/usr/bin/env python3

# Replaces packages with development builds from a build machine's xbstrap repository, and reverts them.
# The build machine serves its xbps-repo directory over HTTP, e.g., python3 -m http.server -d build/xbps-repo,
# and /etc/managarm/devpkgs.toml points to it: remote = "http://192.168.1.10:8000".

import argparse
import http.client
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request

CONFIG_FILE = "/etc/managarm/devpkgs.toml"
# The build machine's index, only used to find the files of the requested packages.
INDEX_DIR = "/var/lib/devpkgs/index"
# xbps only accepts unsigned repositories that are local, so the packages are downloaded into one.
# It only contains the requested packages: xbps also updates reverse dependencies and xbps itself
# from every repository that it sees, which would pull in builds that were never downloaded.
# The pkgdb records this path as the repository of the development builds, which is how they are found again.
REPO_DIR = "/var/lib/devpkgs/repo"
DOWNLOAD_TIMEOUT = 60


class DevpkgsError(Exception):
    pass


def log(message):
    print(f"devpkgs: {message}", flush=True)


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def load_remote():
    try:
        with open(CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        raise DevpkgsError(f"{CONFIG_FILE} does not exist")
    except tomllib.TOMLDecodeError as e:
        raise DevpkgsError(f"{CONFIG_FILE}: {e}")
    if "remote" not in config:
        raise DevpkgsError(f"{CONFIG_FILE}: remote is missing")
    if not isinstance(config["remote"], str):
        raise DevpkgsError(f"{CONFIG_FILE}: remote must be a string")
    return config["remote"]


def recreate_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)


def download(url, path):
    log(f"downloading {url}")
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, open(tmp, "wb") as f:
            shutil.copyfileobj(response, f)
            # http.client returns short data instead of raising when the connection drops early.
            expected = response.headers.get("Content-Length")
            if expected is not None and f.tell() != int(expected):
                raise DevpkgsError(f"cannot download {url}: got {f.tell()} of {expected} bytes")
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise DevpkgsError(f"cannot download {url}: {e}")
    os.rename(tmp, path)


def xbps_arch():
    return run(["xbps-uhelper", "arch"], stdout=subprocess.PIPE, text=True).stdout.strip()


def remote_property(pkg, prop):
    res = subprocess.run(
        ["xbps-query", "-i", f"--repository={INDEX_DIR}", "-p", prop, pkg],
        stdout=subprocess.PIPE, text=True,
    )
    if res.returncode or not res.stdout.strip():
        raise DevpkgsError(f"{pkg} is not in the repository of the build machine")
    return res.stdout.strip()


def upstream_has(pkg):
    res = subprocess.run(["xbps-query", "-R", "-p", "pkgver", pkg], stdout=subprocess.DEVNULL)
    return res.returncode == 0


# Maps the names of the installed development builds to their pkgvers.
def dev_packages():
    # -s -p prints every case-insensitive substring match of the property, hence the exact comparison.
    res = run(["xbps-query", "-s", REPO_DIR, "-p", "repository"], stdout=subprocess.PIPE, text=True)
    pkgs = {}
    for line in res.stdout.splitlines():
        pkgver, _, value = line.partition(": ")
        if value == REPO_DIR:
            pkgs[pkgver.rpartition("-")[0]] = pkgver
    return pkgs


# A repolocked package is only updated from the repository that it was installed from. REPO_DIR is not
# configured, so this keeps xbps from replacing development builds, whose versions are commit counts of a branch.
# Unlike hold, repolock also applies to reverse dependency updates and survives reinstallation.
def set_repolock(pkgs, lock):
    if pkgs:
        run(["xbps-pkgdb", "-m", "repolock" if lock else "repounlock", *pkgs])


def cmd_use(args):
    remote = load_remote()

    arch = xbps_arch()
    recreate_dir(INDEX_DIR)
    recreate_dir(REPO_DIR)

    base = remote.rstrip("/") + "/"
    download(base + f"{arch}-repodata", os.path.join(INDEX_DIR, f"{arch}-repodata"))
    binpkgs = []
    for pkg in args.packages:
        # noarch packages carry that instead of the machine's architecture in their file name.
        fname = f"{remote_property(pkg, 'pkgver')}.{remote_property(pkg, 'architecture')}.xbps"
        download(base + fname, os.path.join(REPO_DIR, fname))
        binpkgs.append(os.path.join(REPO_DIR, fname))
    run(["xbps-rindex", "-a", *binpkgs])

    # -i keeps dependencies from being taken from the upstream repository, and -f reinstalls equal or older versions.
    try:
        run(["xbps-install", "-i", f"--repository={REPO_DIR}", "-yf", *args.packages])
    finally:
        set_repolock(sorted(dev_packages()), True)

    for path in binpkgs:
        os.unlink(path)


def cmd_drop(args):
    dev = dev_packages()
    if args.all:
        pkgs = sorted(dev)
        if not pkgs:
            log("no development builds are installed")
            return
    else:
        unknown = [p for p in args.packages if p not in dev]
        if unknown:
            raise DevpkgsError(f"not development builds: {', '.join(unknown)}")
        pkgs = args.packages

    run(["xbps-install", "-S"])
    missing = [p for p in pkgs if not upstream_has(p)]
    if missing:
        raise DevpkgsError(f"not in the configured repositories, remove them with xbps-remove: {', '.join(missing)}")

    # Without --repository, xbps only sees the configured repositories, and -f also takes older versions.
    set_repolock(pkgs, False)
    try:
        run(["xbps-install", "-yf", *pkgs])
    finally:
        dev = dev_packages()
        set_repolock([p for p in pkgs if p in dev], True)


def cmd_list(args):
    for pkgver in sorted(dev_packages().values()):
        print(pkgver)


def main():
    parser = argparse.ArgumentParser(description="Use development builds of packages from a build machine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    use_parser = subparsers.add_parser(
        "use",
        help="Install the newest development builds of packages, or update them",
    )
    use_parser.add_argument("packages", nargs="+", metavar="PACKAGE")
    use_parser.set_defaults(func=cmd_use)

    drop_parser = subparsers.add_parser(
        "drop",
        help="Replace development builds by the packages of the configured repositories",
    )
    drop_parser.add_argument("--all", action="store_true", help="Drop all development builds")
    drop_parser.add_argument("packages", nargs="*", metavar="PACKAGE")
    drop_parser.set_defaults(func=cmd_drop)

    list_parser = subparsers.add_parser("list", help="List the installed development builds")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()

    if args.command == "drop" and args.all == bool(args.packages):
        drop_parser.error("pass either packages or --all")

    try:
        if args.command != "list" and os.geteuid() != 0:
            raise DevpkgsError("must be run as root")
        args.func(args)
    except DevpkgsError as e:
        print(f"devpkgs: error: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"devpkgs: error: {' '.join(e.cmd)} failed with status {e.returncode}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"devpkgs: error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

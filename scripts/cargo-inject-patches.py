#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tomllib

REGISTRY_PREFIX = "registry+"
GIT_PREFIX = "git+"

PACKAGE_PATCHES = pathlib.Path("extrafiles") / "cargo-patches.toml"


def fail(message):
    print(f"cargo-inject-patches: error: {message}", file=sys.stderr)
    sys.exit(1)


def compat_class(version):
    """Cargo's caret-compatibility class: versions sharing one are interchangeable."""
    core = version.split("+")[0].split("-")[0]
    parts = [int(x) for x in core.split(".")] + [0, 0]
    major, minor, patch = parts[0], parts[1], parts[2]
    if major:
        return (major,)
    if minor:
        return (0, minor)
    return (0, 0, patch)


def canonical_url(url):
    """A git URL without what Cargo ignores when it matches [patch] tables."""
    url = url.rstrip("/")
    return url.removesuffix(".git")


def table_source(source):
    """The patched source of a [patch] table: "crates-io" or the URL of a git repository."""
    if source == "crates-io":
        return source
    return canonical_url(source)


def read_patches(tables):
    """Yield (table name, key, crate name, spec) for each entry of the given [patch] tables."""
    for source, patches in tables.items():
        for key, spec in patches.items():
            # The key is only a rename; "package" carries the real crate name.
            yield source, key, spec.get("package", key), spec


def port_version(key, path, hint=""):
    port = os.path.join(path, "Cargo.toml")
    if not os.path.exists(port):
        fail(f"patch `{key}` points at {path}, which has no Cargo.toml{hint}")

    with open(port, "rb") as f:
        version = tomllib.load(f).get("package", {}).get("version")
    if not isinstance(version, str):
        fail(f"patch `{key}` has a non-literal version, which we cannot resolve")
    return version


def load_patch_table():
    """Map (patched source, crate name) -> compatibility class -> the port that supersedes it,
    for the [patch] tables of CARGO_HOME, which apply to all packages."""
    cargo_home = os.environ.get("CARGO_HOME")
    if not cargo_home:
        fail("CARGO_HOME is not set, cannot locate the [patch] tables")

    config = os.path.join(cargo_home, "config.toml")
    if not os.path.exists(config):
        fail(f"{config} does not exist")

    with open(config, "rb") as f:
        tables = tomllib.load(f).get("patch", {})

    table = {}
    for source, key, name, spec in read_patches(tables):
        version = port_version(key, spec["path"])
        table.setdefault((table_source(source), name), {})[compat_class(version)] = (version, spec["path"])
    return table


def load_package_table():
    """Map (patched source, crate name) -> compatibility class -> (table name, key, spec), for the
    [patch] tables of PACKAGE_PATCHES, which only apply to packages whose lockfile uses them.

    The entries carry the version of their port, such that matching them against a lockfile
    does not need the port to be checked out."""
    source_root = os.environ.get("XBSTRAP_SOURCE_ROOT")
    if not source_root:
        fail("XBSTRAP_SOURCE_ROOT is not set, cannot locate the source root")

    with open(os.path.join(source_root, PACKAGE_PATCHES), "rb") as f:
        tables = tomllib.load(f).get("patch", {})

    table = {}
    for source, key, name, spec in read_patches(tables):
        if not isinstance(spec.get("path"), str):
            fail(f"patch `{key}` in {PACKAGE_PATCHES} has no path")
        if not isinstance(spec.get("version"), str):
            fail(f"patch `{key}` in {PACKAGE_PATCHES} has no version")
        spec = dict(spec, path=spec["path"].replace("@SOURCE_ROOT@", source_root))
        table.setdefault((table_source(source), name), {})[compat_class(spec["version"])] = (source, key, spec)
    return table


def load_lock(lockfile):
    with open(lockfile, "rb") as f:
        return tomllib.load(f).get("package", [])


def patched_source(source):
    """The [patch] table that can supersede a lock entry from `source`, if any."""
    if source.startswith(REGISTRY_PREFIX):
        return "crates-io"
    if source.startswith(GIT_PREFIX):
        # git+<url>?<branch, tag or rev>#<commit>
        return canonical_url(source.removeprefix(GIT_PREFIX).split("#")[0].split("?")[0])
    return None


def unpatched(lockfile, table):
    """Lock entries still coming from a registry or a git repository that a patched port
    supersedes."""
    pending = []
    for pkg in load_lock(lockfile):
        source = patched_source(pkg.get("source", ""))
        if source is None:
            continue
        port = table.get((source, pkg["name"]), {}).get(compat_class(pkg["version"]))
        if port:
            pending.append((pkg["name"], pkg["version"], port))
    return pending


def select(lockfile, table, members):
    """The entries of a package table that lock entries use, whether or not they are applied yet."""
    selected = {}
    for pkg in load_lock(lockfile):
        source = pkg.get("source")
        if source is None:
            if (pkg["name"], pkg["version"]) in members:
                continue
            # An applied patch is a path dependency, which the lockfile records without a source.
            sources = [s for s, name in table if name == pkg["name"]]
        else:
            sources = [patched_source(source)]
        for s in sources:
            entry = table.get((s, pkg["name"]), {}).get(compat_class(pkg["version"]))
            if entry:
                selected[entry[:2]] = entry
    return list(selected.values())


def write_config(path, patches):
    tables = {}
    for source, key, spec in patches:
        fields = {"path": spec["path"]}
        if "package" in spec:
            fields["package"] = spec["package"]
        tables.setdefault(source, []).append((key, fields))

    # JSON strings are valid TOML basic strings.
    lines = []
    for source, entries in tables.items():
        lines.append(f"[patch.{json.dumps(source, ensure_ascii=False)}]")
        for key, fields in entries:
            inline = ", ".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())
            lines.append(f"{json.dumps(key, ensure_ascii=False)} = {{ {inline} }}")
        lines.append("")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines))


def workspace_members(manifest, lockfile_args):
    """(name, version) of the workspace's own packages, which the lockfile records without a
    source, just like applied patches."""
    # --no-deps does not resolve, so this neither needs the patches nor touches the lockfile.
    metadata = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1",
         "--manifest-path", manifest, *lockfile_args],
        capture_output=True,
    )
    if metadata.returncode:
        fail(f"cargo metadata failed:\n{metadata.stderr.decode('utf-8')}")
    return {(pkg["name"], pkg["version"]) for pkg in json.loads(metadata.stdout)["packages"]}


parser = argparse.ArgumentParser(description="Inject patched Rust libraries into Cargo lockfiles")
parser.add_argument("manifest", type=pathlib.Path, help="path to Cargo.toml")
parser.add_argument(
    "--lockfile-path",
    type=pathlib.Path,
    help="lockfile to use instead of the workspace's (passed to cargo as --lockfile-path)",
)
parser.add_argument(
    "--cargo-config",
    type=pathlib.Path,
    help=f"cargo config to write the patches of {PACKAGE_PATCHES.name} that the lockfile uses into",
)
args = parser.parse_args()
manifest = args.manifest

table = load_patch_table()
package_table = load_package_table()

# The config of an earlier run must not leak into the cargo invocations below.
if args.cargo_config and args.cargo_config.exists():
    args.cargo_config.unlink()

if args.lockfile_path:
    lockfile = args.lockfile_path
    lockfile_args = ["-Zunstable-options", "--lockfile-path", lockfile]
else:
    # The lockfile lives next to the workspace root, which need not be the given manifest.
    root = subprocess.run(
        ["cargo", "locate-project", "--workspace", "--message-format", "plain",
         "--manifest-path", manifest],
        capture_output=True,
    )
    if root.returncode:
        fail(f"cargo locate-project failed:\n{root.stderr.decode('utf-8')}")
    lockfile = os.path.join(os.path.dirname(root.stdout.decode("utf-8").strip()), "Cargo.lock")
    lockfile_args = []

if not os.path.exists(lockfile):
    # Resolving from scratch already applies the patches of CARGO_HOME, but the ones of
    # PACKAGE_PATCHES are only selected from the result and applied by the update pass.
    print("cargo-inject-patches: no lockfile, generating one...")
    generate = subprocess.run(
        ["cargo", "generate-lockfile", "--manifest-path", manifest, *lockfile_args],
        capture_output=True,
    )
    if generate.returncode:
        fail(f"cargo generate-lockfile failed:\n{generate.stderr.decode('utf-8')}")

config_args = []
patches = select(lockfile, package_table, workspace_members(manifest, lockfile_args))
if patches:
    if not args.cargo_config:
        fail(f"the lockfile uses crates that {PACKAGE_PATCHES.name} patches, but no --cargo-config "
             "was given to write the patches into")

    for source, key, spec in patches:
        hint = ", make sure that the source containing it is in sources_required"
        version = port_version(key, spec["path"], hint)
        if compat_class(version) != compat_class(spec["version"]):
            fail(f"patch `{key}` in {PACKAGE_PATCHES} claims version {spec['version']}, "
                 f"but {spec['path']} is at {version}")
        name = spec.get("package", key)
        table.setdefault((table_source(source), name), {})[compat_class(version)] = (version, spec["path"])

    write_config(args.cargo_config, patches)
    config_args = ["--config", args.cargo_config]
    print(f"cargo-inject-patches: wrote {len(patches)} patches to {args.cargo_config}")

pending = unpatched(lockfile, table)

if not pending:
    print("cargo-inject-patches: patches already applied, nothing to do")
    sys.exit(0)

# Only ever update the patched crates by exact version: a bare `cargo update` would
# re-resolve the whole graph and drift every unrelated dependency off its pin.
cmd = ["cargo", *config_args, "update", "--manifest-path", manifest, *lockfile_args]
cmd += [f"-p{name}@{version}" for name, version, _ in pending]

output = subprocess.run(cmd, capture_output=True)
if output.returncode:
    fail(f"cargo update failed:\n{output.stderr.decode('utf-8')}")

# Cargo only warns when a patch does not apply and carries on with the unpatched crate,
# so re-reading the lockfile is the one reliable way to tell that it took effect.
stuck = unpatched(lockfile, table)
if stuck:
    fail("the following crates could not be patched, most likely because the port is "
         "older than what the dependency graph requires:\n"
         + "\n".join(f"  {name} {version} is not superseded by {port[0]} in {port[1]}"
                     for name, version, port in stuck))

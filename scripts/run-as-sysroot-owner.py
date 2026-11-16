#!/usr/bin/env python3

# Runs a command, dropping from root to the owner of the sysroot if the sysroot has no real ownership.
# Without sysroot_volume, the sysroot belongs to the build user, and files that root creates in it
# would belong to a subordinate ID on the host.

import os
import sys

if len(sys.argv) < 3:
    print(f"usage: {sys.argv[0]} SYSROOT COMMAND [ARGS...]", file=sys.stderr)
    sys.exit(2)

if os.geteuid() == 0:
    try:
        st = os.stat(os.path.join(sys.argv[1], "usr"))
    except FileNotFoundError:
        st = None
    if st and st.st_uid != 0:
        os.setgroups([])
        os.setgid(st.st_gid)
        os.setuid(st.st_uid)

os.execvp(sys.argv[2], sys.argv[2:])

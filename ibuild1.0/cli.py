#!/usr/bin/env python3
"""
Unified CLI for ibuild.
Wraps modules_py.* into one entrypoint with aliases and "all-in-one" pipeline.
"""

import argparse
import logging
import sys
from pathlib import Path

# Import modules
from ibuild.modules_py import build as build_mod
from ibuild.modules_py import dependency as dep_mod
from ibuild.modules_py import remove as remove_mod
from ibuild.modules_py import upgrade as upgrade_mod
from ibuild.modules_py.common import enter_sandbox, run_hook

logger = logging.getLogger("ibuild.cli")

def main():
    ap = argparse.ArgumentParser(prog="ibuild", description="Ibuild unified CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # build
    ap_build = sub.add_parser("build", aliases=["b"], help="Build a package")
    ap_build.add_argument("--meta", required=True)
    ap_build.add_argument("--source", required=True)
    ap_build.add_argument("--out", required=True)
    ap_build.add_argument("--verbose", action="store_true")

    # dependency
    ap_dep = sub.add_parser("dependency", aliases=["dep", "d"], help="Resolve dependencies")
    ap_dep.add_argument("--repo", required=True)
    ap_dep.add_argument("--resolve", required=True)
    ap_dep.add_argument("--format", choices=["json","dot"], default="json")
    ap_dep.add_argument("--plot", action="store_true")

    # remove
    ap_rm = sub.add_parser("remove", aliases=["rm"], help="Remove a package")
    ap_rm.add_argument("package")

    # upgrade
    ap_up = sub.add_parser("upgrade", aliases=["up"], help="Upgrade a package")
    ap_up.add_argument("package")

    # sandbox
    ap_sb = sub.add_parser("sandbox", aliases=["s"], help="Run command inside sandbox")
    ap_sb.add_argument("--cmd", required=True, nargs=argparse.REMAINDER)
    ap_sb.add_argument("--bind", action="append", default=[])
    ap_sb.add_argument("--mem", default="512M")

    # all-in-one
    ap_all = sub.add_parser("all", aliases=["a"], help="Run full pipeline (deps -> build -> install)")
    ap_all.add_argument("--meta", required=True)
    ap_all.add_argument("--source", required=True)
    ap_all.add_argument("--out", required=True)
    ap_all.add_argument("--repo", required=True)
    ap_all.add_argument("--resolve", required=True)
    ap_all.add_argument("--verbose", action="store_true")

    args = ap.parse_args()

    if args.cmd in ["build","b"]:
        sys.argv = ["ibuild-build"] + [f"--meta={args.meta}", f"--source={args.source}", f"--out={args.out}"]
        if args.verbose: sys.argv.append("--verbose")
        build_mod.main()
    elif args.cmd in ["dependency","dep","d"]:
        sys.argv = ["ibuild-dep"] + [f"--repo={args.repo}", f"--resolve={args.resolve}", f"--format={args.format}"]
        if args.plot: sys.argv.append("--plot")
        dep_mod.main()
    elif args.cmd in ["remove","rm"]:
        sys.argv = ["ibuild-remove", args.package]
        remove_mod.main()
    elif args.cmd in ["upgrade","up"]:
        sys.argv = ["ibuild-upgrade", args.package]
        upgrade_mod.main()
    elif args.cmd in ["sandbox","s"]:
        enter_sandbox(args.cmd, Path.cwd(), binds=args.bind, memory_max=args.mem)
    elif args.cmd in ["all","a"]:
        # 1. Resolve dependencies
        logger.info("Resolving dependencies...")
        sys.argv = ["ibuild-dep", f"--repo={args.repo}", f"--resolve={args.resolve}"]
        dep_mod.main()
        # 2. Build
        logger.info("Building...")
        sys.argv = ["ibuild-build", f"--meta={args.meta}", f"--source={args.source}", f"--out={args.out}"]
        if args.verbose: sys.argv.append("--verbose")
        build_mod.main()
        # 3. Fake install
        logger.info("Running fakeroot install...")
        run_hook("make install", Path(args.source), sandbox=True, binds=[args.source, args.out])
        logger.info("Pipeline complete.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

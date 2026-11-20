#!/usr/bin/env python3
"""Regression test for port_universal.py against real kernel trees.

For every tree given, with a pristine copy of drivers/scsi/sd.c and sd.h:

  1. port_universal.py lands the emulation without manual work (exit 0),
  2. a second run changes nothing (idempotent),
  3. the same run in --semantic-only mode (every piece inserted by anchor, GNU
     patch not used) gives the same sd.c and sd.h, blank lines aside,
  4. --emit-patch writes a unified diff that applies to the original files with
     fuzz 0,
  5. drivers/scsi/sd.o compiles from the ported files and produces no warning
     that the unmodified file did not produce.

The compile step runs inside the tree you name: the original sd.c and sd.h are
put back afterwards, but object files and a .config stay behind, so use a
tree you do not mind building in. It needs the usual kernel build tools (gcc,
make, bc, flex, bison, libssl-dev, libelf-dev, and libdw-dev for a Proxmox or Ubuntu config).

    check_trees.py --tree 6.8=/src/linux-6.8 --tree 6.12=/src/linux-6.12 \\
        --tree pve=/src/pve-ubuntu-kernel,patches=/src/pve-kernel/patches/kernel

Per tree: NAME=PATH[,patches=DIR][,config=FILE]
    patches   a directory of patches that were applied before ours in the real
              build (Proxmox's patches/kernel); those that touch sd.c or sd.h
              are replayed on the pristine copy first
    config    a .config to use; default is the tree's own .config, else
              `make defconfig` with SCSI and BLK_DEV_SD switched on

Options: --no-compile, --jobs N. Exit status 0 only if every tree passes.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "port_universal.py"
PATCH = HERE / "wvg-sd-528.patch"
FILES = ("drivers/scsi/sd.c", "drivers/scsi/sd.h")


def run(cmd, cwd=None, check=False):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode:
        raise RuntimeError("%s failed (%d):\n%s" % (" ".join(map(str, cmd)), p.returncode, p.stdout[-3000:]))
    return p


def copy_files(src, dst):
    for rel in FILES:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst / rel)


def digest(tree):
    return hashlib.sha256(b"".join((tree / r).read_bytes() for r in FILES)).hexdigest()


def squeeze(tree):
    """Both files with blank lines dropped (hunk 6 of the diff adds one cosmetic blank line)."""
    return [l for r in FILES for l in (tree / r).read_text().splitlines() if l.strip()]


def replay_pve_patches(patch_dir, tree):
    names = []
    for p in sorted(Path(patch_dir).glob("*.patch")):
        if "wvg-sd-528" in p.name:          # ours is the thing under test
            continue
        if re.search(r"^(\+\+\+|---) [ab]/drivers/scsi/sd\.[ch]", p.read_text(errors="replace"), re.M):
            r = run(["git", "apply", "--include=drivers/scsi/sd.[ch]", str(p)], cwd=tree)
            if r.returncode:
                raise RuntimeError("%s does not apply to sd.[ch]:\n%s" % (p.name, r.stdout))
            names.append(p.name)
    return names


def tool(tree, *extra):
    return run([sys.executable, str(TOOL), str(tree), "--patch", str(PATCH), *extra])


def compile_object(tree, config, jobs):
    """Return (returncode, warnings) of `make drivers/scsi/sd.o`."""
    if config:
        shutil.copy2(config, tree / ".config")
        # a distribution config points at signing keys, BTF and Rust tooling a test build does not have
        for opt in ("MODULE_SIG", "SYSTEM_TRUSTED_KEYS", "SYSTEM_REVOCATION_KEYS", "DEBUG_INFO_BTF",
                    "DEBUG_INFO_BTF_MODULES", "RUST"):
            run(["scripts/config", "--file", ".config", "-d", opt], cwd=tree, check=True)
        run(["scripts/config", "--file", ".config", "--set-str", "SYSTEM_TRUSTED_KEYS", ""], cwd=tree, check=True)
        run(["scripts/config", "--file", ".config", "--set-str", "SYSTEM_REVOCATION_KEYS", ""], cwd=tree, check=True)
    elif not (tree / ".config").exists():
        run(["make", "-s", "defconfig"], cwd=tree, check=True)
        for opt in ("SCSI", "BLK_DEV_SD", "BLOCK"):
            run(["scripts/config", "-e", opt], cwd=tree, check=True)
    run(["make", "-s", "olddefconfig"], cwd=tree, check=True)
    if not (tree / "include/generated/autoconf.h").exists() or not (tree / "scripts/mod/modpost").exists():
        run(["make", "-s", "-j%d" % jobs, "prepare"], cwd=tree, check=True)
    obj = tree / "drivers/scsi/sd.o"
    if obj.exists():
        obj.unlink()
    p = run(["make", "-j%d" % jobs, "drivers/scsi/sd.o"], cwd=tree)
    warnings = sorted(set(re.findall(r"^.*sd\.[ch]:\d+:\d+: warning: .*$", p.stdout, re.M)))
    if p.returncode:
        warnings.append("BUILD FAILED:\n" + "\n".join(p.stdout.splitlines()[-25:]))
    return p.returncode, warnings


def strip_lines(warnings):
    """Warnings without line numbers, so a shifted line is not 'new'."""
    return {re.sub(r"sd\.([ch]):\d+:\d+", r"sd.\1", w) for w in warnings}


def check_tree(name, path, patches, config, do_compile, jobs):
    tree = Path(path)
    fails = []
    out = []
    with tempfile.TemporaryDirectory(prefix="check528-") as tmp:
        tmp = Path(tmp)
        pristine, a, b, c = (tmp / n for n in ("pristine", "a", "b", "c"))
        for d in (pristine, a, b, c):
            d.mkdir()
        copy_files(tree, pristine)
        if patches:
            out.append("replayed patches touching sd: %s"
                       % (", ".join(replay_pve_patches(patches, pristine)) or "none"))
        for d in (a, b, c):
            copy_files(pristine, d)

        r = tool(a)
        gen = re.search(r"generation: (\w+)", r.stdout)
        out.append("generation %s" % (gen.group(1) if gen else "?"))
        if r.returncode:
            fails.append("port_universal.py exit %d:\n%s" % (r.returncode, r.stdout[-2500:]))
            return out, fails
        out.append("ported without manual work")

        before = digest(a)
        r2 = tool(a)
        if r2.returncode or digest(a) != before:
            fails.append("second run is not a no-op (exit %d)" % r2.returncode)
        else:
            out.append("second run changed nothing")

        rb = tool(b, "--semantic-only")
        if rb.returncode:
            fails.append("--semantic-only exit %d:\n%s" % (rb.returncode, rb.stdout[-2500:]))
        elif squeeze(a) != squeeze(b):
            import difflib
            d = list(difflib.unified_diff(squeeze(a), squeeze(b), "patch-landed", "anchor-only", lineterm="", n=1))
            fails.append("--semantic-only differs from the patch-landed result:\n" + "\n".join(d[:60]))
        else:
            out.append("anchor-only run gives the same code")

        rc = tool(c, "--emit-patch", str(tmp / "out.patch"))
        if rc.returncode or not (tmp / "out.patch").exists():
            fails.append("--emit-patch failed:\n" + rc.stdout[-1500:])
        else:
            out.append("emitted patch applies to the original files with fuzz 0 (%d lines)"
                       % len((tmp / "out.patch").read_text().splitlines()))
        if do_compile:
            keep = tmp / "keep"
            keep.mkdir()
            copy_files(tree, keep)
            try:
                copy_files(pristine, tree)
                rc0, w0 = compile_object(tree, config, jobs)
                copy_files(a, tree)
                rc1, w1 = compile_object(tree, config, jobs)
            finally:
                copy_files(keep, tree)
            if rc0:
                fails.append("the unmodified sd.c does not build here, test setup problem:\n" + "\n".join(w0))
            elif rc1:
                fails.append("ported sd.c does not build:\n" + "\n".join(w1))
            else:
                new = strip_lines(w1) - strip_lines(w0)
                if new:
                    fails.append("new compiler warnings:\n" + "\n".join(sorted(new)))
                else:
                    out.append("sd.o builds, no new warnings (%d before, %d after)" % (len(w0), len(w1)))
    return out, fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tree", action="append", required=True, metavar="NAME=PATH[,patches=DIR][,config=FILE]")
    ap.add_argument("--no-compile", action="store_true")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args()
    bad = 0
    for spec in a.tree:
        head, *opts = spec.split(",")
        name, _, path = head.partition("=")
        kv = dict(o.split("=", 1) for o in opts)
        print("== %s (%s)" % (name, path), flush=True)
        try:
            out, fails = check_tree(name, path, kv.get("patches"), kv.get("config"),
                                    not a.no_compile, a.jobs)
        except RuntimeError as exc:
            out, fails = [], [str(exc)]
        for line in out:
            print("   ok   " + line)
        for f in fails:
            print("   FAIL " + f.replace("\n", "\n        "))
        print("   => %s" % ("FAIL" if fails else "PASS"), flush=True)
        bad += bool(fails)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

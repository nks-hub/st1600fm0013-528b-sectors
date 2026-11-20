#!/usr/bin/env python3
"""Cut the wvg-sd-528 patch against a checked-out Proxmox (Ubuntu) kernel tree.

The result is the single unified diff Proxmox wants in
patches/kernel/9999-wvg-sd-528-translation.patch. It is produced by
port_universal.py on a scratch copy of drivers/scsi/sd.c and sd.h, so the
kernel tree itself is not touched, and it is proven to apply to the original
files with `patch --fuzz=0` before the script reports success.

If the tree needs a Proxmox patch that also touches sd.c or sd.h (they are
applied before ours), pass its directory with --pve-patches: those patches are
replayed on the scratch copy first, so the diff is cut against what the build
will see.

    rebase_pve_528_patch.py --kernel-tree submodules/ubuntu-kernel \\
        --pve-patches patches/kernel --output patches/kernel/9999-wvg-sd-528-translation.patch

Exit status 0 on success, 2 when a piece of the emulation could not be placed
(nothing is written then; see port_universal.py for what was missing).
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_universal  # noqa: E402

FILES = (port_universal.SD_C, port_universal.SD_H)


def replay(patch_dir, tree):
    for p in sorted(Path(patch_dir).glob("*.patch")):
        if "wvg-sd-528" in p.name:
            continue
        if re.search(r"^(\+\+\+|---) [ab]/drivers/scsi/sd\.[ch]", p.read_text(errors="replace"), re.M):
            r = subprocess.run(["git", "apply", "--include=drivers/scsi/sd.[ch]", str(p)], cwd=tree,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if r.returncode:
                raise SystemExit("%s does not apply to sd.[ch]:\n%s" % (p.name, r.stdout))
            print("replayed %s" % p.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kernel-tree", default="submodules/ubuntu-kernel",
                    help="checked-out kernel tree (only sd.c and sd.h are read)")
    ap.add_argument("--patch", default=str(HERE / "wvg-sd-528.patch"),
                    help="the original diff (default: the one next to this script)")
    ap.add_argument("--pve-patches", help="directory of patches applied before ours")
    ap.add_argument("--output", required=True, help="where to write the unified diff")
    a = ap.parse_args()

    tree = Path(a.kernel_tree).resolve()
    with tempfile.TemporaryDirectory(prefix="rebase-528-") as tmp:
        work = Path(tmp)
        for rel in FILES:
            if not (tree / rel).is_file():
                raise SystemExit("kernel source not found: %s" % (tree / rel))
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tree / rel, work / rel)
        if a.pve_patches:
            replay(a.pve_patches, work)
        sys.exit(port_universal.run(work, a.patch, emit=str(Path(a.output).resolve())))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Rebase the WVG 520/528 -> 512 SCSI sd patch onto the checked-out PVE kernel.

The script applies all hunks that still match, semantically relocates the known
sd.c hunks that changed in newer kernels, and emits a fresh unified patch that
is tested against the pristine checked-out files.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Hunk:
    header: str
    lines: list[str]


def die(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], *, cwd: Path | None = None, stdin: bytes | None = None,
        check: bool = False) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and proc.returncode:
        sys.stderr.buffer.write(proc.stdout)
        die(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


def parse_file_hunks(patch_text: str, wanted_path: str) -> list[Hunk]:
    lines = patch_text.splitlines(keepends=True)
    in_file = False
    hunks: list[Hunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- a/"):
            old_path = line[6:].split("\t", 1)[0].strip()
            in_file = old_path == wanted_path
            i += 1
            continue
        if in_file and line.startswith("@@ "):
            header = line
            body: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith("@@ ") or nxt.startswith("--- a/"):
                    break
                body.append(nxt)
                i += 1
            hunks.append(Hunk(header=header, lines=body))
            continue
        i += 1
    return hunks


def addition_runs(hunk: Hunk) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for line in hunk.lines:
        if line.startswith("+") and not line.startswith("+++"):
            current.append(line[1:])
        else:
            if current:
                runs.append("".join(current))
                current = []
    if current:
        runs.append("".join(current))
    return runs


def insert_after_exact(text: str, anchor: str, payload: str, marker: str) -> str:
    if marker in text:
        return text
    count = text.count(anchor)
    if count != 1:
        die(f"anchor expected once, found {count}: {anchor!r}")
    return text.replace(anchor, anchor + payload, 1)


def insert_before_regex(text: str, pattern: str, payload: str, marker: str,
                        description: str) -> str:
    if marker in text:
        return text
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if len(matches) != 1:
        die(f"{description}: expected one match, found {len(matches)}")
    pos = matches[0].start()
    return text[:pos] + payload + text[pos:]


def find_function_span(text: str, signature_pattern: str, description: str) -> tuple[int, int]:
    matches = list(re.finditer(signature_pattern, text, flags=re.MULTILINE))
    # Ignore forward declarations: require an opening brace after whitespace.
    valid: list[re.Match[str]] = []
    for match in matches:
        tail = text[match.end():]
        brace = re.match(r"\s*\{", tail)
        if brace:
            valid.append(match)
    if len(valid) != 1:
        die(f"{description}: expected one function definition, found {len(valid)}")

    start = valid[0].start()
    open_brace = text.find("{", valid[0].end())
    depth = 0
    in_string = False
    in_char = False
    escape = False
    in_line_comment = False
    in_block_comment = False
    i = open_brace
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
        elif in_block_comment:
            if c == "*" and n == "/":
                in_block_comment = False
                i += 1
        elif in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        elif in_char:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == "'":
                in_char = False
        else:
            if c == "/" and n == "/":
                in_line_comment = True
                i += 1
            elif c == "/" and n == "*":
                in_block_comment = True
                i += 1
            elif c == '"':
                in_string = True
            elif c == "'":
                in_char = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    die(f"{description}: unterminated function body")


def insert_queue_depth_call(text: str) -> str:
    marker = "sd_528_limit_queue_depth(sdkp);"
    if marker in text:
        return text
    start, end = find_function_span(
        text,
        r"^static\s+void\s+sd_revalidate_disk\s*\([^)]*\)",
        "sd_revalidate_disk",
    )
    body = text[start:end]
    guard = re.search(
        r"(?m)^(?P<indent>[ \t]*)if\s*\(!scsi_device_online\(sdp\)\)\s*\n"
        r"(?P=indent)[ \t]+(?:return;|goto\s+[A-Za-z_][A-Za-z0-9_]*;)\s*\n",
        body,
    )
    if not guard:
        # Also support one-line guard forms.
        guard = re.search(
            r"(?m)^(?P<indent>[ \t]*)if\s*\(!scsi_device_online\(sdp\)\)\s*"
            r"(?:return;|goto\s+[A-Za-z_][A-Za-z0-9_]*;)\s*\n",
            body,
        )
    if not guard:
        die("sd_revalidate_disk: online-device guard not found")
    insert_at = start + guard.end()
    indent = guard.group("indent")
    payload = f"\n{indent}\tsd_528_limit_queue_depth(sdkp);\n"
    return text[:insert_at] + payload + text[insert_at:]


def ensure_init_cleanup(text: str) -> str:
    marker = "err_out_528_ctx_pool:"
    if marker in text:
        return text
    start, end = find_function_span(
        text,
        r"^static\s+int\s+__init\s+init_sd\s*\(void\)",
        "init_sd",
    )
    body = text[start:end]
    label = body.find("err_out_driver:")
    if label < 0:
        die("init_sd: err_out_driver label not found")
    page_destroy = body.find("mempool_destroy(sd_page_pool);", label)
    if page_destroy < 0:
        die("init_sd: sd_page_pool cleanup not found")
    insert_at = start + page_destroy
    payload = (
        "mempool_destroy(sd_528_page_pool);\n"
        "err_out_528_page_pool:\n"
        "\tmempool_destroy(sd_528_ctx_pool);\n"
        "err_out_528_ctx_pool:\n\t"
    )
    return text[:insert_at] + payload + text[insert_at:]


def generate_diff(original: Path, modified: Path, label: str) -> bytes:
    proc = run([
        "diff", "-u",
        "--label", f"a/{label}",
        "--label", f"b/{label}",
        str(original), str(modified),
    ])
    if proc.returncode not in (0, 1):
        sys.stderr.buffer.write(proc.stdout)
        die(f"diff failed for {label}")
    return proc.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kernel-tree",
        default="submodules/ubuntu-kernel",
        help="checked-out Ubuntu kernel submodule",
    )
    parser.add_argument(
        "--patch",
        default="patches/kernel/9999-wvg-sd-528-translation.patch",
        help="old patch to rebase",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="output patch (default: <old patch>.rebased)",
    )
    args = parser.parse_args()

    kernel = Path(args.kernel_tree).resolve()
    old_patch = Path(args.patch).resolve()
    output = Path(args.output).resolve() if args.output else old_patch.with_suffix(old_patch.suffix + ".rebased")

    if not old_patch.is_file():
        die(f"patch not found: {old_patch}")
    source_paths = ["drivers/scsi/sd.c", "drivers/scsi/sd.h"]
    for rel in source_paths:
        if not (kernel / rel).is_file():
            die(f"kernel source not found: {kernel / rel}")

    # Protect against accidentally generating a patch from already modified files.
    if (kernel / ".git").exists():
        dirty = run(["git", "-C", str(kernel), "diff", "--", *source_paths])
        if dirty.stdout.strip():
            die("sd.c or sd.h is already modified in the kernel submodule; restore it first")

    patch_text = old_patch.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    patch_bytes = patch_text.encode("utf-8")
    c_hunks = parse_file_hunks(patch_text, "drivers/scsi/sd.c")
    if len(c_hunks) < 13:
        die(f"unexpected patch layout: found only {len(c_hunks)} sd.c hunks")

    first_runs = addition_runs(c_hunks[0])
    if len(first_runs) != 3:
        die(f"unexpected first hunk: expected 3 addition runs, found {len(first_runs)}")
    init_runs = addition_runs(c_hunks[10])
    exit_runs = addition_runs(c_hunks[12])
    if len(init_runs) != 1 or len(exit_runs) != 1:
        die("unexpected init/exit hunk layout")

    with tempfile.TemporaryDirectory(prefix="rebase-pve-528-") as tmp_name:
        tmp = Path(tmp_name)
        original = tmp / "original"
        work = tmp / "work"
        test = tmp / "test"
        for root in (original, work, test):
            (root / "drivers/scsi").mkdir(parents=True)
        for rel in source_paths:
            shutil.copy2(kernel / rel, original / rel)
            shutil.copy2(kernel / rel, work / rel)

        applied = run(["patch", "--batch", "--forward", "-p1"], cwd=work, stdin=patch_bytes)
        print(applied.stdout.decode("utf-8", errors="replace"), end="")
        if applied.returncode == 0:
            print("Original patch already applies cleanly; writing an equivalent refreshed diff.")
        elif applied.returncode != 1:
            die(f"patch returned unexpected status {applied.returncode}")

        c_path = work / "drivers/scsi/sd.c"
        c_text = c_path.read_text(encoding="utf-8")

        # Failed hunk #1: split it at its stable semantic anchors.
        c_text = insert_after_exact(
            c_text,
            "static mempool_t *sd_page_pool;\n",
            first_runs[0],
            "static mempool_t *sd_528_page_pool;",
        )
        c_text = insert_after_exact(
            c_text,
            "static struct lock_class_key sd_bio_compl_lkclass;\n",
            first_runs[1],
            "static bool sd_emulate_512_from_fat_sectors;",
        )
        if "struct sd_528_bounce_chunk" not in c_text:
            array = re.search(
                r"(?ms)^static const char \*sd_cache_types\[\]\s*=\s*\{.*?^\};\n",
                c_text,
            )
            if not array:
                die("sd_cache_types array not found")
            c_text = c_text[:array.end()] + first_runs[2] + c_text[array.end():]

        # Failed hunk #9: newer kernels use return + kmalloc_obj here.
        c_text = insert_queue_depth_call(c_text)

        # Failed hunk #11: newer kernels pass &sd_template, not &sd_template.gendrv.
        c_text = insert_before_regex(
            c_text,
            r"^[ \t]*err\s*=\s*scsi_register_driver\(&sd_template(?:\.gendrv)?\);\s*$",
            init_runs[0],
            "sd_528_ctx_pool = mempool_create_kmalloc_pool",
            "init_sd registration",
        )

        # Hunk #12 normally applies, but make the cleanup robust if it did not.
        c_text = ensure_init_cleanup(c_text)

        # Failed hunk #13: same sd_template API change on unregister.
        if "scsi_unregister_driver(&sd_template.gendrv);" in c_text:
            unregister_pattern = r"^[ \t]*scsi_unregister_driver\(&sd_template\.gendrv\);\s*$"
        else:
            unregister_pattern = r"^[ \t]*scsi_unregister_driver\(&sd_template\);\s*$"
        matches = list(re.finditer(unregister_pattern, c_text, flags=re.MULTILINE))
        if "mempool_destroy(sd_528_page_pool);\n\tmempool_destroy(sd_528_ctx_pool);" not in c_text:
            if len(matches) != 1:
                die(f"exit_sd unregister: expected one match, found {len(matches)}")
            pos = matches[0].end()
            c_text = c_text[:pos] + "\n" + exit_runs[0].rstrip("\n") + c_text[pos:]

        c_path.write_text(c_text, encoding="utf-8")

        required = [
            "sd_528_prepare_emulation",
            "sd_528_limit_queue_depth(sdkp);",
            "sd_528_ctx_pool = mempool_create_kmalloc_pool",
            "err_out_528_ctx_pool:",
            "mempool_destroy(sd_528_page_pool);",
        ]
        missing = [item for item in required if item not in c_text]
        if missing:
            die(f"rebased sd.c is missing: {', '.join(missing)}")
        h_text = (work / "drivers/scsi/sd.h").read_text(encoding="utf-8")
        if "SD_528_MEMPOOL_SIZE" not in h_text or "device_sector_size" not in h_text:
            die("sd.h additions were not applied")

        out_bytes = b""
        for rel in source_paths:
            out_bytes += generate_diff(original / rel, work / rel, rel)
        output.write_bytes(out_bytes)

        # Final proof: the generated patch must apply to pristine source files.
        for rel in source_paths:
            shutil.copy2(original / rel, test / rel)
        verified = run(["patch", "--dry-run", "--batch", "-p1"], cwd=test, stdin=out_bytes)
        if verified.returncode:
            sys.stderr.buffer.write(verified.stdout)
            die("generated patch did not pass dry-run")

    print(f"\nOK: rebased patch written to {output}")
    print("Replace the old patch only after reviewing/testing it:")
    print(f"  mv {output} {old_patch}")
    print("  patch --dry-run --batch -d submodules/ubuntu-kernel -p1 < " + str(old_patch))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_dd_payload(payload: str) -> dict:
    parts = (payload or "").split("|", 4)
    while len(parts) < 5:
        parts.append("")
    ddname, dsn, disp_primary, disp_normal, path = parts
    return {
        "ddname": ddname.strip().upper(),
        "dsn": dsn.strip().upper(),
        "disp_primary": disp_primary.strip().upper(),
        "disp_normal": disp_normal.strip().upper(),
        "path": path.strip(),
    }


def dd_first(dds: list, name: str) -> dict:
    target = name.strip().upper()
    for dd in dds:
        if dd.get("ddname") == target:
            return dd
    return None


def dd_all(dds: list, name: str) -> list:
    target = name.strip().upper()
    return [dd for dd in dds if dd.get("ddname") == target]


def ensure_cobc() -> str:
    cobc = "cobc.exe" if os.name == "nt" else "cobc"
    try:
        proc = subprocess.run([cobc, "--version"], capture_output=True, text=True)
        if proc.returncode == 0:
            return cobc
    except Exception:
        pass
    print("COBCOMP: cobc not found in PATH", file=sys.stderr)
    return ""


def decode_text_file(src: Path, dst: Path) -> None:
    raw = src.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp037")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


def stage_source(src: Path, temp_root: Path) -> Path:
    staged = temp_root / "src" / src.name
    decode_text_file(src, staged)
    return staged


def stage_include_tree(include_dir: Path, temp_root: Path) -> Path:
    staged_root = temp_root / "include" / include_dir.name
    staged_root.mkdir(parents=True, exist_ok=True)
    for member in include_dir.iterdir():
        if member.is_file():
            decode_text_file(member, staged_root / member.name)
    return staged_root


def tokenize_parm(parm: str) -> list[str]:
    text = str(parm or "").strip()
    if not text:
        return []

    try:
        return shlex.split(text)
    except ValueError:
        # Preserve existing behavior if the PARM string has mismatched quotes.
        return text.split()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dd", action="append", default=[])
    parser.add_argument("--parm", default="")
    args, _ = parser.parse_known_args(argv)

    dds = [parse_dd_payload(item) for item in args.dd]

    sysin = dd_first(dds, "SYSIN")
    if sysin is None or not sysin.get("path"):
        print("COBCOMP: SYSIN DD is required", file=sys.stderr)
        return 12

    src_path = Path(sysin["path"])
    if not src_path.exists() or not src_path.is_file():
        print(f"COBCOMP: SYSIN path not found: {src_path}", file=sys.stderr)
        return 12

    modname = src_path.stem.upper()

    syslmod = dd_first(dds, "SYSLMOD")
    if syslmod and syslmod.get("path"):
        out_root = Path(syslmod["path"])
    else:
        out_root = Path(__file__).resolve().parent

    out_root.mkdir(parents=True, exist_ok=True)
    out_module_base = out_root / modname.lower()

    cobc = ensure_cobc()
    if not cobc:
        return 12

    extra = tokenize_parm(args.parm)

    with tempfile.TemporaryDirectory(prefix="cobcomp_") as work:
        temp_root = Path(work)
        staged_src = stage_source(src_path, temp_root)

        include_args = []
        for dd in dd_all(dds, "SYSLIB"):
            dd_path = dd.get("path", "")
            if not dd_path:
                continue
            include_dir = Path(dd_path)
            if not include_dir.exists() or not include_dir.is_dir():
                continue
            staged_inc = stage_include_tree(include_dir, temp_root)
            include_args.extend(["-I", str(staged_inc)])

        # Build a dynamically loadable module for cobcrun by module name.
        cmd = [
            cobc,
            "-m",
            "-free",
            "-o",
            str(out_module_base),
            *include_args,
            *extra,
            str(staged_src),
        ]

        print("COBCOMP: running", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)

        if proc.returncode != 0:
            print(f"COBCOMP: compile failed RC={proc.returncode}", file=sys.stderr)
            return proc.returncode

    built_module = None
    for candidate in [
        out_module_base,
        out_module_base.with_suffix(".so"),
        out_module_base.with_suffix(".dylib"),
        out_module_base.with_suffix(".dll"),
        out_module_base.with_suffix(".sl"),
    ]:
        if candidate.exists() and candidate.is_file():
            built_module = candidate
            break

    if built_module is None:
        built_module = out_module_base

    meta = built_module.with_suffix(".meta.json")
    meta.write_text('{"runtime_mode":"gnucobol-env"}\n', encoding="utf-8")

    print(f"COBCOMP: built module {built_module}")
    print(f"COBCOMP: wrote metadata {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

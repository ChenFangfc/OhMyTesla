#!/usr/bin/env python3
"""Make a tree of Mach-O files self-contained, and prove that it is.

    relocate.py fix    <root> --libdir <dir relative to root>
    relocate.py verify <root> [--forbid STRING ...] [--max-minos 13.0]

fix:    every dependency that points outside the system (anything not under
        /usr/lib or /System) is copied into <root>/<libdir> and the reference
        is rewritten to @loader_path/<relative path>.  Absolute LC_RPATHs that
        point outside <root> are deleted.  Every file we touch is ad-hoc
        re-signed, because install_name_tool invalidates the signature and
        arm64 macOS refuses to run unsigned code.

verify: fails (exit 1) if any Mach-O under <root> references an absolute
        non-system path, has an absolute non-system LC_RPATH, has an
        @loader_path/@rpath dependency that does not resolve inside the tree,
        or has an invalid code signature, or (with --max-minos) declares a
        minimum macOS newer than the target.  --forbid additionally reports
        files that merely *contain* a string (e.g. the build directory) —
        that is informational, not a failure, because such strings are often
        harmless defaults that are overridden at runtime.

Only uses otool / install_name_tool / codesign from Xcode.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys

SYSTEM_PREFIXES = ("/usr/lib/", "/System/")
DYLIB_CMDS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
              "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}
MH_EXECUTE, MH_DYLIB, MH_BUNDLE = 2, 6, 8
THIN = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")


def macho_filetype(path: str) -> int | None:
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if len(head) < 16:
                return None
            if head[:4] in THIN:
                return struct.unpack("<I", head[12:16])[0]
            if head[:4] == b"\xca\xfe\xba\xbe":            # fat (big-endian header)
                nfat = struct.unpack(">I", head[4:8])[0]
                if not 0 < nfat < 20:                      # Java .class shares this magic
                    return None
                offset = struct.unpack(">I", head[16:20])[0]
                f.seek(offset)
                inner = f.read(16)
                if inner[:4] in THIN:
                    return struct.unpack("<I", inner[12:16])[0]
    except OSError:
        return None
    return None


def walk_machos(root: str) -> list[str]:
    found = []
    for d, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            if macho_filetype(p) in (MH_EXECUTE, MH_DYLIB, MH_BUNDLE):
                found.append(p)
    return sorted(found)


def min_macos(path: str) -> str | None:
    """LC_BUILD_VERSION minos (or LC_VERSION_MIN_MACOSX version) of a Mach-O."""
    out = subprocess.run(["otool", "-l", path], capture_output=True, text=True).stdout
    cmd = None
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            cmd = line.split(None, 1)[1]
        elif cmd == "LC_BUILD_VERSION" and line.startswith("minos "):
            return line.split()[1]
        elif cmd == "LC_VERSION_MIN_MACOSX" and line.startswith("version "):
            return line.split()[1]
    return None


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def load_commands(path: str) -> tuple[list[str], list[str], str | None]:
    out = subprocess.run(["otool", "-l", path], capture_output=True, text=True, check=True).stdout
    deps: list[str] = []
    rpaths: list[str] = []
    ident = None
    cmd = None
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            cmd = line.split(None, 1)[1]
            continue
        if line.startswith(("name ", "path ")):
            value = line.split(None, 1)[1]
            cut = value.rfind(" (offset ")
            if cut != -1:
                value = value[:cut]
            if cmd in DYLIB_CMDS and line.startswith("name ") and value not in deps:
                deps.append(value)
            elif cmd == "LC_ID_DYLIB" and line.startswith("name "):
                ident = value
            elif cmd == "LC_RPATH" and line.startswith("path ") and value not in rpaths:
                rpaths.append(value)
    return deps, rpaths, ident


def inside(root: str, path: str) -> bool:
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def expand(token_path: str, loader: str, exe_dir: str | None) -> str | None:
    if token_path.startswith("@loader_path"):
        return os.path.normpath(os.path.dirname(loader) + token_path[len("@loader_path"):])
    if token_path.startswith("@executable_path"):
        if exe_dir is None:
            return None
        return os.path.normpath(exe_dir + token_path[len("@executable_path"):])
    return token_path


def resolve(dep: str, loader: str, rpaths: list[str], exe_dir: str | None) -> str | None:
    if dep.startswith("@rpath/"):
        tail = dep[len("@rpath/"):]
        for r in rpaths:
            base = expand(r, loader, exe_dir)
            if base and os.path.exists(os.path.join(base, tail)):
                return os.path.join(base, tail)
        return None
    p = expand(dep, loader, exe_dir)
    return p if p and os.path.exists(p) else None


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr}")


def resign(path: str) -> None:
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", path])


def is_fat(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"\xca\xfe\xba\xbe"


def signature_ok(path: str) -> bool:
    r = subprocess.run(["codesign", "--verify", "--strict", path], capture_output=True, text=True)
    return r.returncode == 0


def normalize(root: str, arch: str = "arm64") -> int:
    """Thin universal binaries to one architecture and ad-hoc sign anything
    whose signature is missing or broken (some wheels ship unsigned slices;
    arm64 macOS will not map unsigned code)."""
    n = 0
    for f in walk_machos(root):
        changed = False
        if is_fat(f):
            info = subprocess.run(["lipo", "-archs", f], capture_output=True, text=True).stdout.split()
            if arch in info and len(info) > 1:
                os.chmod(f, os.stat(f).st_mode | 0o200)
                run(["lipo", f, "-thin", arch, "-output", f + ".thin"])
                os.replace(f + ".thin", f)
                changed = True
        if changed or not signature_ok(f):
            os.chmod(f, os.stat(f).st_mode | 0o200)
            resign(f)
            n += 1
    return n


def fix(root: str, libdir: str) -> int:
    root = os.path.realpath(root)
    libdir_abs = os.path.join(root, libdir)
    queue = walk_machos(root)
    seen: set[str] = set()
    copied: set[str] = set()
    touched = 0
    while queue:
        f = queue.pop(0)
        if f in seen:
            continue
        seen.add(f)
        deps, rpaths, ident = load_commands(f)
        args: list[str] = []
        for dep in deps:
            if dep.startswith(SYSTEM_PREFIXES):
                continue
            src = None
            if dep.startswith("/"):
                src = dep
            elif dep.startswith("@rpath/"):
                hit = resolve(dep, f, rpaths, None)
                if hit and not inside(root, hit):
                    src = hit
            if src is None:
                continue
            if not os.path.exists(src):
                sys.exit(f"{f}: dependency {dep} does not exist on this machine")
            name = os.path.basename(dep)
            target = os.path.join(libdir_abs, name)
            if not os.path.exists(target):
                os.makedirs(libdir_abs, exist_ok=True)
                shutil.copy2(os.path.realpath(src), target)
                os.chmod(target, 0o755)
                copied.add(target)
                queue.append(target)
                print(f"  bundled {src} -> {os.path.relpath(target, root)}")
            rel = os.path.relpath(libdir_abs, os.path.dirname(f))
            new = "@loader_path/" + ("" if rel == "." else rel + "/") + name
            if new != dep:
                args += ["-change", dep, new]
        for r in rpaths:
            if r.startswith("/") and not r.startswith(SYSTEM_PREFIXES) and not inside(root, r):
                args += ["-delete_rpath", r]
        if f in copied and ident and not ident.startswith("@"):
            args += ["-id", "@rpath/" + os.path.basename(f)]
        if args:
            os.chmod(f, os.stat(f).st_mode | 0o200)
            run(["install_name_tool", *args, f])
            resign(f)
            touched += 1
            print(f"  rewrote {os.path.relpath(f, root)}")
    signed = normalize(root)
    print(f"relocate fix: {len(seen)} Mach-O files scanned, {touched} rewritten, "
          f"{len(copied)} dylibs bundled, {signed} thinned/re-signed")
    return 0


def verify(root: str, forbid: list[str], check_sign: bool, max_minos: str | None = None) -> int:
    root = os.path.realpath(root)
    machos = walk_machos(root)
    problems: list[str] = []
    notes: list[str] = []
    for f in machos:
        rel = os.path.relpath(f, root)
        deps, rpaths, _ident = load_commands(f)
        exe_dir = os.path.dirname(f) if macho_filetype(f) == MH_EXECUTE else None
        for dep in deps:
            if dep.startswith(SYSTEM_PREFIXES):
                continue
            if dep.startswith("/"):
                problems.append(f"{rel}: links absolute non-system path {dep}")
                continue
            if dep.startswith("@executable_path/") and exe_dir is None:
                notes.append(f"{rel}: {dep} (resolved by whichever executable loads it)")
                continue
            hit = resolve(dep, f, rpaths, exe_dir)
            if hit is None:
                if dep.startswith("@rpath/") and not rpaths:
                    notes.append(f"{rel}: {dep} (no LC_RPATH of its own; loader supplies it)")
                else:
                    problems.append(f"{rel}: {dep} does not resolve inside the bundle")
            elif not inside(root, hit):
                problems.append(f"{rel}: {dep} resolves outside the bundle ({hit})")
        for r in rpaths:
            if r.startswith("/") and not r.startswith(SYSTEM_PREFIXES):
                problems.append(f"{rel}: absolute LC_RPATH {r}")
        if max_minos:
            m = min_macos(f)
            if m is None:
                notes.append(f"{rel}: no minimum macOS recorded")
            elif vkey(m) > vkey(max_minos):
                problems.append(f"{rel}: requires macOS {m} (target is {max_minos})")
        if check_sign:
            r = subprocess.run(["codesign", "--verify", "--strict", f], capture_output=True, text=True)
            if r.returncode != 0:
                problems.append(f"{rel}: invalid code signature: {r.stderr.strip()}")
    hits: dict[str, list[str]] = {}
    if forbid:
        needles = [s.encode() for s in forbid]
        for d, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(d, name)
                if os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                for n in needles:
                    if n in blob:
                        hits.setdefault(n.decode(), []).append(os.path.relpath(p, root))
    print(f"relocate verify: {len(machos)} Mach-O files under {root}")
    for n in notes[:20]:
        print(f"  note: {n}")
    if len(notes) > 20:
        print(f"  note: ... {len(notes) - 20} more")
    for needle, files in hits.items():
        print(f"  info: {len(files)} file(s) contain the string {needle!r}")
        for p in files[:15]:
            print(f"        {p}")
        if len(files) > 15:
            print(f"        ... {len(files) - 15} more")
    for p in problems:
        print(f"  PROBLEM: {p}")
    if problems:
        print(f"relocate verify: FAILED with {len(problems)} problem(s)")
        return 1
    print("relocate verify: OK — no Mach-O references anything outside the bundle or the OS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fix")
    f.add_argument("root")
    f.add_argument("--libdir", required=True)
    v = sub.add_parser("verify")
    v.add_argument("root")
    v.add_argument("--forbid", action="append", default=[])
    v.add_argument("--no-sign-check", action="store_true")
    v.add_argument("--max-minos", default=None, help="fail on Mach-Os that need a newer macOS")
    a = ap.parse_args()
    if a.cmd == "fix":
        return fix(a.root, a.libdir)
    return verify(a.root, a.forbid, not a.no_sign_check, a.max_minos)


if __name__ == "__main__":
    sys.exit(main())

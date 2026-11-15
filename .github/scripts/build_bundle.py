"""Build the uploadable skill bundles from git HEAD.

    python .github/scripts/build_bundle.py dist

Writes dist/openai-token-rent.zip (ChatGPT: Skills > Create > Upload from your computer) and
dist/openai-token-rent.skill (claude.ai), both a zip with one top-level folder holding SKILL.md.
Only files tracked by git are included; development files are left out.
"""
import os
import subprocess
import sys
import zipfile

NAME = "openai-token-rent"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXCLUDE_DIRS = ("evals/", "tests/", ".github/")
EXCLUDE_FILES = (".gitattributes", ".gitignore", "install.sh", "install.ps1")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    for f in out.decode("utf-8").split("\0"):
        if f and not f.startswith(EXCLUDE_DIRS) and f not in EXCLUDE_FILES:
            yield f


def build(outdir):
    os.makedirs(outdir, exist_ok=True)
    files = sorted(tracked_files())
    if "SKILL.md" not in files:
        sys.exit("SKILL.md is not tracked; nothing built")
    paths = []
    for ext in ("zip", "skill"):
        path = os.path.join(outdir, f"{NAME}.{ext}")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                with open(os.path.join(ROOT, f), "rb") as fh:
                    data = fh.read()
                if f.endswith((".md", ".py", ".json", ".jsonl", ".yaml", ".txt")) or f == "LICENSE":
                    data = data.replace(b"\r\n", b"\n")      # same bytes on every OS
                info = zipfile.ZipInfo(f"{NAME}/{f}", date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, data)
        paths.append(path)
    print(f"{len(files)} files -> " + ", ".join(paths))
    return paths


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "dist")

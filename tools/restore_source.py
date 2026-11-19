"""Restore the pinned upstream and apply the verified Dev16 source delta."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path,
                        help="New directory for the complete source tree")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "source-manifest.json").read_text("utf-8"))
    patch = ROOT / "patches/dev16.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise SystemExit("Patch checksum mismatch; nothing was downloaded or changed.")
    # Refuse existing directories, including empty ones and symbolic links.
    destination = args.destination.resolve()
    if args.destination.is_symlink() or destination.exists():
        raise SystemExit("Destination already exists; choose a new directory.")
    destination.mkdir(parents=True)
    run("git", "init", str(destination))
    run("git", "-C", str(destination), "config", "core.autocrlf", "false")
    run("git", "-C", str(destination), "remote", "add", "upstream", manifest["upstream"])
    run("git", "-C", str(destination), "fetch", "--depth=1", "upstream", manifest["base_commit"])
    run("git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD")
    run("git", "-C", str(destination), "apply", "--check", str(patch))
    run("git", "-C", str(destination), "apply", str(patch))
    for relative, expected in manifest["overlay_sha256"].items():
        if hashlib.sha256((destination / relative).read_bytes()).hexdigest() != expected:
            # Git may produce CRLF on Windows; compare the canonical LF content.
            actual = (destination / relative).read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(actual).hexdigest() != expected:
                raise SystemExit(f"Restored source checksum mismatch: {relative}")
    print(f"Dev16 sources restored in {destination}. No application was started.")


if __name__ == "__main__":
    main()

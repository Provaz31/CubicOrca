"""Check Git index blobs, never displaying the matched credential values.

This is a targeted pre-publication check, not a guarantee against every secret
format. Protocol constants and public MQTT trust material are documented in
docs/STATUS.md. The staged patch is checked too, including removed diff lines.
"""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "JWT": rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    "private key": rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "AWS access key": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "Slack token": rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
    "authenticated URL": rb"https?://[^\s/:'\"]+:[^\s/@'\"]+@",
}
LITERAL = re.compile(
    rb'''(?i)(?<![A-Za-z0-9_])["']?(?:password|passwd|access_token|refresh_token|token|api_key|client_secret)["']?\s*[:=]\s*(["'])([^"'\r\n]{12,})\1''')
# Exact, reviewed documentation and negative-test inputs; no wildcard exemptions.
PUBLIC_EXAMPLES = {
    b"eyJ123456789.abcdefghijk.abcdefghijk",
    b"https://username:password@",
    b"https://user:secret@",
}
FORBIDDEN_PARTS = {"private_profile_backup", ".test", "__pycache__", "verification"}
FORBIDDEN_NAMES = {"state.json", "session.json", ".env"}
FORBIDDEN_SUFFIXES = {".zip", ".log", ".pfx", ".p12", ".key", ".exe", ".dll"}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def findings(path, data):
    parts = PurePosixPath(path)
    if (set(parts.parts) & FORBIDDEN_PARTS or parts.name in FORBIDDEN_NAMES
            or parts.suffix.lower() in FORBIDDEN_SUFFIXES
            or parts.name.startswith((".env.", "credentials"))):
        yield "private/runtime file"
    for name, pattern in PATTERNS.items():
        if any(match.group(0) not in PUBLIC_EXAMPLES for match in re.finditer(pattern, data)):
            yield name
    # The large patch duplicates the overlay, and fixtures use deliberately fake
    # account strings. Strong credential formats above are still checked in all.
    if path.endswith((".py", ".json", ".cpp", ".hpp", ".js")) and "/tests/" not in path:
        for match in LITERAL.finditer(data):
            value = match.group(2).lower()
            if not value.startswith((b"synthetic", b"example", b"test-", b"offline")):
                yield "credential literal (review required)"


def main():
    failed = False
    count = 0
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        path = raw_path.decode("utf-8")
        if stage != b"0" or mode not in (b"100644", b"100755"):
            print(f"REJECT {path}: non-regular or unmerged entry")
            failed = True
            continue
        count += 1
        for reason in sorted(set(findings(path, git("cat-file", "blob", oid.decode())))):
            print(f"REJECT {path}: {reason}")
            failed = True
    print(f"Checked {count} staged files; {'FAILED' if failed else 'no credential patterns found'}.")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())

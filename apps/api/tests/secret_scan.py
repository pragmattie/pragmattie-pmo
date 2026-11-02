"""A small scanner for secrets in tracked files, run by `test_secret_scan.py` in the required CI
check (there is no separate pre-commit hook).

It flags:

- key and certificate files by name: `*.pem`, `*.key`, `*.p12`, `*.pfx`;
- a private-key header in any file;
- common token prefixes followed by a token-shaped value (GitHub, Anthropic, AWS, Slack);
- a password literal in assignment form, matched case-insensitively in every file: a name holding
  `password` or `passwd`, then ` = ` with spaces and a quoted value (code), or a whole line
  `NAME=value` (env and shell files). Keyword arguments (`password="..."`, no spaces) are not
  assignments and are not flagged. Env placeholders (`change-me...`, `${...}`) are allowed.

A line that is a deliberate, harmless exception (a made-up test password) carries the marker
`# secret-scan: allow`, so every exception is visible in review. Tests are not exempt by path:
a real token pasted into a test is still a leak.

The patterns here are written so this file never matches itself.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "secret-scan: allow"

KEY_FILE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})

PRIVATE_KEY = re.compile(r"-{5}BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-{5}")

TOKENS = {
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}

_NAME = r"[A-Za-z0-9_.]*pass(?:word|wd)[A-Za-z0-9_]*"
# Code: the name, ` = ` with spaces (as an assignment is written), then a quoted value.
CODE_PASSWORD = re.compile(_NAME + r"""\s+=\s+(["'])(?!\1)""", re.IGNORECASE)
# Env and shell files: a whole line `PASSWORD=value` or `export PASSWORD="value"`.
ENV_PASSWORD = re.compile(
    r"""^\s*(?:export\s+)?""" + _NAME + r"""=(?P<value>["']?[^\s,()]+["']?)\s*$""",
    re.IGNORECASE,
)
ENV_PLACEHOLDER = re.compile(r"""^["']?(?:change-me[\w-]*|\$\{.*\})["']?$""", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        # The matched text is never shown: it may be a real secret.
        return f"{self.path}:{self.line}: {self.kind}" if self.line else f"{self.path}: {self.kind}"


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        if PRIVATE_KEY.search(line):
            findings.append(Finding(path, number, "private key"))
        for kind, pattern in TOKENS.items():
            if pattern.search(line):
                findings.append(Finding(path, number, kind))
        env = ENV_PASSWORD.match(line)
        if CODE_PASSWORD.search(line) or (env and not ENV_PLACEHOLDER.match(env["value"])):
            findings.append(Finding(path, number, "password literal"))
    return findings


def scan_files(root: Path, names: list[str]) -> list[Finding]:
    """Scan the files `names` (relative to `root`). Binary files are checked by name only."""
    findings = []
    for name in names:
        if Path(name).suffix.lower() in KEY_FILE_SUFFIXES:
            findings.append(Finding(name, 0, "key or certificate file"))
        path = root / name
        if not path.is_file():
            continue  # deleted in the working tree but still tracked
        data = path.read_bytes()
        if b"\0" in data:
            continue
        findings += scan_text(name, data.decode("utf-8", errors="replace"))
    return findings


def tracked_files(root: Path) -> list[str]:
    """Every file git tracks under `root`. Raises if git fails, so a scan can't pass by
    scanning nothing."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=60
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def repo_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    return Path(result.stdout.strip())

#!/usr/bin/env python3
"""Refuse a shell command that reads a credential file.

PreToolUse hook on Bash; exit 2 blocks; fails open. Matches the command shape
rather than the filename alone, so `test -f .env`, `ls -la .env`, `mv .env x`,
and code that mentions the filename all pass. Only shapes that would print or
source the file's bytes are blocked.
"""

from __future__ import annotations

import json
import re
import sys

READERS = r"cat|bat|less|more|head|tail|xxd|od|hexdump|strings|grep|egrep|fgrep|rg|ag|ack|awk|sed"

PATH_PATTERN = (
    r"(?:\.env(?:\.[\w.-]+)?"
    r"|\.netrc"
    r"|\.pgpass"
    r"|id_(?:rsa|ed25519|ecdsa|dsa)(?:\.pub)?"
    r"|\.ssh/(?:id_[\w.-]+|config|known_hosts)"
    r"|\.aws/credentials"
    r"|\.config/gh/hosts\.yml"
    r"|credentials\.json"
    r"|service[-_]account\.json)"
)

FULL_PATH = rf"(?:[^\s\"';|&()`]*/)?{PATH_PATTERN}"

# `cat .env`, `grep FOO .env`, `sed -n ... .env`, `head -n1 ~/.aws/credentials`.
READ_COMMAND = re.compile(
    rf"(?:^|[\s;&|`(])(?:{READERS})\b[^|;&()`]*?\s{FULL_PATH}(?:$|[\s;&|)`\"'])",
    re.IGNORECASE,
)

# `. .env` and `source .env` execute the file in the current shell.
SOURCE_COMMAND = re.compile(
    rf"(?:^|[\s;&|`(])(?:source|\.)\s+{FULL_PATH}(?:$|[\s;&|)`\"'])",
    re.IGNORECASE,
)

# `export $(cat .env)`, `envsubst < .env`, `< .env command`.
REDIRECT_READ = re.compile(rf"<\s*{FULL_PATH}", re.IGNORECASE)

KEYCHAIN = re.compile(r"\bsecurity\s+(find|dump)-(generic|internet)-password\b", re.IGNORECASE)

MESSAGE = """This command reads a credential file. API keys live in the system
keyring; a `.env` here carries local paths and is not worth pulling into
context. Metadata operations (test -f, stat, ls, mv, rm, chmod) and code that
merely names the file are fine; only pipelines that would print or source its
bytes are refused.
"""


def reads_credentials(command: str) -> bool:
    """Return whether the command would print, source, or redirect a credential file."""
    return bool(
        READ_COMMAND.search(command)
        or SOURCE_COMMAND.search(command)
        or REDIRECT_READ.search(command)
        or KEYCHAIN.search(command)
    )


def main() -> int:
    """Hook entry point: exit 2 to refuse, 0 to allow."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    if reads_credentials(command):
        sys.stderr.write(MESSAGE)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fails open
        sys.exit(0)

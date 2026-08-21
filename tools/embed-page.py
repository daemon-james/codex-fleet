#!/usr/bin/env python3
"""Embed dashboard.html into the codex-fleet CLI, and prove it survived.

WHY THIS EXISTS. The dashboard is one HTML string inside a single-file Python
CLI. Editing it in place means writing JavaScript through a Python string
literal, and on 2026-08-21 that mangled every quote in the page: 86 occurrences
of backslash-quote, which is invalid JavaScript, in a file that still imported
and ran fine. Python cannot catch it and neither can a syntax check.

So dashboard.html is the source of truth and this tool is the only thing that
writes the string. Edit the HTML, run this, done.

    tools/embed-page.py            # embed, verify, report
    tools/embed-page.py --check    # verify only, non-zero if they differ

--check is what CI and the test suite use to catch a CLI edited by hand.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "dashboard.html"
CLI = ROOT / "codex-fleet"
MARKER = 'SERVE_PAGE = """'


def extract(source: str) -> str:
    start = source.index(MARKER)
    end = source.index('"""', source.index("</html>", start))
    return source[start + len(MARKER) : end]


def main() -> int:
    check_only = "--check" in sys.argv
    page = PAGE.read_text(encoding="utf-8")

    # A triple quote would end the string early; a backslash is the mangling
    # this tool exists to prevent. Refuse rather than produce a broken CLI.
    if '"""' in page:
        print("dashboard.html contains a triple quote, which cannot be embedded", file=sys.stderr)
        return 2
    if "\\" in page:
        print("dashboard.html contains a backslash. Write the page so it needs none:",
              file=sys.stderr)
        print("  use double quotes in JS, and string concatenation over templates",
              file=sys.stderr)
        return 2

    source = CLI.read_text(encoding="utf-8")
    current = extract(source)

    if current == page:
        print("codex-fleet already carries this dashboard.html, byte for byte")
        return 0
    if check_only:
        print("codex-fleet is OUT OF DATE with dashboard.html", file=sys.stderr)
        print("  run tools/embed-page.py to update it", file=sys.stderr)
        return 1

    start = source.index(MARKER)
    end = source.index('"""', source.index("</html>", start)) + 3
    CLI.write_text(source[:start] + MARKER + page + '"""' + source[end:], encoding="utf-8")

    # Read it back. Trusting the write is how the mangling survived last time.
    if extract(CLI.read_text(encoding="utf-8")) != page:
        print("embedded page does not match the source after write-back", file=sys.stderr)
        return 3
    print(f"embedded {len(page)} bytes, verified byte-identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())

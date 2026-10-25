#!/usr/bin/env python3
"""make-help.py — render `make help` from the Makefile's help comments.

    python3 scripts/make-help.py Makefile [more makefiles...]

Makefile convention (unchanged from the old awk renderer):
  ##@ Title              starts a section
  ##> text               a section note; each line is one wrapped paragraph
  ##| text               detail lines just above a target, joined and wrapped
                         under that target's entry
  target [alias]: ## text   a target and its summary

Layout: every summary starts in one column (the widest target name + 4) and
wraps at HELP_WIDTH, else the terminal width when stdout is a terminal, else
100 columns, so captured output is stable. Long words (paths) are never split.

Color: on when stdout is a terminal, off otherwise. COLOR=1 forces it on and
COLOR=0 off; otherwise NO_COLOR (any value) or TERM=dumb turns it off. Colors
are only added around text, so stripping them gives the plain output exactly.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from typing import List, Optional, Sequence, Tuple

TARGET = re.compile(r"^([a-zA-Z_-]+(?: [a-zA-Z_-]+)*):.*?##\s?(.*)$")
DEFAULT_WIDTH = 100
MIN_TEXT = 24                     # never wrap descriptions narrower than this

BOLD, CYAN, YELLOW = "1", "36", "33"


class Section:
    def __init__(self, title: str):
        self.title = title
        self.notes: List[str] = []
        self.targets: List[Tuple[str, str, str]] = []     # (names, summary, details)


def parse(lines: Sequence[str]) -> List[Section]:
    sections: List[Section] = [Section("")]
    details: List[str] = []
    for line in lines:
        if line.startswith("##@ "):
            sections.append(Section(line[4:].strip()))
        elif line.startswith("##> "):
            sections[-1].notes.append(line[4:].strip())
        elif line.startswith("##| "):
            details.append(line[4:].strip())
            continue
        else:
            m = TARGET.match(line)
            if m:
                sections[-1].targets.append((m.group(1), m.group(2).strip(), " ".join(details)))
        details = []
    return [s for s in sections if s.title or s.notes or s.targets]


def want_color(env=os.environ, stream=sys.stdout) -> bool:
    forced = env.get("COLOR", "")
    if forced in ("0", "1"):
        return forced == "1"
    if env.get("NO_COLOR") or env.get("TERM", "") == "dumb":
        return False
    return stream.isatty()


def width(env=os.environ, stream=sys.stdout) -> int:
    raw = env.get("HELP_WIDTH", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if stream.isatty():
        return shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
    return DEFAULT_WIDTH


def wrap(text: str, width: int, first: str, rest: str) -> List[str]:
    return textwrap.wrap(text, width=width, initial_indent=first, subsequent_indent=rest,
                         break_long_words=False, break_on_hyphens=False) or [first.rstrip()]


def render(sections: Sequence[Section], width: int, color: bool) -> str:
    def c(text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if color and text else text

    names = [n for s in sections for n, _, _ in s.targets]
    col = 2 + max(map(len, names), default=0) + 2
    width = max(width, col + 2 + MIN_TEXT)
    out = [c("Usage:", BOLD) + " make <target> [VAR=value]"]
    for s in sections:
        out.append("")
        if s.title:
            out.append(c(s.title, BOLD))
        for note in s.notes:
            label, sep, body = note.partition(": ")
            lines = wrap(note, width, "  ", "    ")
            if sep and " " not in label and lines[0].startswith(f"  {label}{sep}"):
                lines[0] = "  " + c(label + ":", YELLOW) + lines[0][3 + len(label):]
            out += lines
        for name, summary, details in s.targets:
            lines = wrap(summary, width, " " * col, " " * col)
            lines[0] = "  " + c(name, CYAN) + lines[0][2 + len(name):]
            out += lines
            if details:
                out += wrap(details, width, " " * (col + 2), " " * (col + 2))
    return "\n".join(out) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        print("usage: make-help.py <Makefile>...", file=sys.stderr)
        return 2
    lines: List[str] = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            lines += fh.read().splitlines()
    sys.stdout.write(render(parse(lines), width(), want_color()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

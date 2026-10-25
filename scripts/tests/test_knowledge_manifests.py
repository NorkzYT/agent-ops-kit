"""Checks on the real profile book lists (hermes/profiles/*/knowledge-sources.txt).

`books-in-text/` is a retired library collection: every book in it has a
content-equivalent EPUB under `edu/` (strong match, see docs/knowledge-refresh.md),
so the folder can be deleted from the library without breaking any book list.
Reads the repo seeds only; nothing touches /mnt/books or data/knowledge.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "hermes" / "profiles"
RETIRED = ("books-in-text",)


def active_lines(path: Path):
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield n, line


class RetiredCollections(unittest.TestCase):
    def test_book_lists_exist(self):
        self.assertTrue(sorted(PROFILES.glob("*/knowledge-sources.txt")))

    def test_no_book_list_uses_a_retired_collection(self):
        hits = [f"{p.relative_to(REPO)}:{n}: {line}"
                for p in sorted(PROFILES.glob("*/knowledge-sources.txt"))
                for n, line in active_lines(p)
                if line.split("/", 1)[0] in RETIRED]
        self.assertEqual(hits, [], "book-list lines still use a retired collection")


if __name__ == "__main__":
    unittest.main()

# Profile knowledge sources

This folder is the default **knowledge root**: the shared library each Hermes
profile learns from. One subfolder per profile, one book or note per file.

```
data/knowledge/
├── strategy/
├── marketing/
├── herbalism/
├── survival/
└── business/
```

## Convention

- Path: `data/knowledge/<profile>/**/*.md` and `**/*.txt`.
- One book per file, chapters as headings. Nested folders are kept.
- Only `.md` and `.txt` files sync. PDFs, images and other formats are ignored;
  convert a book to Markdown or plain text first.

## How it reaches a profile

`make hermes-profile NAME=strategy` copies `data/knowledge/strategy/` into
`~/.hermes/profiles/strategy/knowledge/`, then you run `/learn` in that profile's
chat to build its knowledge-base skill. The copy is **additive**: it adds and
refreshes files and never deletes the source or unrelated destination files. Set
`KNOWLEDGE_SYNC_MODE=mirror` to also prune knowledge files the source dropped.

Point the sync at another location (for example an SMB/CIFS mount) with
`KNOWLEDGE_ROOT`:

```bash
make hermes-profile NAME=strategy KNOWLEDGE_ROOT=/mnt/books/knowledge
```

## Recommended book groups

| Folder | What to put here |
|--------|------------------|
| `strategy`   | business model, pricing, prioritisation, competitive analysis |
| `marketing`  | positioning, messaging, channels, funnel and growth |
| `herbalism`  | botanical references, traditional uses, plant safety and identification |
| `survival`   | preparedness, first aid, navigation, water, shelter, food storage |
| `business`   | operations, finance analysis, planning, market research |

## Books stay out of Git

The `data/` tree is gitignored, so book files are never committed. Only this
README is tracked. Keep copyrighted book text on disk (or the SMB mount), not in
the repository.

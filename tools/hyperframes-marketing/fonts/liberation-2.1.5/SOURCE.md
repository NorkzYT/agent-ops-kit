# Liberation Sans 2.1.5 (vendored sample font)

Unmodified files used by the tracked HyperFrames sample (`props/sample.json`).
`props/sample.provenance.json` points `localSource` here (relative to the
provenance file) and pins the same sha256; `render-campaign.mjs` copies a file
into `assets/fonts/` only when its bytes match.

| File | sha256 |
|---|---|
| `LiberationSans-Regular.ttf` | `4659bc0c58c5028dd488ec928d41d9265db43d9b669fc14ca8b0832daca7b144` |
| `LiberationSans-Bold.ttf` | `3973aa5054fb467dd5627245d3dc82e37bf16fe075756156a570455871351582` |

- License: SIL Open Font License 1.1. `COPYRIGHT` is the package's
  `/usr/share/doc/fonts-liberation/copyright`, byte for byte: it carries the
  copyright notices (Red Hat, Inc. with Reserved Font Name Liberation; Google
  Corporation) and the full OFL text. The OFL permits bundling and
  redistributing the fonts with software; they may not be sold by themselves.
- Source: Ubuntu 24.04 (noble) `main` package `fonts-liberation` `1:2.1.5-3`,
  `pool/main/f/fonts-liberation/fonts-liberation_2.1.5-3_all.deb`
  (sha256 `065c2ab1abc9108b17d401016dc594b79750904390f095845c93bb06e1153acc`,
  as listed in the noble `Packages` index). The two `.ttf` files and
  `copyright` were extracted with `dpkg-deb -x`; the font hashes above also
  match `/var/lib/dpkg/info/fonts-liberation.md5sums` on an installed noble host.
- Upstream: https://github.com/liberationfonts/liberation-fonts (2.1.5).

Do not replace these files with whatever a host has installed: other builds of
the same family (for example a GitHub runner image's copy) have different bytes
and are refused by the sha256 check. To change the font, vendor the new files
with their license and source, then update the provenance record.

# pme — the package manager & build system for Emerald

`pme` is to [Emerald](https://github.com/evangelion-research/emerald) what cargo
is to Rust: it resolves dependencies, materializes them into a local
content-addressed store, and drives `emeraldc` with the right module search
path. It is a *driver* around the compiler — it never parses `.rald`, never
rewrites imports, and never generates code. The whole contract with the
compiler is one frozen flag: ordered `-I` roots.

pme is two tools with one seam:

- the **package manager** — resolve, fetch, verify, publish (`DESIGN.md`);
- the **build system** — compute the search path, detect change, drive
  `emeraldc`, cache artifacts, forward diagnostics (`BUILD.md`).

Prior art for both is collected in [`REFERENCES.md`](REFERENCES.md).

**Status:** design. Nothing in pme itself is implemented yet. Its one hard
prerequisite — the Emerald module system — shipped at `emerald@1f683be` and
is still exactly as it shipped: this document tracks the compiler at
`emerald@1facafe` (HEAD, 2026-08-17), and everything upstream landed since —
the functional core (lambdas, thunks, closures, tail-call optimization),
proof mode, and the ray-tracer example — left the resolution rules and the
`-I` contract pme consumes untouched. The implementation is planned in
**Python 3.11+**, published to PyPI as `emerald-pme`.

## Why Python

pme is I/O-bound — network fetches, tarball extraction, subprocess exec of
`emeraldc` — not CPU-bound. Python's stdlib (`tomllib`) plus three small
dependencies (`click` for the CLI, `tomlkit` for comment-preserving manifest
edits, `httpx` for HTTP) cover everything; the hot path stays in the C compiler.
Distribution is `pipx install pme` / `uv tool install pme`.

The scaffold's `pyproject.toml`/`main.py` are close to this (Python 3.13 +
`click` + `requests`); they are reconciled to 3.11+ / `httpx` at milestone 1 —
see `DESIGN.md` §10.0.

## Planned CLI

| command | behavior |
|---|---|
| `pme init [name]` | scaffold `emerald.toml`, `src/main.rald`, `.gitignore` |
| `pme add <pkg>[@ver]` | resolve latest (or given), edit manifest preserving comments, update lock |
| `pme remove <pkg>` | inverse of add |
| `pme install [--locked]` | resolve + fetch + verify; writes `emerald.lock` |
| `pme build` | compute `-I` roots from the lock, exec `emeraldc` |
| `pme run [-- args]` | build, then exec the binary |
| `pme test` | compile and run `tests/*.rald` with dev-deps |
| `pme update [pkg]` | raise minimums to latest compatible; re-resolve |
| `pme tree` / `pme why <pkg>` | dependency tree / every root→package path |
| `pme publish` / `pme login` / `pme yank` | registry writes |
| `pme verify` / `pme clean` | re-hash the store / remove `target/` (and prune) |

Every command takes `--json` and `-q`. Exit codes: `0` ok, `1` user/build
error, `2` bad usage, `3` network/registry error.

## Design in one paragraph

Manifests are `emerald.toml` — exactly one of `[lib]` or `[[bin]]` (both
allowed), strict semver, path deps legal locally but rejected at publish.
Resolution is **minimal version selection** (MVS): a pure fixed-point
computation, ~100 lines, with one hard constraint — one version per package,
no multi-major support (mangling is keyed on the dotted module path only).
The lockfile `emerald.lock` pins exact versions **and** content hashes and is
committed; `pme build` consumes it as-is and fails rather than silently
re-resolving. Everything lands in a content-addressed, immutable store at
`~/.emerald/store/`, and the build step reduces to: ordered `-I` roots (each
package's `src/` directory, dependencies before dependents, ties broken by
name) → `emeraldc -I <root> … -o target/<bin> <entry>`. The registry starts
as a static, append-only NDJSON index served over HTTPS — no infrastructure
to operate — with tarballs as release assets and publishes as PRs.

There are no build scripts, by design: installing a package never executes
its code.

## Implementation plan (Python)

The detailed step-by-step plan — 17 steps across 6 phases, each with a
verification gate — is in this repo's [`DESIGN.md`](DESIGN.md) (appendix),
expanded from the pme spec's §10–§11. Milestones:

| # | milestone | status |
|---|---|---|
| 0 | imports in emerald (`-I` contract frozen) | ✅ done — re-verified at `emerald@1facafe` |
| 1 | manifest + lockfile + semver | **current front of work** |
| 2 | MVS resolver | — |
| 3 | store + build (path deps only, no network) | — |
| 4 | registry reads (`add` / `install` / `tree` / `why`) | — |
| 5 | registry writes (reproducible tarball, `publish`, Stage-1 index) | — |
| 6 | polish (`test`, `update`, `--json` everywhere, docs) | — |

Milestone 3 is the first genuinely useful build: it needs no registry at all —
`path` dependencies alone prove the whole `-I` pipeline end to end.

## Compiler contract

`emeraldc [-I <dir>]... [--json] [-o OUT] <entry>.rald` — re-verified at
`emerald@1facafe`. The full driver flag set is now `--emit-tokens`,
`--emit-ast`, `--check`, `--emit-c`, `--proof` (proof mode), `--keep-c`, `-I`,
`-o`, `--json`; `-I` is repeatable and order-preserving, and `--check`,
`--emit-c`, and a full build operate on the linked program. pme's job is to
compute the ordered `-I` list and exec. The LSP consumes the same lockfile and
the same rule (see the "Package management" section of the LSP design) — pme
never does analysis, the LSP never does resolution.

## Links

- Package-manager + overall design: [`DESIGN.md`](DESIGN.md)
- Build-system design + route: [`BUILD.md`](BUILD.md)
- Prior art: [`REFERENCES.md`](REFERENCES.md)
- pme spec companion: `evangelion-research/pme` (`DESIGN.md`)
- Emerald compiler: `evangelion-research/emerald`
- License: MIT — see `LICENSE`

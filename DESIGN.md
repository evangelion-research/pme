# pme — the Package Manager for Emerald

**Status:** design. Nothing here is implemented yet.
**Target language:** Python 3.11+ (chosen for iteration speed; pme is I/O-bound, not CPU-bound).
**Distribution model:** central registry.

pme is to [Emerald](https://github.com/evangelion-research/emerald) what cargo is to Rust:
it resolves dependencies, materializes them into a local cache, and drives `emeraldc`
with the right module search path. It is a *driver* around the compiler, never a
replacement for it.

---

## 0. Prerequisite: Emerald needs a module system

pme cannot exist in a useful form until the language can name code in another file.
Today it cannot:

- `src/lexer.c` keyword table has no `import`/`from`/`as`.
- `src/main.c` accepts exactly one `file.rald`, emits one `.gen.c`, and invokes `cc` once.
- `src/check.c` resolves all top-level `def`/`type` in a single flat namespace, and
  `docs/grammar.md` requires type aliases to be declared before use — both single-unit
  assumptions.

So Phase 0 is compiler work, tracked here because pme's entire design depends on its
outcome.

### 0.1 Syntax

Python-flavored, to match the rest of the language:

```
import strings                  # module object; strings.split(...)
import strings as s
from strings import split, join
```

Module paths are dotted and map to directories: `import text.strings` → `text/strings.rald`.

### 0.2 Resolution

A module path resolves to a file by searching, **in order**:

1. the directory of the importing file,
2. the project's `src/` root,
3. each `-I <dir>` passed on the command line, in the order given.

Rule: the first hit wins; a later `-I` never shadows an earlier one. Diagnose ambiguity
across *the same* `-I` root as an error, not a silent pick.

### 0.3 The compiler contract

This is the only interface between pme and `emeraldc`, and it should be designed first
and then frozen:

```
emeraldc [-I <dir>]... [--json] [-o OUT] <entry>.rald
```

pme's job reduces to: compute the ordered list of `-I` roots, then exec the compiler.
pme never parses `.rald` source. It never rewrites imports. It never generates code.

### 0.4 Checker and codegen

- Per-module symbol tables; a module's top-level names are private unless exported.
  Cheapest export rule that fits the language: **a leading underscore means private,
  everything else is exported.** No new keyword needed.
- Import cycles are an error with a real diagnostic code, not a hang.
- Codegen mangles top-level symbols as `<module>__<name>` so two packages can both
  define `parse`.
- Start by concatenating all modules into one translation unit. It preserves the
  existing GC shadow-stack setup and avoids designing a linking story on day one.
  Split into separate `.gen.c` files later, when compile times justify it.

### 0.5 New diagnostics

Following `docs/diagnostics.md` conventions (stable code, `file:line:column`, `--json`):

| code | meaning |
|---|---|
| `E_IMPORT_NOT_FOUND` | module path resolved to no file on the search path |
| `E_IMPORT_CYCLE` | import graph contains a cycle; note lists the cycle |
| `E_IMPORT_PRIVATE` | imported name exists but is private (leading `_`) |
| `E_IMPORT_NAME` | imported name does not exist in that module |
| `E_IMPORT_AMBIGUOUS` | two files under one root claim the same module path |

### 0.6 Definition of done for Phase 0

`tests/imports/` golden suite wired into `task test`, covering: basic import, aliased
import, `from`-import, transitive imports, private-name rejection, cycle rejection,
and shadowing precedence across two `-I` roots.

---

## 1. Concepts

| term | meaning |
|---|---|
| **package** | a named, versioned unit of Emerald source, published to the registry |
| **project** | a directory with an `emerald.toml`; may be a package, an app, or both |
| **entry** | the `.rald` file compiled for a binary target |
| **manifest** | `emerald.toml` — what the human writes |
| **lockfile** | `emerald.lock` — what pme resolved; committed to git |
| **store** | the global content-addressed cache at `~/.emerald/store/` |

Package names: `[a-z][a-z0-9_]*`, 2–64 chars. Flat namespace (no `org/name`) — the
registry enforces uniqueness on first-publish-wins. Reserved: anything shipping in the
stdlib.

Versions are strict semver `MAJOR.MINOR.PATCH` with optional `-prerelease`. No build
metadata, no `v` prefix inside the manifest.

---

## 2. Manifest — `emerald.toml`

```toml
[package]
name        = "myapp"
version     = "0.1.0"
description = "A short line."
license     = "MIT"
authors     = ["Sagnik Chatterjee <sagnikchatterjee607@gmail.com>"]
repository  = "https://github.com/evangelion-research/myapp"
emerald     = ">=0.2.0"          # compiler version constraint

[dependencies]
strings = "1.2.0"                # minimum version (see MVS below)
json    = "0.4.1"

[dev-dependencies]
testkit = "0.1.0"

[[bin]]
name  = "myapp"
entry = "src/main.rald"

[lib]
root = "src/lib.rald"            # optional; presence makes this publishable
```

Rules:

- Exactly one of `[lib]` or `[[bin]]` is required; both may be present.
- A dependency value is a bare version string. **There is no git or path dependency
  in the published graph** — but `pme` supports them locally for development:

  ```toml
  [dependencies]
  strings = { path = "../strings" }        # local dev only
  ```

  A manifest with any `path` dependency is rejected by `pme publish`. This keeps the
  registry's dependency graph closed and resolvable offline from the index alone.
- `[dev-dependencies]` are resolved for `pme test` and never for consumers.

### 2.1 Why not a lockfile-free design

Because MVS (below) is reproducible only given a fixed *set* of manifests, and the
registry is mutable in one direction (new versions appear). The lockfile pins exact
versions **and content hashes**, which is also the supply-chain check.

---

## 3. Lockfile — `emerald.lock`

Generated, committed, never hand-edited. TOML, sorted deterministically by name so
diffs are readable.

```toml
version = 1                       # lockfile format version

[[package]]
name     = "strings"
version  = "1.2.3"
checksum = "sha256:9f2c…"         # of the canonical tarball
deps     = ["unicode"]

[[package]]
name     = "unicode"
version  = "0.9.0"
checksum = "sha256:1ab4…"
deps     = []
```

- `pme build` uses the lockfile as-is and **fails** if the manifest asks for something
  the lock doesn't satisfy (rather than silently re-resolving).
- `pme install` re-resolves when the manifest changed, then rewrites the lock.
- `pme install --locked` (for CI) refuses to modify the lock; exits non-zero on drift.
- A checksum mismatch against the store is a hard error, never a re-download.

---

## 4. Resolution — minimal version selection

Use **MVS** (Go-style), not a SAT/PubGrub solver.

Algorithm:

1. Start with the root manifest's direct dependencies as `{name: min_version}`.
2. Repeatedly: for each selected `(name, version)`, fetch its manifest from the index,
   and for each of its dependencies raise the selected version to the max of what's
   already selected and what it requires.
3. Fixed point = the resolution.

Properties that make this the right call here:

- Adding a dependency never silently upgrades an unrelated one.
- The result is a pure function of the manifest set — trivially reproducible and easy
  to test.
- The implementation is ~100 lines, versus a solver you will spend weeks debugging.
- Only one version of a package is ever selected. **pme does not support multiple
  major versions of the same package in one build** — the flat C symbol namespace after
  mangling can't express it anyway. Diagnose the conflict clearly instead.

`pme update` is the escape hatch: it bumps the recorded minimums in the manifest to the
latest compatible release, then re-resolves.

Failure cases to report well:

- `E_RESOLVE_NOT_FOUND` — package not in the registry.
- `E_RESOLVE_NO_VERSION` — no published version satisfies the minimum.
- `E_RESOLVE_MAJOR_CONFLICT` — two dependents require incompatible majors; the message
  must print both dependency paths (this is what `pme why` renders).
- `E_RESOLVE_COMPILER` — the resolved set needs a newer `emeraldc` than is installed.

---

## 5. The store and the build

### 5.1 Layout

```
~/.emerald/
  store/
    strings-1.2.3-9f2c…/        # extracted, immutable, named by name-version-hash
      emerald.toml
      src/
  cache/
    tarballs/9f2c….tar.gz       # as downloaded, for offline re-extract
  index/                        # cloned/synced registry index
  credentials.toml              # publish token, mode 0600
```

Content-addressed and immutable: two projects on the same machine share one extracted
copy, and a corrupted entry is detectable rather than sticky. Never vendor into the
project directory by default.

### 5.2 What `pme build` actually does

```
read emerald.toml
read emerald.lock            (error if stale)
for each locked package:
    ensure ~/.emerald/store/<name>-<ver>-<hash>/ exists
        else: download tarball → verify sha256 → extract atomically (tmp + rename)
compute -I roots in dependency order
exec: emeraldc -I <root1> -I <root2> … -o target/<binname> src/main.rald
```

Output goes to `target/`. `pme build --json` passes `--json` through to `emeraldc` and
emits pme's *own* errors in the same shape (see §8), so one JSON stream describes the
whole build.

There is **no build script mechanism**. Installing a package must never execute code
from that package. This is a deliberate, load-bearing security property — do not add
`[build] script = …` later without a sandbox story.

---

## 6. CLI

Keep it small. Every command takes `--json` and `-q`.

| command | behavior |
|---|---|
| `pme init [name]` | scaffold `emerald.toml`, `src/main.rald`, `.gitignore` |
| `pme add <pkg>[@ver]` | resolve latest (or given), edit manifest preserving comments, update lock |
| `pme remove <pkg>` | inverse of add |
| `pme install` | resolve + fetch + verify; writes lock. `--locked` for CI |
| `pme update [pkg]` | raise minimums to latest compatible; re-resolve |
| `pme build [--release]` | §5.2. `--release` passes optimization flags through to `emeraldc` |
| `pme run [-- args]` | build, then exec the binary |
| `pme test` | build with dev-deps and run the test target (see §9) |
| `pme tree` | dependency tree with selected versions |
| `pme why <pkg>` | print every path from root to `<pkg>` |
| `pme publish` | §7.3 |
| `pme login` | store a registry token in `~/.emerald/credentials.toml` |
| `pme clean` | remove `target/`; `--store` also prunes unreferenced store entries |
| `pme verify` | re-hash every store entry against the lock |

Exit codes: `0` ok, `1` user/build error, `2` bad usage, `3` network/registry error.

---

## 7. The central registry

### 7.1 Recommended staging

Do **not** build a service first. Ship the registry in two stages:

**Stage 1 — static index, zero servers.**
- Index lives in a git repo `evangelion-research/pme-index`, also served over HTTPS
  from `evangelion-research.github.io/pme-index/`.
- Tarballs are GitHub Release assets on that same index repo (or on each package's own
  repo — pick one and record the URL in the index).
- `pme publish` opens a PR (or pushes, for trusted authors) adding one line to the
  index and uploading the tarball via the GitHub API.
- Reads are a plain HTTPS GET with CDN caching. Auth is a GitHub token.

This gives you a real central namespace, real immutability, and real availability, with
no infrastructure to operate. It is enough for a very long time.

**Stage 2 — a real service**, once publish friction or download volume justifies it.
The client-side protocol below is designed so Stage 2 is a drop-in swap: only the
`registry.base_url` changes.

### 7.2 Index format

One newline-delimited-JSON file per package, at a sharded path so directories stay
small:

```
index/st/ri/strings.json
```

Each line is one published version, append-only:

```json
{"name":"strings","version":"1.2.3","checksum":"sha256:9f2c…","deps":{"unicode":"0.9.0"},"emerald":">=0.2.0","yanked":false,"url":"https://…/strings-1.2.3.tar.gz"}
```

Append-only means a client can cache aggressively and fetch by range. `yanked` is
mutated in place (the one exception) — a yanked version stays downloadable for existing
lockfiles but is never newly selected.

### 7.3 Publish flow

```
pme publish
  ├ validate manifest (name, semver, license, no path deps, [lib] present)
  ├ verify version not already published        → immutable, no overwrites ever
  ├ build the tarball from git-tracked files only, honoring .pmeignore
  ├ normalize: sorted entries, fixed mtime/uid/gid/mode → byte-reproducible tarball
  ├ sha256 the tarball
  ├ dry-run compile in a temp dir to prove it builds against its own deps
  └ upload + append the index line
```

Immutability is absolute: a published `name@version` never changes bytes. `pme yank`
marks a version unselectable without deleting it.

### 7.4 Client-side registry protocol

Exactly three operations, so Stage 1 and Stage 2 can both implement it:

- `GET {base}/index/{shard}/{name}.json` → NDJSON version list
- `GET {url}` → tarball (URL comes from the index line)
- `POST {base}/publish` with bearer token → multipart tarball + metadata

---

## 8. Errors

Mirror the compiler's convention from `emerald/docs/diagnostics.md` so tooling —
including `emerald-lsp` and any LLM loop — sees one consistent shape.

Human:

```
error[E_RESOLVE_MAJOR_CONFLICT]: cannot select a single version of `unicode`
  --> emerald.toml
    = myapp 0.1.0 → strings 1.2.3 → unicode ^0.9
    = myapp 0.1.0 → json 0.4.1 → unicode ^1.0
```

`--json`, one object per error:

```json
{"kind":"resolve","severity":"error","code":"E_RESOLVE_MAJOR_CONFLICT",
 "file":"emerald.toml","message":"…","notes":[{"label":"path","value":"…"}]}
```

`kind` extends the compiler's set with `manifest`, `resolve`, `registry`, `io`.

---

## 9. Testing story

Emerald has no test construct — `tests/` in the compiler repo is golden-file testing
*of the compiler*. So pme needs a convention, and it should be the dumbest one that
works:

- `pme test` compiles and runs every `tests/*.rald` in the project, with dev-deps on
  the `-I` path.
- A test binary passes if it exits `0`. Failures print its stdout.

That needs no language change. A real assertion library can ship later as a package
(`testkit`), and a `test` keyword can come much later if it earns its place.

---

## 10. Implementation in Python

### 10.1 Layout

```
pme/
  pyproject.toml            # hatchling; console_scripts: pme = pme.cli:main
  src/pme/
    cli.py                  # argparse dispatch, exit codes, --json plumbing
    manifest.py             # emerald.toml parse/validate  (tomllib + tomlkit)
    lockfile.py             # emerald.lock read/write, deterministic ordering
    semver.py               # parse/compare/constraints — hand-rolled, ~150 lines
    resolve.py              # MVS
    registry.py             # index fetch, tarball download, publish  (httpx)
    store.py                # content-addressed cache, atomic extract, verify
    build.py                # -I computation, emeraldc invocation, diagnostic passthrough
    diagnostics.py          # the §8 shapes, human + JSON renderers
    errors.py               # PmeError hierarchy → exit codes
  tests/                    # pytest
```

### 10.2 Dependency choices

- `tomllib` (stdlib, 3.11+) to read; **`tomlkit`** to write, because `pme add` must
  preserve the user's comments and formatting.
- `httpx` for HTTP (timeouts and retries that actually work).
- No `packaging` — Emerald semver is stricter and simpler than PEP 440; hand-roll it.
- `pytest` + `pytest-httpx` for tests.

### 10.3 Non-negotiables

- **Every filesystem mutation is atomic**: write to a temp path in the same directory,
  `os.replace`. A `^C` during extract must never leave a half-package in the store.
- **A lock file (`~/.emerald/.lock`, flock) around store writes**, so two concurrent
  `pme build`s don't corrupt each other.
- **Verify before use, always**: hash on extract and on `pme verify`; never trust a
  path just because it exists.
- **No network in `pme build`** when the lock is satisfied by the store. Builds must
  work offline.
- **Never `shell=True`**. `emeraldc` is invoked with an argv list.

### 10.4 Shipping

`pipx install pme` / `uv tool install pme`, published to PyPI as `emerald-pme`
(`pme` is likely taken). Pin `requires-python = ">=3.11"`.

Python is a *for-now* choice. The clean seam is §5.2 and §7.4: if pme is ever rewritten
in C or Go, the manifest, lockfile, index format, and the `emeraldc -I` contract all
survive unchanged.

---

## 11. Milestones

| # | milestone | done when |
|---|---|---|
| 0 | **imports in emerald** | `tests/imports/` green; `-I` frozen |
| 1 | manifest + lockfile + semver | round-trip parse/write, property tests on semver |
| 2 | MVS resolver | resolves against a fake in-memory index, conflicts reported |
| 3 | store + build | `pme build` works with `path` deps only — no network yet |
| 4 | registry reads | index fetch, download, verify, extract; `pme add/install/tree/why` |
| 5 | registry writes | reproducible tarball, `pme publish`, `pme login`, Stage-1 index live |
| 6 | polish | `pme test`, `pme update`, `--json` everywhere, `pme verify`, docs |

Milestone 3 is the first genuinely useful build, and it needs no registry at all —
`path` dependencies alone prove the whole `-I` pipeline end to end.

---

## 12. Open questions

1. **Stdlib boundary.** Do `strings`/`json`/`math` ship inside `emeraldc`, or as pme
   packages? This shapes the entire ecosystem and should be decided before milestone 5.
   Recommendation: a small set compiled into the compiler, everything else in packages.
2. **Compiler version pinning.** Should `emerald = ">=0.2.0"` be enforced by pme
   invoking `emeraldc --version`? (Requires adding `--version` to the driver — it has
   no such flag today.)
3. **`emerald-lsp` integration.** The LSP must read `emerald.lock` and pass the same
   `-I` roots, or cross-package go-to-definition breaks. Worth designing alongside
   milestone 3.
4. **Prereleases.** MVS + prereleases interact badly. Simplest answer: prereleases are
   never selected automatically, only by exact pin.

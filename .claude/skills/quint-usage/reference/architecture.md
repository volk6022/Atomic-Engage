# Quint — Codebase Structure & Architecture

Internal architecture of the Quint tool itself. **Only needed when contributing
to Quint or debugging tool internals** — not for writing specs (see `../SKILL.md`
for that). Companion reference to `commands.md`.

Repository: https://github.com/informalsystems/quint
Analyzed version: `0.32.0` (from `quint/package.json`). License: Apache 2.0.
Maintainer: Informal Systems.

## 1. Project purpose and scope

Quint is a modern, executable specification language for describing the behavior
of distributed and concurrent systems (protocols, smart contracts, consensus
algorithms) as formal **state machines**, and for checking properties of those
state machines automatically.

It occupies the same space as TLA+ (and is built on top of the TLA+ ecosystem
and the Apalache model checker), but aims for:
- A conventional, programmer-friendly syntax (closer to TypeScript/Scala than to
  mathematical notation).
- Strong, automatically-inferred **static types**.
- A built-in **effect system** checking state-variable read/update discipline
  (e.g. "every variable must be updated exactly once per step").
- A REPL and a random **simulator** for fast, lightweight feedback, in addition
  to full model checking.
- First-class integration with **Apalache** (symbolic, SMT-based) and **TLC**
  (explicit-state, from the TLA+ tools).

User-facing mental model: write a **model** (`var` + `init`/`step` actions),
write **properties** (invariants / temporal), run the **simulator** (fast,
samples, no false positives, may miss bugs) or a **model checker** (exhaustive
up to a bound). On violation, Quint produces a concrete **counter-example** trace.

## 2. Repository layout (top level)

A **monorepo** hosting several related projects, each with its own toolchain:

```
quint/                     (repo root)
├── quint/                 TypeScript: the `quint` CLI/compiler/simulator package
├── evaluator/             Rust: alternative/optional high-performance evaluator
├── vscode/                TypeScript: VSCode extension + Language Server (LSP)
├── docs/                  TypeScript/Next.js (Nextra): website + docs content
├── editor-plugins/        Vim/Emacs/Helix syntax & LSP glue (non-VSCode editors)
├── examples/              .qnt example specifications, organized by domain
├── logos/                 Branding assets
├── flake.nix / flake.lock Nix dev-shell / reproducible-build definitions
├── Makefile               Top-level build orchestration (delegates to subprojects)
├── CONTRIBUTING.md        Architecture pointers + dev/test/release workflow
└── CHANGELOG.md           Per-release changelog
```

The CLI, Rust evaluator, VSCode extension/LSP, website, and TLC helper scripts
live under one roof because they evolve together and share the same Quint
Intermediate Representation (IR) and grammar.

## 3. Main components and their roles

### 3.1 `quint/` — core CLI / compiler package (TypeScript)
The heart of the project, published to npm as `@informalsystems/quint`. It is
simultaneously a parser, type checker, effect checker, TLA+/Apalache-JSON
compiler, simulator front-end, REPL, and a thin RPC client for Apalache. It:
- Parses `.qnt` source into an **IR**.
- Resolves names across modules/imports/instances.
- Infers types (Hindley-Milner-like, row-typed records) and effects.
- "Flattens" multi-module specs into one self-contained module (Apalache and the
  evaluators can't handle modules/imports/instances directly).
- Drives simulation via the in-process TS evaluator or the Rust evaluator binary.
- Drives verification via a locally-managed **Apalache server** (gRPC, "Shai") or
  transpilation to TLA+ for TLC.
- Exposes everything as CLI subcommands and a programmatic API (`src/index.ts`).

### 3.2 `evaluator/` — Rust evaluator ("rust backend")
A from-scratch reimplementation of Quint's runtime semantics in Rust, used with
`--backend=rust` (the **default** for `run`/`test`/`repl`). Deserializes the same
IR/JSON the TS compiler produces and executes it natively for performance
(random simulation is CPU-bound). Ships its own benchmarks (`evaluator/benches/`)
and `insta` snapshot tests. Per `CONTRIBUTING.md` it is "not yet feature-complete"
vs the TS evaluator, so `--backend=typescript` remains a fallback.

### 3.3 `vscode/` — editor tooling
- `vscode/quint-vscode/client/` — the VSCode extension shell.
- `vscode/quint-vscode/server/` — the **Quint Language Server** (LSP), published
  separately as `@informalsystems/quint-language-server` so Emacs/Neovim reuse it.
- The LSP reuses the core parser/typechecker for hovers, go-to-def,
  find-references, rename, document symbols.

### 3.4 `editor-plugins/`
Lightweight syntax/LSP-bootstrap files for Vim and Emacs. Helix has built-in
Quint support upstream.

### 3.5 `docs/`
A Next.js site (Nextra). Contains user docs (`docs/content/docs/*.mdx`),
**Architecture Decision Records (ADRs)** under
`docs/content/docs/development-docs/architecture-decision-records/` (the best
source for *why* the internals are shaped as they are), RFCs/"stories", a
`docs/codetour/` step-by-step codebase walkthrough, and blog/marketing content.

### 3.6 `examples/`
Curated real `.qnt` specs by domain: `classic`, `cosmos` (bank, ICS-20, ICS-23,
light client, Tendermint), `cosmwasm`, `cryptography`, `games`,
`language-features`, `puzzles`, `solidity` (ERC20, auctions, Ponzi), `spells`
(reusable utility modules — Quint's informal "stdlib extension"), `tutorials`,
and `verification`.

## 4. Code organization — `quint/src/` deep dive

A **multi-pass compiler pipeline**: a `.qnt` file flows through named
transformation stages, each its own subfolder, mirroring ADR001.

### 4.1 Top-level orchestration (`quint/src/*.ts`)

| File | Role |
| --- | --- |
| `cli.ts` | CLI entry. Defines all `yargs` subcommands and wires each to a chain of `CLIProcedure`s. |
| `cliCommands.ts` | Implements command handlers (`load`, `parse`, `typecheck`, `compile`, `runRepl`, `runSimulator`, `runTests`, `verifySpec`, `docs`, `outputResult`). |
| `cliHelpers.ts`, `cliReporting.ts` | CLI arg/output helpers; human-readable error/result reporting. |
| `quintAnalyzer.ts` | Orchestrates **static analysis**: type inference, effect inference, mode checking, multiple-updates and nondet checks; aggregates errors. Glue between parser/resolver and type/effect subsystems. Has `analyzeInc` for incremental re-analysis (REPL/LSP). |
| `index.ts` | Public programmatic API (what the LSP and other tools import). |
| `repl.ts` | The interactive REPL. |
| `simulation.ts` | Drives the random simulator. |
| `verify.ts` | Drives model checking via Apalache/TLC. |
| `apalache.ts` | Manages the Apalache distribution: download/cache the right version, launch/talk to its **Shai** gRPC server, version checks. |
| `tlc.ts` | TLC backend support. |
| `compileToTlaplus.ts` | Compiles flattened IR to TLA+ source (TLC path + export). |
| `docs.ts` | Extracts `///` docstrings and renders docs (self-generates `builtin.md`). |
| `builtin.qnt` / `builtin.ts` | Built-in operators — `builtin.qnt` is Quint source typing the stdlib; `builtin.ts` wires it into the compiler. |
| `quintError.ts`, `ErrorMessage.ts`, `errorTree.ts`, `errorReporter.ts` | Structured error model (`QNTxxx` codes) + pretty-printing (ADR002). |
| `idGenerator.ts`, `FreshVarGenerator.ts` | Fresh unique IDs/var names for IR construction, flattening, inference. |
| `itf.ts` | Informal Trace Format — JSON schema for exporting traces (`--out-itf`). |
| `rng.ts` | Seeded RNG for reproducible simulation (`--seed`). |
| `graphics.ts`, `prettierimp.ts`, `jsonHelper.ts`, `util.ts`, `verbosity.ts`, `config.ts` | Pretty-printing, JSON, utilities, verbosity, config loading. |
| `reflection.proto` | Protobuf/gRPC schema for the Apalache RPC ("Shai"). |
| `version.ts` | Auto-generated version string (via `genversion`). |

### 4.2 `src/generated/` — ANTLR-generated parser
Grammar in two ANTLR4 files: `Quint.g4` (full language) and `Effect.g4` (effect
annotations). `npm run antlr` regenerates the `*Lexer/Parser/Listener/Visitor.ts`
files. The only place classic parser-generator tech is used; everything
downstream works on the hand-rolled IR, not the ANTLR parse tree.

### 4.3 `src/parsing/` and `src/ir/` — parsing & IR
- `parsing/quintParserFrontend.ts` drives ANTLR, reports syntax errors
  (`parseErrors.ts`), resolves `import`/file lookups (`sourceResolver.ts`).
- `parsing/ToIrListener.ts` walks the parse tree and **builds the Quint IR** — the
  bridge from generated grammar to the hand-written IR types.
- `ir/quintIr.ts` defines core IR nodes: modules, declarations (`def`, `val`,
  `var`, `action`, `assume`, `import`, `instance`, `export`, type aliases),
  expressions.
- `ir/IRVisitor.ts` / `ir/IRTransformer.ts` — the **visitor pattern** (ADR003)
  used by every pass to traverse/transform IR.
- `ir/idRefresher.ts`, `ir/namespacer.ts` — re-number IDs and namespace-qualify
  names (used in flattening/instancing).
- `ir/initToPredicate.ts` — converts `init`/`step` actions to boolean predicates.
- `ir/IRprinting.ts`, `ir/IRFinder.ts` — print IR back to Quint; locate nodes.

### 4.4 `src/names/` — name resolution
Module-aware resolution: `collector.ts` gathers definitions + scoping metadata
(extended in ADR007 for flattening), `resolver.ts` resolves references,
`unshadower.ts` handles shadowing, `importErrors.ts` defines resolution errors,
`base.ts` defines the `LookupTable` shared by nearly every later pass (type/effect
inference, LSP hover/go-to-def).

### 4.5 `src/types/` — type system (ADR005)
A constraint-based Hindley-Milner-style inferrer, custom-built (no ad-hoc
polymorphism/GADTs in Quint). `constraintGenerator.ts` (visitor) emits equality
constraints per expression; `constraintSolver.ts` unifies; `inferrer.ts`
orchestrates, producing a `TypeScheme` per definition; `substitutions.ts` applies
substitutions; `builtinSignatures.ts` holds built-in signatures;
`aliasInliner.ts`/`typeApplicationResolution.ts` resolve aliases/type-application;
`simplification.ts`/`printing.ts`/`specialConstraints.ts`/`parser.ts`/`base.ts`
handle messages, pretty-printing, row/record constraints, annotation parsing, and
the core `Type`/`TypeScheme` defs.

### 4.6 `src/effects/` — effect system (ADR004)
A second, independent inference system (deliberately not unified with types)
tracking **Read**, **Update**, **Temporal** effects, enforcing the
`pure`/`def`/`action`/`val` qualifiers and catching bugs like "this action
doesn't update `x`" or "this updates `x` twice". Mirrors `types/`: `inferrer.ts`
+ `EffectVisitor.ts` + `ToEffectVisitor.ts` (constraint gen), `substitutions.ts`
(unification), `builtinSignatures.ts`, `namespaces.ts`, `printing.ts`/
`simplification.ts`, `parser.ts` (the `Effect.g4` annotation syntax).
`modeChecker.ts` checks inferred effects against the declared qualifier;
`MultipleUpdatesChecker.ts` and `NondetChecker.ts` are extra focused checks.

### 4.7 `src/flattening/` — module flattening (ADR007)
Eliminates `import`/`instance`/`export` and inlines into one self-contained module
(required by Apalache and the evaluators): `instanceFlattener.ts` resolves module
instances first; `flattener.ts` / `fullFlattener.ts` recursively copy only the
transitively-needed definitions, re-ID and namespace-qualify them.

### 4.8 `src/static/` — auxiliary static analyses
- `callgraph.ts` — call graph of operator definitions (recursion/dependency order).
- `toposort.ts` — topological sort (ordering defs during flattening/solving).

### 4.9 `src/runtime/` — TypeScript evaluator backend
Original pure-TS operational semantics (`--backend=typescript`):
`runtime/impl/evaluator.ts` (tree-walking evaluator), `builtins.ts` (set/map/list
ops, arithmetic, `oneOf`, `forall`), `runtimeValue.ts`/`runtimeValueDiff.ts`
(values via `immutable.js`, diffing for trace output), `Context.ts`/`VarStorage.ts`
(eval context, state-var storage), `nondet.ts`/`builder.ts` (nondeterministic
`oneOf`, assembling runtime from IR), `trace.ts` (recording), `testing.ts`
(supports `quint test`).

### 4.10 `src/rust/`
Glue for invoking the external **Rust evaluator binary** as a subprocess when
`--backend=rust` (locating/downloading the right version, spawning, exchanging
IR/JSON over IPC).

### 4.11 Tests
- `test/` mirrors `src/` 1:1 — Mocha + Chai unit tests (`npm run test`).
- `testFixture/` — golden/snapshot fixtures (`npm run update-fixtures`).
- `integration-tests/` — **txm**-driven (Markdown-as-test) e2e tests: `lang/`
  (CLI exit codes + stdout/stderr), `runtime/typescript/` & `runtime/rust/`,
  `verification.md`, `distribution/` (binary download/caching).

## 5. Architecture patterns (the ADRs)

Documented as Architecture Decision Records under
`docs/content/docs/development-docs/architecture-decision-records/`:

1. **Staged/data-flow pipeline, not rigid sequential** (ADR001) — a transpiler
   context + task-list/scheduler so consumers (CLI, LSP, future tools) can inject
   or reorder passes. `cliCommands.ts` chains `load → parse → typecheck → compile
   → run/verify`; the LSP uses the same blocks incrementally (`analyzeInc`).
2. **Structured, coded error model** (ADR002) — every failure is a `QuintError`
   with a stable `QNTxxx` code, message, and source `reference`. Convention: throw
   only for "impossible" internal invariant violations; use `Either`
   (`@sweet-monads/either`) for anything depending on user input.
3. **Visitor pattern for IR traversal** (ADR003) — `IRVisitor`/`IRTransformer`
   give every pass uniform traversal without re-implementing tree recursion.
4. **Independent type and effect systems** (ADR004 vs ADR005) — types (data
   shape) and effects (state access) are two orthogonal passes run side-by-side in
   `quintAnalyzer.ts`, interacting only at error reporting.
5. **Constraint-generation + unification** for both inferences — a visitor emits
   equality constraints over fresh variables; a solver unifies (Algorithm-W-style
   split, purpose-built rather than borrowed).
6. **Flatten-late, dependency-aware inlining** (ADR007) — module structure kept as
   long as possible, eliminated only right before steps that need a single flat
   module; only the transitive closure of *used* definitions is copied.
7. **External process integration over re-implementation** (ADR008) — manages a
   long-lived **Apalache server ("Shai")** over gRPC and a Rust evaluator binary
   as a subprocess, with version-compatibility tracking, downloading/caching
   on demand instead of vendoring.
8. **Pluggable backends behind a stable CLI** — `run`/`test`/`repl` expose
   `--backend=typescript|rust`; `verify` exposes `--backend=apalache|tlc`.
9. **Monorepo with separation by language/runtime** — TS CLI/compiler, Rust
   evaluator, and VSCode extension/LSP are versioned/tested/released somewhat
   independently (three release processes) while sharing one repo for atomic
   IR/grammar changes.

## 6. Dependencies and external integrations

### 6.1 Core TypeScript runtime deps (`quint/package.json`)

| Dependency | Purpose |
| --- | --- |
| `antlr4ts` | Runtime for the ANTLR-generated lexer/parser. |
| `@sweet-monads/either`, `@sweet-monads/maybe` | Functional `Either`/`Maybe` for error handling. |
| `immutable` | Persistent collections backing the TS runtime's `RuntimeValue`. |
| `yargs` | CLI argument parsing. |
| `@grpc/grpc-js`, `@grpc/proto-loader` | gRPC client for the Apalache "Shai" server. |
| `@octokit/request` | GitHub API client — fetch Apalache/evaluator release artifacts. |
| `adm-zip`, `tar` | Unpack downloaded distribution archives. |
| `cross-spawn` | Cross-platform subprocess spawning (Apalache / Rust evaluator). |
| `seedrandom` | Deterministic seeded RNG (`--seed`). |
| `lodash`, `chalk`, `cli-progress`, `line-column`, `json-bigint` | Utilities, terminal output, error-location mapping, big-int-safe JSON (Quint `int` is unbounded). |

### 6.2 External tools/services (downloaded/managed at runtime, not npm)
- **Apalache** — default integrated symbolic model checker (`quint verify`).
  Downloaded/cached and launched as a background gRPC server by `apalache.ts`;
  requires **JDK ≥ 17**.
- **TLC** — alternative explicit-state checker (`quint verify --backend=tlc`),
  via transpilation to TLA+.
- **Z3** — the SMT solver Apalache depends on internally (not a direct Quint dep).

### 6.3 Rust evaluator deps (`evaluator/Cargo.toml`)
`serde`/`serde_json` (deserialize TS-produced IR/JSON), `imbl` (immutable
collections), `num-bigint`/`num-traits` (arbitrary-precision ints), `itf` (write
ITF), `rand`+`squares-rnd` (RNG), `mimalloc` (allocator), `criterion`/`insta`
(dev: benchmarking + snapshot tests).

### 6.4 Build/dev tooling
Nix (`flake.nix`, all-in-one dev shell), Mocha+Chai (TS unit tests), **txm**
(Markdown-driven integration tests), **lmt** (Go tool tangling literate Markdown
into code/tests), Nextra/Next.js (docs site), ANTLR4 via antlr4ts-cli (grammar
codegen, needs a JRE), ESLint+Prettier (`npm run format`), clippy+rustfmt (Rust).

## 7. Data flow — `quint verify spec.qnt --invariant=myInv`

```
.qnt source file(s)
        │
        ▼
 ┌─────────────┐   ANTLR lexer/parser (src/generated/)
 │ load/parse  │   + sourceResolver.ts resolves imports
 │             │   ToIrListener.ts builds Quint IR (src/ir/quintIr.ts)
 └─────────────┘
        │  IR (possibly multi-module, unresolved names)
        ▼
 ┌─────────────┐   names/collector.ts + names/resolver.ts
 │  resolve    │   builds/queries the LookupTable (names/base.ts)
 └─────────────┘
        │  IR with names resolved
        ▼
 ┌─────────────┐   quintAnalyzer.ts orchestrates:
 │ typecheck   │     types/inferrer.ts        (constraint gen + solve)
 │ (+ effects, │     effects/inferrer.ts      (read/update/temporal)
 │  modes,     │     effects/modeChecker.ts   (qualifier conformance)
 │  nondet)    │     NondetChecker.ts, MultipleUpdatesChecker.ts
 └─────────────┘
        │  Typed + effect-annotated IR, or QuintError[]
        ▼
 ┌─────────────┐   flattening/instanceFlattener.ts (resolve instances)
 │  flatten    │   flattening/flattener.ts / fullFlattener.ts
 └─────────────┘   (inline imports/exports, namespace-qualify)
        │  Single, self-contained, flat IR module
        ▼
 ┌─────────────┐   compileToTlaplus.ts  → TLA+ source        (TLC path)
 │  compile    │   apalache.ts          → Apalache JSON IR   (Apalache path)
 └─────────────┘
        │
        ▼
 ┌──────────────────────────────┐
 │   verify.ts                  │
 │   ├─ Apalache: gRPC call ────┼── locally-managed "Shai" server (apalache.ts)
 │   └─ TLC: spawn `tlc` ───────┼── tlc.ts shells out to the TLA+ tools
 └──────────────────────────────┘
        │
        ▼
  [ok]  — invariant holds for all explored states up to --max-steps
   or [violation] — counter-example trace reconstructed and printed
                    (optionally written as ITF JSON via --out-itf, itf.ts)
```

For `quint run` (simulation) and `quint test`, the flow is identical through
`typecheck`/`flatten`/`compile`, then branches to `simulation.ts`, dispatching to
either the **TypeScript runtime** (`src/runtime/impl/evaluator.ts`) or the **Rust
evaluator** (`src/rust/` spawns the `quint_evaluator` binary, optionally
multi-threaded via `--n-threads`). Both output `[ok]` or `[violation]` + trace,
matching the model-checker format. The **REPL** (`repl.ts`) reuses the same
pipeline incrementally (hence `analyzeInc` in `quintAnalyzer.ts`, shared with the
LSP's live diagnostics).

## 8. Where to look for what

| To understand... | Look at... |
| --- | --- |
| The language grammar | `quint/src/generated/Quint.g4` |
| What a parsed spec looks like internally | `quint/src/ir/quintIr.ts` |
| How `.qnt` text becomes IR | `quint/src/parsing/`, `ir/ToIrListener.ts` |
| Why two separate type/effect systems | ADR004, ADR005 |
| How `import`/`instance`/`export` get resolved away | ADR007, `quint/src/flattening/` |
| How `quint verify` talks to Apalache | ADR008, `quint/src/apalache.ts` |
| How simulation/random testing works | `quint/src/simulation.ts`, `runtime/impl/`, `evaluator/src/simulator.rs` |
| The full CLI surface (all flags) | `quint/src/cli.ts` (and `commands.md` here) |
| Coding conventions for contributors | `CONTRIBUTING.md` |
| End-to-end behavioral tests / expected CLI output | `quint/integration-tests/` |

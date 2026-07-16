# Quint — CLI Command & Flag Reference

Full command reference for the `quint` CLI. Companion to `../SKILL.md`. All flags
below are grounded in the Quint CLI (`quint/src/cli.ts`).

Repository: https://github.com/informalsystems/quint · Docs: https://quint-lang.org

## Commands

| Command | Purpose |
| --- | --- |
| `quint` (no subcommand) or `quint repl` | Launch the interactive REPL. Preload a file: `quint -r bank.qnt::bank` (loads module `bank`). |
| `quint parse <file>` | Check **syntax only**; reports parse errors with source locations. |
| `quint typecheck <file>` | Check **types and effects** (read/update/temporal discipline) without running anything. Fast static check while iterating. |
| `quint run <file> --invariant=<name>` | **Simulate** random executions, checking the invariant at each step. Fast, no false positives, can miss rare bugs. |
| `quint test <file>` | Run Quint's built-in unit-test-style assertions (`run`-defined scenarios) against the spec. |
| `quint verify <file> --invariant=<name>` | Exhaustively **model-check** up to `--max-steps`, via Apalache (default) or TLC (`--backend=tlc`). |
| `quint compile <file> --target=tlaplus\|json` | Compile a flattened spec to TLA+ source or Apalache JSON IR, written to stdout. |
| `quint docs <file>` | Render documentation from docstring comments (`///`) in a `.qnt` file. |

## Flags by command

### `quint run` (simulator)
- `--invariant=<name>` — the invariant (a `val: bool`) to check at every state.
- `--invariants=<a> --invariants=<b>` — array form, check several (repeatable).
- `--max-samples=<N>` — how many executions to try before giving up. Default
  **10000**, or **1** if `--seed` is set.
- `--max-steps=<N>` — length of each trace. Default **20**.
- `--seed=0x...` — reproduce a specific run. Every violation prints the seed that
  triggered it, so you can replay it. (Single-value: cannot be passed twice.)
- `--n-traces=<N>` — generate multiple distinct counter-example traces. **Cannot
  exceed `--max-samples`** (CLI rejects it otherwise).
- `--mbt` — emit Model-Based-Testing metadata so the trace shows *which action*
  caused each transition (e.g. the `withdraw` that drove a balance negative).
- `--out-itf=<file.json>` — export the trace in **Informal Trace Format** (ITF),
  for model-based testing against a real implementation.
- `--witnesses=<expr>` — count how often a boolean expression holds across
  sampled traces (repeatable, array-typed).
- `--backend=rust|typescript` — evaluator choice. **`rust` is the default** and is
  faster for large numbers of samples.
- `--hide=<...>` — hide variables from trace output (repeatable, array-typed).

### `quint verify` (model checker)
- `--invariant=<name>` / `--invariants=...` — property to verify.
- `--max-steps=<N>` — bound on execution length. Default **10**. `[ok]` means "no
  violation exists within N steps" — much stronger than the simulator's "none
  found in N samples".
- `--backend=apalache|tlc` — **Apalache** (symbolic, SMT-based) is default; **TLC**
  (explicit-state, from the TLA+ tools) for small fully-enumerable state spaces.
  TLC path transpiles to TLA+ first.
- `--inductive-invariant=<name>` — supply an inductive invariant to make
  verification tractable when the full state space is too large for `--max-steps`.
- `--apalache-version=<tag>` — pin/override the Apalache version to download.
- `--server-endpoint=<host:port>` — talk to your own running Apalache "Shai"
  server instead of the auto-managed one. Default `localhost:8822`.

### Global-ish flags (most commands)
- `--main=<module>` — name the main module if it can't be inferred from the filename.
- `--out=<file>` — write output to a file instead of stdout (suppresses console output).
- `--verbosity=0..5` — output detail. `0` = silent (useful for scripting), `5` = max.
- `-r <file>::<module>` — (REPL) preload a file and enter a module's scope.

## Backends at a glance

| Concern | Options | Default | Notes |
| --- | --- | --- | --- |
| Simulation evaluator (`run`/`test`/`repl`) | `rust`, `typescript` | `rust` | Rust is faster; TS is the fallback (more feature-complete historically). |
| Verification engine (`verify`) | `apalache`, `tlc` | `apalache` | Apalache = symbolic/SMT (needs JDK ≥ 17 + downloads Apalache). TLC = explicit-state. |

## Validation quirks (so errors make sense)
- `--n-traces cannot be greater than --max-samples` — intentional. Raise
  `--max-samples` or lower `--n-traces`.
- `--<flag> can not be specified more than once` — only array-typed options
  (`--invariants`, `--witnesses`, `--hide`) accept repetition; single-value flags
  (`--invariant`, `--seed`) are rejected if duplicated (not "last wins").

## Exit / output conventions
- `[ok]` — no violation found (simulator: in the samples; verifier: within `--max-steps`).
- `[violation]` — a counter-example trace is printed state-by-state, and optionally
  written as ITF JSON via `--out-itf`.
- Use `--verbosity=0` + `--out` for clean, scriptable output in CI.

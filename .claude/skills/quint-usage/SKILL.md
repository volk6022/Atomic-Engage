---
name: quint-usage
description: Use the Quint formal specification language to model and verify state machines (protocols, distributed systems, smart contracts), AND to connect a spec to a real Python codebase via Model-Based Testing. Trigger when asked to write a `.qnt` spec, model a protocol/algorithm as a state machine, find or prove the absence of bugs via simulation or model checking, run `quint run`/`quint verify`/`quint test`, debug Quint type/effect errors, set up Quint (install, editor, Apalache/TLC), or build a conformance/MBT harness in Python that replays Quint traces against real code. Self-contained — no external knowledge base needed.
---

# Quint Usage

Quint is an executable **specification language** for modeling state machines —
protocols, distributed systems, consensus algorithms, smart contracts — and
checking properties against those models. It pairs a programmer-friendly syntax
(closer to TypeScript/Scala than to math) with strong inferred static types, an
effect system that enforces state read/update discipline, a random **simulator**
(`quint run`) for fast feedback, and two integrated **model checkers**
(`quint verify` → Apalache by default, or TLC). It sits in the same space as
TLA+ and is in fact built on the TLA+/Apalache ecosystem.

Repository: https://github.com/informalsystems/quint ·
Docs: https://quint-lang.org · Language manual: https://quint-lang.org/docs/lang

> This skill is **self-contained**. Everything needed to install, write, run, and
> debug Quint is in this file plus the `reference/` folder:
> - `reference/commands.md` — full CLI command + flag reference (cheat sheet).
> - `reference/mbt-python.md` — **connecting a spec to real Python code** via
>   Model-Based Testing: invariants, adapters, the ITF trace format, a stdlib-only
>   ITF reader, a `pytest` conformance harness, CI wiring. Read this for §8.
> - `reference/architecture.md` — Quint's internal codebase architecture (only
>   needed when contributing to Quint itself or debugging tool internals, not for
>   writing specs).

---

## The mental model (read this first)

1. **Model** — declare state variables with `var`, then `init` and `step`
   actions describing how state evolves. `x' = x + 1` reads as "x in the next
   state equals x plus one". The primed name (`x'`) is the next-state value.
2. **Properties** — write *invariants* (booleans that must hold in every
   reachable state) or temporal properties (`always`, `eventually`).
3. **Check** — run the **simulator** (`quint run`, fast, samples random
   executions, never reports a false bug but can miss rare ones) or a **model
   checker** (`quint verify`, exhaustive up to a step bound, finds any bug
   within that bound).
4. **Counter-example** — on a violation Quint always prints a concrete
   state-by-state **trace** showing exactly how the property broke. There is
   never an "I think there's a bug" without reproduction steps.

`[ok]` from the simulator is **not a proof** — it only means no violation was
found in the sampled runs. Only `quint verify` gives a guarantee (bounded).

---

## 1. System requirements

| Component        | Requirement                          | Needed for                                      |
| ---------------- | ------------------------------------ | ----------------------------------------------- |
| **Node.js**      | `>= 18`                              | Installing/running the `quint` CLI (npm)        |
| **npm**          | bundled with Node                    | install                                         |
| **Java (JDK)**   | `>= 17` (Temurin or Zulu)            | `quint verify` with the default Apalache backend |
| **Rust (cargo)** | recent stable                        | only if building the Rust evaluator from source |
| **Nix**          | optional, flakes enabled             | one reproducible dev shell with everything       |
| **Git**          | any                                  | cloning the repo (source builds/contributions)  |

Easy to miss: the **JDK is only needed at verify time**. `quint run` (simulator)
and `quint repl` work with just Node.js. The model checker requires JDK ≥ 17.

## 2. Installation

You do **not** need to clone the repo to use Quint — it's a CLI tool.

```sh
# npm (most common)
npm i @informalsystems/quint -g
quint --version            # verify

# Homebrew (macOS/Linux)
brew install quint

# Nix (great for CI — pins exact versions)
nix shell "github:NixOS/nixpkgs#quint"        # current shell only
nix profile add "github:NixOS/nixpkgs#quint"  # permanent
```

Prebuilt binaries (if you can't install Node): https://github.com/informalsystems/quint/releases

**Building from source** (only to modify Quint itself):
```sh
git clone https://github.com/informalsystems/quint.git
cd quint/quint        # the CLI package lives in the nested quint/ folder
npm install
npm run compile        # tsc build + codegen
npm link                # global symlink to your local build
```
Re-run `npm run compile` after each change. `nix develop` from the repo root
gives a full shell (Node, JDK, Rust, Go). The Rust evaluator (`--backend=rust`,
the default for `run`/`test`/`repl`) is auto-downloaded and cached by the CLI
the first time it's needed — normal users never build it manually.

## 3. Editor setup (recommended)

Specs are plain `.qnt` files, but LSP support (inline type errors, hover,
go-to-def) helps a lot:
- **VSCode**: Extensions panel → search "Quint" → install (backed by the Quint
  Language Server, published as `@informalsystems/quint-language-server`).
- **Neovim**: `npm i @informalsystems/quint-language-server -g`, copy
  `editor-plugins/vim/quint.vim` into `~/.config/nvim/syntax`, add LSP wiring.
- **Vim / Emacs**: see `editor-plugins/vim/` and `editor-plugins/emacs/`.
- **Helix**: built in upstream, nothing to install.

## 4. Language essentials (for writing specs)

Qualifiers (modes) — pick the most restrictive that compiles:

| Qualifier  | Can read state? | Can update state? | Use for                                   |
| ---------- | --------------- | ----------------- | ----------------------------------------- |
| `pure val` | no              | no                | a constant value                          |
| `pure def` | no              | no                | a pure function of its arguments          |
| `val`      | yes             | no                | a derived read-only value (e.g. an invariant) |
| `def`      | yes             | no                | a function that reads state               |
| `action`   | yes             | yes (via `x'`)    | a state transition                        |
| `temporal` | yes             | n/a               | a temporal property (`always`/`eventually`) |
| `run`      | yes             | yes               | a scripted test scenario for `quint test` |

The effect system **enforces** these: if a `def` reads a `var`, or an `action`
fails to update a variable exactly once per step, you get an effect error.

Core building blocks:
- `var name: T` — a state variable. `val`/`def` — derived/computed. `const` — a
  spec parameter (instantiated later).
- `init` — action defining the initial state(s). `step` — action defining one
  transition (what can happen each tick).
- `x' = expr` — assign the next-state value of `x`. Every `var` must be updated
  exactly once along any taken action.
- `all { a, b, c }` — **conjunction of actions**: every clause must hold; acts as
  a guard + simultaneous update. If any clause is false the action is *disabled*.
- `any { a, b, c }` — **disjunction**: non-deterministically take one enabled branch.
- `nondet x = S.oneOf()` — bind `x` to a non-deterministically chosen element of
  set `S` (the source of branching the checker explores).
- Invariant: a `val` returning `bool`, e.g.
  `val no_negatives = ADDRESSES.forall(a => balances.get(a) >= 0)`.

Common operators: sets (`Set(...)`, `.forall`, `.exists`, `.oneOf`, `.union`,
`.map`, `.filter`), maps (`a -> b` type, `.get`, `.set`, `.setBy`, `.keys`,
`.mapBy`), ints (unbounded), `1.to(100)` ranges. Doc comments use `///`.

### Worked example: `bank.qnt`

```quint
module bank {
  /// balance of each account
  var balances: str -> int

  pure val ADDRESSES = Set("alice", "bob", "charlie")

  action deposit(account, amount) = {
    balances' = balances.setBy(account, curr => curr + amount)
  }

  action withdraw(account, amount) = {
    balances' = balances.setBy(account, curr => curr - amount)
  }

  action init = {
    balances' = ADDRESSES.mapBy(_ => 0)
  }

  action step = {
    nondet account = ADDRESSES.oneOf()
    nondet amount = 1.to(100).oneOf()
    any {
      deposit(account, amount),
      withdraw(account, amount),
    }
  }

  /// every account stays non-negative
  val no_negatives = ADDRESSES.forall(addr => balances.get(addr) >= 0)
}
```

This spec has a **deliberate bug**: `withdraw` has no guard, so a balance can go
negative.

## 5. The core workflow

```sh
# (a) fast static checks
quint parse bank.qnt          # syntax only
quint typecheck bank.qnt      # types + effects (read/update discipline)

# (b) simulate: find a violation fast
quint run bank.qnt --invariant=no_negatives
#   → [violation] + a counter-example trace
quint run bank.qnt --invariant=no_negatives --mbt
#   → adds Model-Based-Testing metadata so you see WHICH action caused it
```

**Fix** — add a guard with `all { ... }` so withdraw needs sufficient funds:

```quint
action withdraw(account, amount) = all {
  balances.get(account) >= amount,                              // precondition
  balances' = balances.setBy(account, curr => curr - amount),  // update
}
```

```sh
# (c) re-simulate → should print [ok]
quint run bank.qnt --invariant=no_negatives

# (d) verify: exhaustive guarantee up to a step bound (needs JDK >= 17)
quint verify bank.qnt --invariant=no_negatives                 # Apalache (default)
quint verify bank.qnt --invariant=no_negatives --backend=tlc   # TLC instead
```

`verify` checks all executions up to `--max-steps` (default 10). The first run
auto-downloads and caches the right Apalache version and launches it as a local
gRPC server ("Shai").

### Recommended day-to-day loop
1. Edit the `.qnt` in your editor (LSP feedback as you type).
2. `quint typecheck` — fast static loop.
3. `quint run --invariant=...` — quick simulation; iterate until violations stop
   (or until you've fixed the real bugs it finds).
4. `quint test` if the spec defines `run`-based scenarios.
5. Once the simulator is consistently `[ok]` after a long run (bump
   `--max-samples` and `--max-steps`), switch to `quint verify` for the bounded
   exhaustive guarantee.
6. If `verify` is too slow: shrink constants, add an inductive invariant
   (`--inductive-invariant`), or use `--witnesses` to sanity-check assumptions
   first.

The REPL (`quint repl`, or bare `quint`, optionally `quint -r bank.qnt::bank`)
lets you evaluate expressions interactively to sanity-check definitions.

## 6. Command summary

| Command | Purpose |
| --- | --- |
| `quint` / `quint repl` | interactive REPL; preload with `quint -r file.qnt::module` |
| `quint parse <file>` | syntax check only |
| `quint typecheck <file>` | types + effects, no execution |
| `quint run <file> --invariant=<name>` | random simulation, checks invariant each step |
| `quint test <file>` | run `run`-defined test scenarios |
| `quint verify <file> --invariant=<name>` | exhaustive model check (Apalache default, `--backend=tlc`) |
| `quint compile <file> --target=tlaplus\|json` | emit TLA+ source or Apalache JSON IR |
| `quint docs <file>` | render docs from `///` docstrings |

Key flags: `--max-samples` (sim executions to try; default 10000, or 1 with
`--seed`), `--max-steps` (trace length; 20 for run, 10 for verify), `--seed=0x...`
(reproduce a run — printed on every violation), `--n-traces` (multiple
counter-examples; cannot exceed `--max-samples`), `--out-itf=trace.json` (export
trace as Informal Trace Format for model-based testing), `--witnesses` (count how
often an expression holds), `--backend=rust|typescript` (evaluator; rust is
default & faster), `--main` (name the main module), `--out` (write to file),
`--verbosity=0..5` (0 = silent, for scripting). **Full reference:**
`reference/commands.md`.

## 7. Troubleshooting

- **`verify` hangs / can't connect to server.** Apalache runs as a background
  gRPC server ("Shai") that Quint manages. A stale process or a failed
  auto-download (check network/proxy) breaks it. Try `--apalache-version` or
  `--server-endpoint` (default `localhost:8822`) if running your own Apalache.
- **`verify` Java error.** Confirm `java -version` ≥ 17. Multiple JDKs is the
  usual cause — ensure the one on `PATH`/`JAVA_HOME` is ≥ 17 (Temurin 17 / Zulu 17).
- **`run` finds no violation but you suspect a bug.** The simulator only samples;
  `[ok]` ≠ proof. Increase `--max-samples`/`--max-steps`, or use `quint verify`.
- **`--n-traces cannot be greater than --max-samples`.** Intentional validation —
  raise `--max-samples` or lower `--n-traces`.
- **`--<flag> can not be specified more than once`.** Only array options
  (`--invariants`, `--witnesses`, `--hide`) repeat; single-value flags
  (`--invariant`, `--seed`) reject duplicates.
- **Hard-to-parse type/effect errors.** Run `quint typecheck` alone. A mode error
  ("expected Pure effect, found Read effect") usually means a definition reads a
  `var` its qualifier doesn't permit — relax the qualifier (`pure def` → `def`)
  or stop reading state. See the FAQ on `pure def`/`def`/`val`/`const`.
- **Windows.** The CLI runs fine on Windows. Note the project's own
  `integration-tests/lang/io.md` (stdout matching) aren't run on Windows due to
  CRLF differences — a test-suite limitation, not a tool limitation.

## 8. Linking the spec to real Python code (Model-Based Testing)

`quint verify` proves properties about the **model**, not your code — it never
reads the codebase. To make the spec say something about a running **Python**
system you build a **chain of trust**:

```
verify(model)             ⟹ property P holds in the model (up to N steps)
  + conformance(model≈code) ⟹ the code behaves like the model
  ─────────────────────────────────────────────────────────────
  ⟹ property P holds in the real Python code
```

The link is **Model-Based Testing**: Quint emits execution traces; a small Python
harness replays each step against the real code and asserts the real state equals
the model's state. If they diverge, either the code has a bug or the spec is
inaccurate. **Full guide + copy-paste code: `reference/mbt-python.md`.** Summary:

**Two key concepts.**
- **Invariant** — a `val: bool` over state vars that must hold in *every*
  reachable state (e.g. `val no_negatives = ADDRESSES.forall(a => balances.get(a) >= 0)`).
  It is the correctness property `verify`/`run` check. Different from a *guard*
  (enables one action) and a *temporal* property (constrains whole sequences).
- **Adapter** — the Python glue that makes an abstract trace executable. Three
  jobs: (1) **action mapping** — dispatch on `mbt::actionTaken` + `mbt::nondetPicks`
  to call the real function; (2) **abstraction function** — snapshot the real
  system into the spec's `var` shape; (3) **assertion** — `abstract(real) == model`
  each step. Written once per spec, in your test suite; it is the single place
  that breaks loudly when spec and code drift.

**Generate traces** (note: the Quint tool is Node; the harness is Python):
```sh
quint run bank.qnt --invariant=no_negatives --mbt \
      --max-steps=20 --n-traces=50 --out-itf=traces/run.itf.json   # valid traces
quint verify bank.qnt --invariant=no_negatives --out-itf=traces/cex.itf.json  # bug trace
```
`--mbt` adds `mbt::actionTaken` (which action) and `mbt::nondetPicks` (the chosen
args, option-typed) to every state. ITF values are tagged (`{"#bigint":"42"}`,
`{"#map":[...]}`, `{"#set":[...]}`) — decode them (reader provided in the reference).

**Replay with pytest** (skeleton — full version in `reference/mbt-python.md`):
```python
@pytest.mark.parametrize("trace_path", glob.glob("traces/*.itf.json"))
def test_spec_conformance(trace_path):
    bank = BankService()                              # real code under test
    for i, state in enumerate(load_trace(trace_path)):
        apply_action(bank, state["mbt::actionTaken"],  # (1) action mapping
                     state.get("mbt::nondetPicks", {}))
        assert abstract(bank) == state["balances"], (  # (2)+(3) abstract + assert
            f"divergence at step {i} after {state['mbt::actionTaken']}")
```

The honesty of the whole link rests on the **abstraction function**: map only
*observable* state, project to the spec's shape, keep it total/deterministic. If
conformance fails but the code looks correct, suspect `abstract()` or the action
mapping first. Wire both checks into CI: `quint verify` the spec **and** replay
fresh traces with `pytest` on every run.

## 9. Learning path & resources

- **Lessons**: https://quint-lang.org/docs/lessons — `hello`, `booleans`,
  `integers`, `sets`, and a worked `coin` example.
- **`examples/`** in the repo: real specs by domain — `classic`,
  `cosmos` (bank, IBC/ICS-20, ICS-23, light client, Tendermint), `cosmwasm`,
  `cryptography`, `games`, `puzzles`, `solidity` (ERC20, auctions), `spells`
  (reusable utility modules), `verification`.
- **Language manual**: https://quint-lang.org/docs/lang
- **FAQ**: clarifies `pure def` vs `def`, `val` vs `def`, `pure val` vs `const`,
  and how Quint compares to TLA+, Alloy, Coq/Isabelle/Lean, fuzzing.
- **Quint LLM Kit**: https://github.com/informalsystems/quint-llm-kit — Claude
  Code agents/commands for generating specs from code/docs (`/spec:next`).
- **Internals** (for tool contributors): see `reference/architecture.md`.

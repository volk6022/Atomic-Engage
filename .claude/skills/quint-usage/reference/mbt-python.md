# Linking a Quint spec to real Python code (Model-Based Testing)

How to build a feedback loop between a Quint specification and an existing
**Python** codebase, so that the spec actually says something about the running
system. Companion to `../SKILL.md`. Self-contained.

> The Quint **tool** runs on Node.js (`npm i @informalsystems/quint -g`). The
> **conformance harness** described here is plain Python (stdlib only) and lives
> inside your project's test suite (e.g. `pytest`). These are two separate
> processes: Quint generates trace files; Python replays them against your code.

---

## 1. The core idea (and what is NOT possible)

`quint verify` **does not read your code**. It proves properties about the
*model* — an abstract state machine. The spec and the code are two separate
artifacts. There is no live binding where `verify` inspects the codebase.

What you build instead is a **chain of trust**:

```
   verify(model)            ⟹  property P holds in the model (up to N steps)
        +
   conformance(model ≈ code) ⟹  the real code behaves like the model
   ──────────────────────────────────────────────────────────────────
        ⟹  property P holds in the real code too
```

The link is the **conformance test**: Quint emits execution traces, and a Python
harness replays each trace step against the real code, asserting that the real
state matches the model's state at every step. This is **Model-Based Testing
(MBT)**. If they diverge, either the code has a bug or the spec is inaccurate —
either way you found a real discrepancy.

```
┌───────────┐ quint run --mbt   ┌────────────┐  Python harness   ┌─────────────┐
│ spec.qnt  │ --out-itf=t.json  │ trace.itf  │  (pytest)         │ real Python │
│ (model)   │──────────────────▶│  .json     │──────────────────▶│ code + ASSERT│
│ +invariant│ --n-traces=100    │ steps:     │  step-by-step      │ state==spec │
└───────────┘                   │ action,    │                   └─────────────┘
      ▲                         │ picks,state│                          │
      └────────── divergence = spec and code disagree ──────────────────┘
```

---

## 2. What is an *invariant* (in this context)?

An **invariant** is a boolean expression over the state variables that must be
**true in every reachable state** of the machine. In Quint it is a read-only
definition returning `bool`:

```quint
val no_negatives = ADDRESSES.forall(addr => balances.get(addr) >= 0)
val conservation = balances.values().sum() == total_minted
```

- It is the **correctness property** — the "what must always be true" of your
  system. Examples: no account goes negative; total supply is conserved; at most
  one process holds the lock; a processed order is never re-processed; a cache
  entry never outlives its TTL.
- `quint run --invariant=no_negatives` checks it on **sampled** executions;
  `quint verify --invariant=no_negatives` checks it **exhaustively** up to
  `--max-steps`. On failure you get a concrete counter-example trace.
- Distinguish from neighbours:
  - a **guard / precondition** (inside `all { cond, x' = ... }`) *enables* an
    action — it controls when a transition may happen, it is not a global property.
  - a **temporal property** (`always`, `eventually`) constrains whole *sequences*
    of states, not a single state. An invariant is the single-state case.

**Why it matters for the code link:** the invariant is the thing `verify` proves
about the model. In the Python harness you usually don't re-check the invariant
(the model checker already did) — you check **state conformance** (real state ==
model state). But asserting the invariant on the *real, abstracted* state too is
a cheap, valuable extra sanity check (see the harness below).

---

## 3. What is an *adapter*?

The **adapter** (a.k.a. harness/driver) is the glue code that makes an abstract
trace *executable* against concrete code. It exists because the spec is
**abstract and non-deterministic** while the code is **concrete**: something has
to translate each abstract step into real calls and verify the real outcome.

An adapter has exactly **three responsibilities**:

1. **Action mapping (dispatch)** — for each step, look at `mbt::actionTaken`
   (which `action` fired) and `mbt::nondetPicks` (the chosen arguments), and call
   the corresponding real function with the corresponding real arguments.
2. **Abstraction function** — take a snapshot of the real system and project it
   into the *same shape* as the spec's `var`s (e.g. real `Account` rows →
   `{name: balance}`). This is the heart of the binding (see §7).
3. **Assertion** — after each step, assert `abstract(real_state) == model_state`.
   On mismatch, report the step, the action, and both states.

Write the adapter once per spec, in your test suite. When you add/rename an
action or change the state shape in either the spec or the code, the adapter is
the single place that breaks loudly — which is exactly the drift-detection you
want.

---

## 4. The ITF format (what the trace file contains)

ITF = **Informal Trace Format**, a simple JSON from the Apalache ecosystem.
Top-level keys: `#meta`, `vars`, `states`. Each entry in `states` is a map from
variable name → value for that state. With `--mbt`, two extra "variables" appear
in every state:

- `mbt::actionTaken` — string, the name of the `action` taken to reach this state.
- `mbt::nondetPicks` — record of the `nondet` choices; each field is an
  **option** (`Some(v)` / `None`) because a pick may not occur in every action.

Values are **tagged** so types survive JSON:

| Quint value      | ITF JSON encoding                          | Decodes to (Python)        |
| ---------------- | ------------------------------------------ | -------------------------- |
| `int` (unbounded)| `{"#bigint": "42"}`                        | `int`                      |
| `Set(...)`       | `{"#set": [ ... ]}`                         | `frozenset`                |
| map `a -> b`     | `{"#map": [[k, v], ...]}`                   | `dict`                     |
| tuple            | `{"#tup": [ ... ]}`                         | `tuple`                    |
| record           | plain `{"field": v, ...}` (no `#` keys)    | `dict`                     |
| list             | plain `[ ... ]`                            | `list`                     |
| `Some(v)`/`None` | `{"tag":"Some","value":v}` / `{"tag":"None"}` | unwrap with a helper    |
| str / bool       | plain                                       | `str` / `bool`             |

Keys starting with `#` (like `#meta`) are metadata, not state — skip them.

---

## 5. Generating traces

```sh
# Many VALID random traces from a (correct) spec — exercises the code broadly.
# --mbt adds actionTaken/nondetPicks; --n-traces writes multiple files
# (an index is inserted into the filename).
quint run bank.qnt --invariant=no_negatives --mbt \
      --max-steps=20 --n-traces=50 --out-itf=traces/run.itf.json

# A single COUNTER-EXAMPLE trace from the model checker (when it finds a bug) —
# turn this into a regression test against the real code.
quint verify bank.qnt --invariant=no_negatives --out-itf=traces/cex.itf.json

# Reproduce one exact run (the seed is printed on every violation):
quint run bank.qnt --invariant=no_negatives --mbt --seed=0x1a2b3c \
      --out-itf=traces/repro.itf.json
```

Generate **valid** traces (from a passing spec) to get positive coverage: they
walk the real code through many model-legal scenarios you'd never hand-write.
Generate **counter-example** traces to pin down specific bugs.

---

## 6. The Python harness

### 6.1 `itf.py` — a minimal ITF reader (stdlib only)

```python
# itf.py — minimal Informal Trace Format reader, no external dependencies.
import json
from typing import Any


def _decode(v: Any) -> Any:
    """Recursively turn ITF-tagged JSON into native Python values."""
    if isinstance(v, dict):
        if "#bigint" in v:
            return int(v["#bigint"])
        if "#map" in v:
            return {_decode(k): _decode(val) for k, val in v["#map"]}
        if "#set" in v:
            return frozenset(_decode(x) for x in v["#set"])
        if "#tup" in v:
            return tuple(_decode(x) for x in v["#tup"])
        if "#unserializable" in v:
            return v["#unserializable"]
        return {k: _decode(val) for k, val in v.items()}  # plain record
    if isinstance(v, list):
        return [_decode(x) for x in v]
    return v  # str, bool, small int, None


def load_trace(path: str) -> list[dict]:
    """Return a list of states; each state maps var-name -> decoded value."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    states = []
    for s in raw["states"]:
        states.append(
            {k: _decode(val) for k, val in s.items() if not k.startswith("#")}
        )
    return states


def pick(picks: dict, name: str):
    """Unwrap a `nondet` pick. With --mbt, picks are option-typed records
    {"tag":"Some","value":...} / {"tag":"None"}; tolerate raw values too."""
    v = picks.get(name)
    if isinstance(v, dict) and v.get("tag") in ("Some", "None"):
        return None if v["tag"] == "None" else v["value"]
    return v
```

### 6.2 `test_conformance.py` — the adapter + pytest

```python
import glob
import pytest

from itf import load_trace, pick
from mybank import BankService          # <- your real code under test


# (2) ABSTRACTION FUNCTION: real system -> the shape of the spec's vars.
def abstract(bank: BankService) -> dict:
    # spec var: `balances: str -> int`  ==>  a plain {name: balance} dict
    return {acc: bank.balance_of(acc) for acc in bank.accounts()}


# (1) ACTION MAPPING: drive the real code from one trace step.
def apply_action(bank: BankService, action: str, picks: dict) -> None:
    if action == "init":
        bank.reset(["alice", "bob", "charlie"])
    elif action == "deposit":
        bank.deposit(pick(picks, "account"), pick(picks, "amount"))
    elif action == "withdraw":
        bank.withdraw(pick(picks, "account"), pick(picks, "amount"))
    else:
        raise AssertionError(f"unmapped action: {action!r}")


# (optional) the invariant, re-checked on the REAL abstracted state.
def no_negatives(state: dict) -> bool:
    return all(bal >= 0 for bal in state.values())


@pytest.mark.parametrize("trace_path", glob.glob("traces/*.itf.json"))
def test_spec_conformance(trace_path):
    bank = BankService()
    for i, state in enumerate(load_trace(trace_path)):
        action = state["mbt::actionTaken"]
        picks = state.get("mbt::nondetPicks", {})

        apply_action(bank, action, picks)          # run the real code

        expected = state["balances"]               # model's next state
        actual = abstract(bank)                     # real state, abstracted

        # (3) ASSERTION: real code must match the model, step by step.
        assert actual == expected, (
            f"divergence at step {i} after action {action!r}:\n"
            f"  model: {expected}\n  code : {actual}"
        )
        # extra safety net: the property must also hold on the real state.
        assert no_negatives(actual), f"invariant broken in real code at step {i}"
```

Run it: `pytest test_conformance.py -v`. Each generated trace becomes a test
case; a divergence prints the exact step, action, and both states.

---

## 7. The abstraction function — the part that actually matters

The whole binding is only as honest as `abstract()`. Rules of thumb:

- **Map only observable state.** A `var` in the spec must correspond to something
  you can *read* from the system: a field, a DB query, an API response, an
  in-memory structure. If you can't observe it, don't model it as a `var`.
- **Project to the spec's shape, lossily on purpose.** The spec is an
  abstraction: it deliberately ignores detail (timestamps, IDs, encoding). The
  abstraction function throws away exactly that detail so the comparison is
  meaningful. E.g. model `balances: str -> int` ignores currency precision, audit
  log, account metadata — `abstract()` collapses real `Account` objects to
  `{name: integer_balance}`.
- **Keep it total and deterministic.** Given a real state, `abstract()` must
  always return the same model-shaped value with no side effects.
- **When state lives in a DB/external store,** `abstract()` does the reads (or
  uses a test transaction / fixture). For services, hit the same getters your
  app exposes.

If conformance fails and the code is actually correct, the bug is almost always
in `abstract()` or in the action mapping — check those before "fixing" code.

---

## 8. Reverse direction: the spec as an oracle/monitor

The same ITF pipe runs backwards. Record **real** executions (sequence of
operations + state snapshots) from your integration tests or production logs,
encode them as ITF, and check them against the spec — the spec becomes an
**oracle** answering "was this observed scenario even reachable/legal in the
model?". Useful for catching "the code did something the model never allowed."
(In practice: emit each real op as an `mbt::actionTaken` + snapshot via your
`abstract()`, then assert each transition is one the spec permits.)

---

## 9. Wiring into CI

```yaml
# Two independent checks per CI run:
# 1) the model itself still satisfies its properties
- run: quint verify spec/bank.qnt --invariant=no_negatives --max-steps=15

# 2) regenerate traces and replay them against the Python code
- run: |
    mkdir -p traces
    quint run spec/bank.qnt --invariant=no_negatives --mbt \
          --n-traces=50 --max-steps=20 --out-itf=traces/run.itf.json
- run: pytest test_conformance.py -v
```

Commit the counter-example traces you turn into regression tests
(`traces/cex_*.itf.json`) so they run forever; regenerate the random `run`
traces fresh each CI run (optionally pin `--seed` for reproducibility).

---

## 10. Recipe: adding a spec to an existing Python project

1. **Pick one state machine slice** with clear state + transitions (payment flow,
   a lock/mutex, an allocator, cache invalidation, an order FSM). Never spec the
   whole app.
2. **Observable state → `var`s.** List what you can read from the system.
3. **Entry points → `action`s.** API calls, methods, message handlers. Encode
   real preconditions as `all { guard, x' = ... }`.
4. **Properties → invariants.** The things that must always hold (§2).
5. **Debug the model:** `quint run --invariant=...`, then `quint verify`.
6. **Write the adapter** (`itf.py` + `test_conformance.py`): action mapping,
   `abstract()`, assertion. Generate traces, replay with `pytest`.
7. **Wire CI** (§9): verify the spec + replay traces every run.
8. **Keep them in sync:** when an action or the state shape changes in code or
   spec, update both; the adapter fails loudly on drift.

---

## 11. Pitfalls

- **Abstraction gap.** The link is only as good as `abstract()` + action mapping.
  These are hand-maintained — they are the main source of false confidence.
- **Bounded checking.** `verify` is exhaustive only up to `--max-steps`; `[ok]`
  means "no bug within N steps", not an absolute proof.
- **State explosion.** Large constants kill `verify` — keep models small; use an
  inductive invariant (`--inductive-invariant`) when needed.
- **No live binding.** The spec is not extracted from code and does not watch it
  at runtime; synchronisation is maintained by CI + the conformance test.
- **`nondetPicks` are option-typed.** Always unwrap via the `pick()` helper; a
  raw `picks["x"]` may be `{"tag":"Some","value":...}`, not the value itself.
- **Non-determinism in the real code** (timestamps, random IDs, ordering) must be
  projected away by `abstract()`, or pinned via fixtures/seeds, or the comparison
  will flap.
```

# provision-arms — what a live NodeProvision CR actually rests at

`arm7.sh` measures one thing: **does a NodeProvision CR rest at not-ready after its
transition lands, or does it re-dispatch?** Three repos relied on the answer before
anyone had read it.

## Running it

```
bash tests/provision-arms/arm7.sh --self-test        # no cluster needed; runs in CI
bash tests/provision-arms/arm7.sh --analyze FILE.csv # re-derive a verdict from data
VER=0.73.0 ARM7_OUT=/some/dir bash tests/provision-arms/arm7.sh   # the full arm
```

The full arm builds its own throwaway kind cluster (`arm7-provision`) and deletes it.
It **never** touches the parked `ironic-kog` lab, and it asserts that before starting.

## The baselines, and why both are committed

| file | oasgen/rdc | window | ordering A result |
|---|---|---|---|
| `arm7-samples-0.61.0.csv` | 0.61.0 | 831s = 4.6 resync periods | 1 dispatch, rests at `Pending` |
| `arm7-samples-0.73.0.csv` | 0.73.0 | 1147s = 6.4 resync periods | 1 dispatch, rests at `Pending` |

The measurement has a **version coordinate**, so it needs at least two runs. The
dispatch count is identical across both (A=1, B=2), which is what makes "0.73.0 did
not change this" a measurement rather than a reading of `types.go`. A future run at
the tag that ships `restsAtNotReady` is the acceptance test, and it needs these two
to compare against — which is why the data is committed and not just the script.

Each row carries its own `ver`. The analyzer **refuses** a CSV holding more than one
version rather than summing across them, because a filename does not survive
concatenating two runs into one table.

## Why the self-test is a standing screen

Every verdict this arm can print has been wrong at least once:

- **v1** accepted an invalid Ironic transition (`active` from `enroll`), counted a
  Debug-gated line that was structurally always 0, and put a newline inside a CSV
  field. It crashed before printing a false verdict — luck, not design.
- **v6** wired the dispatch count to `latched-absence-repeats` and so read **5
  suppressions as 5 dispatches**, printing `RE-DISPATCH_LOOP` on a run that in fact
  confirmed the premise. It nearly went to two repos as "0.73.0 regresses
  NodeProvision". rdc's own source names which counter is which: `inferred` counts
  the main path *"where a create does follow"*, `latched` counts *"observes that took
  the LATCH"*.
- The **self-test itself** could not fail: it printed `exit 4 (want 4)` without
  comparing the two, and `--self-test` ended in a hardcoded `exit 0`. Both are fixed,
  and the suite is mutation-tested — reintroducing the v6 defect, or deleting either
  CSV-width gate or the version gate, each turns it red.

So case 9 (a flat `inferred` with a climbing `latched` must read premise-holds and
must never print `RE-DISPATCH`) is the control for the defect that actually happened,
and case 10 exists because a mutation test proved nothing covered a wrong-width
header.

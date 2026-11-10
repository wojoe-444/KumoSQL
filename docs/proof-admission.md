# Proof admission

[Plain-language version](../docs_simple/proof-admission.md)

An eval's results file (`benchmarks/results/<eval>.json`) holds a count of proven pairs, and the README scoreboard is generated from it. Until this check nothing recorded *which* pairs were counted, so a prover rule that newly proved a pair raised the count with no more evidence than the eval's own short random run. The admission check closes that gap: a proof is counted only after it has been through the heavy checks, and a gate test fails when a count moves without them.

It has three parts: a ledger of admitted proofs per eval, a tool that admits new proofs, and the near-miss probe that the tool runs on each of them.

```sh
python tools/proof_admission.py status                        # ledger size against each results file
python tools/proof_admission.py check                         # fast: every results file counts what its ledger admits
python tools/proof_admission.py admit qed-calcite             # prove every pair, check the new proofs, update the ledger
python tools/proof_admission.py admit qed-calcite --dry-run   # report unadmitted and lost proofs, write nothing
python tools/proof_admission.py admit singh --baseline        # start a ledger from today's proofs, unchecked
```

## The ledger

`benchmarks/admitted/<eval>.txt` has one line per admitted proof: `<digest> <how> <pair>`. The digest is twelve hex digits over the eval's name and the pair exactly as the eval's [re-check adapter](proof-recheck.md#adapters) lists it, so a pair whose text changes is a new pair. `how` says what stands behind the line:

| `how` | Meaning |
| --- | --- |
| `baseline` | Counted before the ledger existed. Covered by the earlier [proof re-check](proof-recheck.md#what-the-runs-found) rounds, not by this tool. |
| `checked` | The executed search and the near-miss probe both ran and found nothing. |
| `searched` | Only the executed search ran: a held-out pair (never mutated), or an eval whose pairs cannot be mutated. |
| `unrunnable` | DuckDB cannot run the pair, so no executed check exists; the proof stands on the prover alone. |

The tool writes the file sorted by pair; it is not edited by hand (`tests/test_proof_admission.py` rewrites every ledger and compares).

## Admitting a proof

`admit <eval>` proves every pair exactly as the eval does (through the adapter in `tools/recheck/`) and then:

- a pair already in the ledger with the same digest is **kept** if it is still proven and **dropped** if not;
- every other proven pair is a **new proof** and goes through two checks:
  1. the heavy executed search of the proof re-check (`tools/recheck/engine.py`; 3,000 databases or 120 seconds by default): exhaustive tiny databases, edge databases and random ones, with DuckDB's optimizer on and, for a sample, off;
  2. the [near-miss probe](#the-near-miss-probe) (24 single-site mutants by default), unless the pair is held-out or the eval's pairs cannot be mutated.
- a new proof with no difference is added; one with a difference is a **false proof**: it is printed with its witness, it is not added, and the command exits 1. A check that timed out or crashed leaves the pair **unsettled** (not added, exit 1; rerun with a larger `--budget` or `--seconds`).

`--dry-run` proves everything and lists the proofs that are not admitted yet and the ledger lines that are no longer proven, without the checks and without writing; it is the exact comparison for a scheduled run. `--baseline` starts a ledger for an eval that has none and marks every current proof `baseline`.

## The gate

`python tools/proof_admission.py check` reads two small files per eval: the ledger and the results file's `coverage.proven` (or the number that opens `score`). They must be equal. A higher count means a proof was counted without the checks. A lower count means the ledger still lists a proof the eval lost, which would let a new, unchecked proof take its place without the count moving. `tests/test_proof_admission.py` runs this check in the pull-request gate, so a pull request that changes a covered eval's score has to run `admit` for it and commit the ledger with the results file.

The count is a tripwire, not the whole check: a prover change that loses one proof and gains another leaves it unchanged. `admit --dry-run` is the precise comparison and belongs with the scheduled eval runs.

Evals with a ledger today: the SQLSolver suites (Calcite, Spark, TPC-H, TPC-C), QED, mined Calcite, R-Bot, Cosette, SPES, and VeriEQL's Calcite-397 and Literature suites; 1,696 `baseline` lines, started on 2026-11-10. Every ledger matched its results file except TPC-H, whose row said 22 while the prover proved 21 (pair 20, lost to a dialect bug in nested proofs); the check found that on its first run. Once that bug was fixed the same day, `admit sqlsolver-tpch` put pair 20 through the search and the probe, and it is the first `checked` line. VeriEQL Calcite-397 joined the same day with its row corrected from 335 to 333 (see [what the checks found](#what-the-checks-found)). An eval joins by `admit <eval> --baseline` once its adapter's proven count equals its results file. The evals without a ledger are not gated.

An eval that scores a proven pair as something other than a proof says which pairs it counts: its adapter defines `counted(item)`, and the tool asks that instead of whether the adapter builds a case. VeriEQL's suites do (`VeriEQLSuite` in `tools/recheck/verieql.py`): the eval searches for a counterexample before it tries to prove, and a pair it refutes is scored as different whatever the prover says.

## The near-miss probe

```sh
python tools/proof_mutants.py --list
python tools/proof_mutants.py qed-calcite --jobs 3 --out proof-mutants
python tools/proof_mutants.py qed-calcite --pairs testAggregateMerge --per-pair 0 --budget 5000
python tools/proof_mutants.py --show proof-mutants/qed-calcite.jsonl
```

A rule written with an eval's failing pairs in view proves those pairs. The risk is that it also proves their neighbours: the same shapes with `<` for `<=`, an inner join for a left join, a missing `DISTINCT`, one grouping key fewer. `tools/proof_mutants.py` builds single-site mutants of each pair with `kumosql.query_mutants` and proves each mutated pair exactly as the eval proves its own: the left query against a mutant of the right one (`right`), a mutant of the left one against the right one (`left`), and each query against its own mutant (`self`). Every mutated pair the prover accepts goes to the executed search. A database that separates the two queries makes the record `differs`, a false proof with its witness; `survived` records are the mutants that really are equivalent plus whatever the search could not separate, and `--show` prints them for reading. Held-out pairs are never read.

An eval can be probed when its adapter sets `mutable` to the dialect its pairs are written in (the nine Calcite-family evals above; the VeriEQL suites are admitted on the search alone). The first run, on QED, is recorded under [what the checks found](#what-the-checks-found); the other eight have not been probed as a whole yet.

## What the checks found

Hunting by hand on 2026-11-10, with BigQuery as the reference, found three families of false proofs, and tracing a stale score the same day found a fourth. All four were in how the prover reads SQL, not in the benchmark pairs, and the DuckDB-based checks could not have found the second and third because DuckDB reads those queries differently from BigQuery:

| Family | Example the prover called equivalent | Fix |
| --- | --- | --- |
| A decimal literal next to a NUMERIC or BIGNUMERIC operand read as a double | `WHERE n = 0.1` and `WHERE 1 = 0`; `b = 0.1` and `b = 0.10000000000000001` | [`smt_literal_coercion.py`](provers.md#numbers-and-runtime-errors) |
| An `ORDER BY` expression reading a select alias as the source column | `SELECT b AS a FROM t ORDER BY a + 1 LIMIT 1` and the same with `ORDER BY t.a + 1` | [`_split_limit`](provers.md#a-name-that-is-both-a-select-alias-and-a-column) |
| A `QUALIFY` window reading a select alias as the source column | `SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1` and the same with `ORDER BY t.a` | [`clause_alias_reads.py`](provers.md#a-name-that-is-both-a-select-alias-and-a-column) |
| An `ORDER BY` column read as an unaliased `CAST` of it in the select list | `SELECT id, CAST(b AS STRING) FROM t ORDER BY b LIMIT 1` and the same with `ORDER BY 2` (BigQuery sorts 9 before 10 on the left and `'10'` before `'9'` on the right) | [`_split_limit`](provers.md#a-name-that-is-both-a-select-alias-and-a-column) names a select-list entry only by its alias or by the bare column it is |

The first family's NUMERIC half was reported by the bug hunt in the split prover repository. None of the nine ledger evals of that day changed its count with the three fixes.

One eval outside the ledger did, and nothing flagged it: VeriEQL Calcite-397 lost pair 8 (`ORDER BY deptno` beside an unaliased `CAST(deptno AS DOUBLE)` in the select list), which the new `ORDER BY` guard refused as a name used for two outputs. The eval comparison run for that change did not include VeriEQL. Re-measuring the row afterwards found it had been stale for a month as well: pairs 147 and 204 had been declined by soundness guards merged in early October while the row still said 335.

Pair 8 led to the fourth family. sqlglot gives an unaliased `CAST(b AS STRING)` the name `b`, and since 2026-10-02 `_split_limit` had matched `ORDER BY` keys against those names, so `ORDER BY b` could be read as ordering by the cast. All three fuzzers, the near-miss probe and the 38-eval comparison passed with it in place; none of them writes an unaliased cast of a sort key into a select list. With the name taken only from an alias or a bare column, pair 8 is proved for the right reason, the row says 333, and both VeriEQL suites have a ledger so that a count and its proved pairs can no longer part silently.

The automated checks, run the same day while the fixes were being written:

| Check | Size | Result |
| --- | --- | --- |
| `tools/soundness_fuzz.py --seed 20261110 --count 20000` | 20,000 generated pairs, 10,109 proved | 0 false proofs |
| Near-miss probe, QED (`--modes right,left --per-pair 12 --budget 300 --seconds 15`) | 335 of the 375 pairs (stopped there for time), 3,814 mutated pairs, 1,367 accepted by the prover | 0 `differs` |
| 130 hand-written non-equivalent pairs (numeric edges, NULL logic, joins, aggregates, windows, set operations) through the three provers | 130 | 1 hit, the `ORDER BY` family |

Of the 3,814 mutated QED pairs the prover accepted 1,367 (36%), and every accepted one survived 300 databases. 969 of them (71%) add or drop a `DISTINCT` over a keyed table, which changes nothing; the rest are aggregates swapped over one-row groups, comparisons and predicates implied by a join or sitting on a join whose other side is not read, and constants changed in filters over empty inputs. A sample of each kind was read by hand.

## Limits

- Surviving the search and the probe is evidence, not a proof. A database the search never builds can still separate a pair.
- DuckDB is the oracle. A pair whose meaning differs between its own dialect and DuckDB (the alias families above) is outside what these checks can see; such semantics need a run on the real engine.
- `baseline` lines carry no evidence from this tool.
- Only eleven evals have a ledger. The count comparison cannot see a lost proof replaced by a new one; `admit --dry-run` can.
- The gate compares two files. A prover change that loses a proof does not fail it until someone re-measures the eval; the scheduled `admit --dry-run` is what notices, and it is not scheduled yet.
- The probe mutates one site at a time and only the constructs `kumosql.query_mutants` knows.

## Tests

`tests/test_proof_admission.py`: the gate check on the repository's ledgers, the ledger format, `admit` with stand-in checks (kept, admitted, lost, false, unsettled, a changed pair, `--dry-run`, `--baseline`), the order of the two checks and the held-out rule, an eval that says which proven pairs it counts, the probe's mutant labels and cap, and a probe run against a deliberately careless prover, which it must catch.

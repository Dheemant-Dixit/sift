# Answer-quality evals

`tests/` proves sift's code does what it says. It cannot prove sift gives good
answers — `tests/conftest.py` makes real embedding calls *raise*, so nothing
there ever sees a real vector. That leaves every retrieval constant in the
product unprotected: `child_size`, `child_min`, `doc_head_chars`, `min_score`.

This directory is that missing net.

```bash
pytest evals/          # 17 tests, needs Ollama + both default models, ~60s
```

It is **not** part of `pytest` (`testpaths` is `tests/`) and **not** in CI,
because it needs a model server and is not deterministic enough to gate a
merge. It skips itself, rather than failing, when Ollama isn't running.

## What it pins

Two failures, both of which shipped in 0.1.x and were fixed at ingestion in
0.2.0. Neither is hallucination — in both cases every value is real and every
citation is correct.

| | failure | layer | what fixed it |
|---|---|---|---|
| **1** | a question about the *landlord's* account number retrieves the *payslip's* | index | larger indexed units (`child_min` 200) |
| **2** | "what is my designation?" returns the HR signatory's job title | serve | the document's opening line on each passage (`doc_head_chars` 120) |

### How much each test is worth — measured, not assumed

A test that passes under the old settings too is decoration. So both were
re-run against this corpus with 0.2.0's ingestion reverted:

| arm | account passage served for the landlord query | designation |
|---|---|---|
| `child 200/66`, no head | **served at 0.56** | 3/3 correct, 0 wrong-entity |
| `child 300/200`, head 120 | none | 3/3 correct, 0 wrong-entity |

**Read that honestly.** Defect 1 is genuinely pinned: revert `child_min` and
the account passage walks straight back into the context, and the test fails.

**Defect 2 is not.** The attribution trap does not reproduce on invented
documents — both arms answer correctly. It survived several attempts to
reconstruct it, and each attempt that worked did so by tuning the fixture until
it broke, which is fitting, not testing. The real failure depended on the
embedding geometry of one real Form 16: the employee's own title had to score
*below* the signatory's clause, and that margin is not something a synthetic
document reproduces reliably.

So `test_designation_answers_about_the_employee_not_the_signatory` is a
**guard, not a proof**. It asserts the right outcome and would catch a gross
regression, but it has not been shown to fail under the old settings. Do not
read it passing as evidence that attribution is safe.

The honest way to close that gap is a private corpus overlay — point
`SIFT_SOURCE` at real documents and run the same questions — which is exactly
what the original investigation did and what a public repository cannot ship.

## The corpus is synthetic, and why

These questions were originally scored against one person's real Downloads
folder — payslips, a Form 16, a rental agreement. That corpus cannot go in a
public repository, and redacting it would have destroyed the exact structure
that causes the failures.

`corpus/` is fictional and reconstructs that structure instead:

- **`form16_acme.txt`** names its subject once, at the top, then contains a
  *different* person's job title next to the literal word "designation" in the
  verification clause. This is what makes defect 2 reproduce.
- **`payslip_march_2026.txt`** carries an account number on a self-contained
  line among salary rows. Long enough to survive as its own indexed unit when
  `child_min` is small — which is what makes defect 1 reproduce — and absorbed
  into payroll text when it isn't.
- **`rental_agreement_sunwood.txt`** names a landlord and explicitly says his
  banking details are *provided separately*, so the landlord's account number
  is genuinely absent from the corpus. The honest answer is "it isn't here."
- **`distributed_systems_notes.md`** is the prose genre: self-describing
  paragraphs that don't depend on a heading pages above. It's the easy case,
  and it's here so a fix aimed at forms can't silently break it.

Every name, number and identifier is invented.

## The relevance bar is 0.50 here, not the product default

`min_score` is **0.50** in this suite, and `README.md`'s warning is the reason:
0.55 was measured on a different set of documents and does not transfer. This
corpus is a live demonstration of that — it's a smaller, cleaner set of
documents, and everything scores a little lower.

Measured separation on this corpus:

```
lowest positive   0.528   ("what is my designation?")
highest negative  0.479   ("what is my blood group?")
```

0.50 sits in that gap. It was chosen once, from that measurement, and is fixed
in `evalset.py`. **No test may move it.** If the corpus changes, recalibrate by
measuring the separation again — never by nudging the number until something
passes. That distinction is the whole difference between an eval and a
rubber stamp.

## Rules

Written down because they are easy to violate accidentally:

- **Score on value markers, never on absence-of-refusal.** A model that says
  nothing useful must not count as correct.
- **Markers are compiled case-insensitively**, always, via `compile_marker()`.
  They were once compiled bare, and it scored three correct answers as misses
  because the model capitalised differently than the corpus did. Making the
  flag structural is the fix; a convention would rot again.
- **Negative controls must stay refused.** Admitting one is a failure however
  many positives pass.
- **Every question is asked more than once.** A single sample from a
  temperature-0.1 local model is not evidence, and the assertions cover all
  runs — so a marker that only sometimes appears is a failure, not a coin flip
  that happened to land well.

## Files

| file | what it is |
|---|---|
| `evalset.py` | the pre-registered questions, markers, negatives and bar |
| `conftest.py` | builds a real index over `corpus/` with the real pipeline |
| `test_answer_quality.py` | the assertions |
| `corpus/` | four synthetic documents |

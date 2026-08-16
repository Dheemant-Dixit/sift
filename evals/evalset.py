"""The pre-registered evaluation set.

WHAT THIS IS FOR
  The unit suite in tests/ proves sift's code does what it says. It cannot
  prove sift gives good answers: tests/conftest.py makes real embedding calls
  raise, so nothing there ever sees a real vector. Every retrieval constant in
  the product — child_size, child_min, doc_head_chars, min_score — is therefore
  unprotected by it. This file is that missing net.

WHY A SYNTHETIC CORPUS
  These questions were originally scored against one person's real Downloads
  folder, which cannot go in a public repository. The corpus in corpus/ is
  fictional and reproduces the same two structural failures, so the regression
  is pinned in a form anyone can run and nobody has to redact:

    1. wrong-entity attribution — form16_acme.txt names its subject once, at
       the top, and later contains a signatory's job title next to the literal
       word "designation". Ask "what is my designation?" and pre-0.2.0 chunking
       hands back the signatory's title: a real value, correctly cited, wrong
       person.

    2. identifier leakage — payslip_march_2026.txt contains a bank account
       number on a short line of its own. Ask for the *landlord's* account
       number, which appears nowhere in the corpus, and small indexed units let
       that line match on specificity alone.

RULES, FIXED IN ADVANCE
  - Scoring is on value markers. Never on absence-of-refusal: a model that
    says nothing useful must not score as correct.
  - Negative controls must stay refused. Admitting one is a failure however
    many positives pass.
  - The relevance bar is calibrated ONCE for this corpus, below, and no test
    may move it. It is 0.50 rather than the product default of 0.55 because
    0.55 was measured on a different set of documents and does not transfer —
    which is exactly what README.md warns users about, and this corpus is a
    live demonstration of it. Measured separation here: the lowest positive
    scores 0.528, the highest negative 0.479. 0.50 sits in that gap. Recalibrate
    by measurement if the corpus changes; never nudge it to make a test pass.
  - Markers are compiled case-insensitively, always, by compile_marker() below.
    They used to be compiled bare, and it silently scored three correct answers
    as misses because the model capitalised differently than the corpus did.
    Making the flag structural is the fix; a convention would rot again.
"""
from __future__ import annotations

import re
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"

# Calibrated for this corpus; see RULES above. Not the product default.
MIN_SCORE = 0.50


def compile_marker(pattern: str) -> re.Pattern:
    """Compile a scoring marker. Case-insensitive is not optional — see above."""
    return re.compile(pattern, re.I)


# (query, genre, regex the answer must contain)
POSITIVES = [
    # forms: key-value documents, the genre where attribution breaks
    ("what is my designation?",            "form",     r"staff data engineer"),
    ("what is my employee code?",          "form",     r"\b884120\b"),
    ("who is my employer?",                "form",     r"acme cloud systems"),
    ("what is my provident fund contribution?", "form", r"provident fund|\bPF\b"),

    # contracts: prose-like legal clauses
    ("what is the notice period?",         "contract", r"\b60\b"),
    ("how much is the security deposit?",  "contract", r"1,20,000|120000|one lakh twenty"),
    ("what is the lock-in period?",        "contract", r"\b6\b|six"),
    ("who is the owner of the property?",  "contract", r"ramesh krishnamurthy"),

    # prose: self-describing paragraphs, the easy genre
    ("how does consistent hashing work?",  "prose",    r"consistent hash|ring"),
    ("what is a bloom filter?",            "prose",    r"bloom filter"),
    ("what is write ahead logging?",       "prose",    r"write ahead log|log record"),
]

# Must stay refused. The first is the one that matters: its answer is genuinely
# absent from the corpus, and a payslip is sitting there full of digits.
NEGATIVES = [
    "what is the landlord's bank account number?",
    "what is my blood group?",
    "what is the airspeed of an unladen swallow?",
]

# The specific wrong answer that 0.2.0 exists to prevent: the Form 16
# signatory's title, offered when the employee's own title was asked for.
WRONG_ENTITY = compile_marker(r"senior payroll controller")
DESIGNATION_QUERY = "what is my designation?"
LANDLORD_QUERY = "what is the landlord's bank account number?"

# Values that live in the corpus and must never surface in an answer to a
# question that did not ask for them. All fictional.
ACCOUNT_NUMBER = "90210044556677"

OWNED_IDENTIFIERS = {
    ACCOUNT_NUMBER: "bank account number",
    "101998776543": "UAN",
    "884120": "employee code",
    "MCBK0000417": "IFSC code",
}

# A citation legitimately repeats a filename; strip those before leak-hunting
# so "[payslip_march_2026.txt]" is not mistaken for a leaked value.
CITATION = re.compile(r"\[[^\]]*\]|\b[\w,. -]+\.(?:pdf|docx|txt|md)\b", re.I)

REFUSAL = compile_marker(
    r"couldn't find|could not find|not (?:contain|include|provide|specif)|"
    r"no .{0,20}(?:information|mention|record)|does not appear|"
    r"unable to (?:find|locate)|isn't (?:in|available)|is not (?:in|available)"
)


def leaked_identifiers(reply: str) -> list[str]:
    """Owned values that appear in the reply body, ignoring citations."""
    body = CITATION.sub(" ", reply)
    return sorted({label for value, label in OWNED_IDENTIFIERS.items() if value in body})

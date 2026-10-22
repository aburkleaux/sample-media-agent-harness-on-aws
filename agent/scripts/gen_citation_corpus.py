#!/usr/bin/env python3
"""Generate the cross-language citation corpus (media-harness-search-qa-rendering task 3.4).

Writes a committed, deterministic set of citation *parse inputs* to
``infra/lib/projects/fixtures/citation-corpus.json``. The corpus feeds Property 8
(the Python/TypeScript parser-agreement test, task 3.9): the agreement test runs
BOTH ``parse_citations`` (Python) and ``parseCitations`` (TypeScript) over the same
inputs and compares their results.

This generator therefore stores ONLY input strings. It deliberately does NOT compute
or commit any expected parse result — the expected output is whatever both parsers
agree on at test time, not something this script decides. It never imports the
citation parser; ``agent/src/project_search/citations.py`` is consulted only to pick
realistic string shapes (names, timecodes, the two dash code points), not for output.

The draw is seeded (``random.Random(20261004)``), so re-running reproduces the file
byte-for-byte. Task 3.9's ``test_corpus_in_sync`` regenerates the corpus in memory and
fails, naming this script, when the committed file differs.

The 2,000 inputs mix:
  * valid citations (both dash code points, varied interior spacing),
  * boundary timecodes (``00:00``, ``59:59``, the ``h:mm:ss`` rollover at ``1:00:00``),
  * web-citation links — a citation-shaped span immediately followed by ``(...)``,
    which the grammar's trailing ``(?!\\()`` must leave plain,
  * malformed brackets (missing/extra ``[`` or ``]``),
  * nested brackets,
  * non-ASCII display names (``İ`` U+0130, ``ß`` U+00DF, astral code points), and
  * pure adversarial-alphabet noise drawn from the design's alphabet.

Usage:
  python3 agent/scripts/gen_citation_corpus.py            # write the committed file
  python3 agent/scripts/gen_citation_corpus.py --stdout   # print, do not write
"""

from __future__ import annotations

import argparse
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(
    os.path.join(HERE, "..", "..", "infra", "lib", "projects", "fixtures", "citation-corpus.json")
)

# The seed is fixed by the design (Testing Strategy, Property 8). Changing it rewrites
# the whole committed corpus and must be a deliberate, reviewed change.
SEED = 20261004
COUNT = 2000

# --- The adversarial alphabet (design: Testing Strategy, Property 8) --------------------
# Structural grammar characters, digits, both dashes, U+0020, every TRIM_CHARS member
# (mirrors citations.TRIM_CHARS — kept inline so this script imports nothing from the
# parser), CR, LF, the dotted-capital-I and sharp-s, and a few astral code points.
_STRUCT = ["[", "]", "(", "@", ":"]
_DIGITS = list("0123456789")
_DASHES = ["\u2013", "\u002d"]  # en-dash (what the formatter writes) and hyphen-minus
# citations.TRIM_CHARS, verbatim. U+0020 (space) is its first member.
_TRIM = list(
    "\u0020\u0009\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u202f\u205f\u3000\ufeff"
)
_NEWLINES = ["\r", "\n"]
_LETTERS = ["\u0130", "\u00df"]  # İ, ß
_ASTRAL = ["\U0001f600", "\U0001f3ac", "\U0001f4a9", "\U00010000"]  # emoji + linear-B start

ALPHABET = _STRUCT + _DIGITS + _DASHES + _TRIM + _NEWLINES + _LETTERS + _ASTRAL

# Display-name shapes (realistic + awkward), chosen to exercise trimming, interior '@',
# over-length rejection, and non-ASCII normalization. Not expected output — just inputs.
_NAMES = [
    "clip.mp4",
    "nba.mp4",
    "still.png",
    "long.mov",
    "doc",
    "a@b",
    "shot 2",
    "İstanbul.mp4",
    "straße.wav",
    "reel\U0001f3ac.mp4",
    "a" * 256,  # one past NAME_MAX (255): parser must reject
    "",  # empty name: parser must reject
]

# Interior spacing variants around '@' and the dash. Includes assorted TRIM members so
# both languages' trim sets are exercised, plus a tab which neither trims mid-token.
_SPACERS = ["", " ", "  ", "   ", "\u00a0", "\u2009", "\t"]


def _rand_timecode(rng: random.Random) -> str:
    """A timecode fragment: in-range mm:ss, boundary values, the h:mm:ss form, or an
    out-of-range value the grammar rejects. Inputs only — validity is the parser's call."""
    kind = rng.randrange(6)
    mm = rng.randint(0, 59)
    ss = rng.randint(0, 59)
    if kind == 0:
        return f"{mm:02d}:{ss:02d}"
    if kind == 1:
        return rng.choice(["00:00", "59:59", "00:01", "00:59"])  # boundaries
    if kind == 2:
        return f"{rng.randint(0, 9)}:{mm:02d}:{ss:02d}"  # h:mm:ss
    if kind == 3:
        return rng.choice(["1:00:00", "1:00:01", "0:00:00"])  # hour rollover boundaries
    if kind == 4:
        return f"{rng.randint(60, 99)}:{ss:02d}"  # minutes out of [0-5]\d range
    return f"{rng.randint(1, 9)}:{ss:02d}"  # single-digit leading field (grammar rejects)


def _wrap(rng: random.Random, core: str) -> str:
    """Optionally surround a token with adversarial noise so matches are not always
    anchored at string boundaries."""
    def noise(max_len: int) -> str:
        return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, max_len)))

    return noise(6) + core + noise(6)


def _citation(rng: random.Random, name: str, dash: str) -> str:
    s = _SPACERS
    return (
        f"[{rng.choice(s)}{name}{rng.choice(s)}@{rng.choice(s)}"
        f"{_rand_timecode(rng)}{rng.choice(s)}{dash}{rng.choice(s)}{_rand_timecode(rng)}"
        f"{rng.choice(s)}]"
    )


def _gen_one(rng: random.Random) -> str:
    """Draw a single corpus input. Category chosen by the seeded rng so the full set is
    reproducible yet covers every shape the design and task 3.4 call for."""
    category = rng.randrange(8)
    name = rng.choice(_NAMES)
    dash = rng.choice(_DASHES)

    if category == 0:  # pure adversarial-alphabet noise
        return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 40)))
    if category == 1:  # a well-formed-looking citation (may still be rejected on bounds)
        return _wrap(rng, _citation(rng, name, dash))
    if category == 2:  # web-citation link: citation span immediately followed by (...)
        return _wrap(rng, _citation(rng, name, dash) + "(https://example.test/x)")
    if category == 3:  # nested opening bracket before the name
        junk = rng.choice(["a ", "[", "x[", "see "])
        return _wrap(rng, "[" + junk + _citation(rng, name, dash)[1:])
    if category == 4:  # malformed: drop one bracket
        c = _citation(rng, name, dash)
        return _wrap(rng, c[1:] if rng.random() < 0.5 else c[:-1])
    if category == 5:  # malformed: extra brackets around a citation
        return _wrap(rng, "[[" + _citation(rng, name, dash) + "]]")
    if category == 6:  # non-ASCII / astral name forced
        nn = rng.choice(["İstanbul.mp4", "straße.wav", "\U0001f600clip", "a\U00010000b"])
        return _wrap(rng, _citation(rng, nn, dash))
    # category == 7: a citation embedded in a sentence of noise
    return _wrap(rng, "See " + _citation(rng, name, dash) + " now")


def generate(seed: int = SEED, count: int = COUNT) -> list[str]:
    """The committed corpus as a list of input strings. Deterministic for a given seed."""
    rng = random.Random(seed)
    return [_gen_one(rng) for _ in range(count)]


def build_document(seed: int = SEED, count: int = COUNT) -> dict:
    return {
        "description": (
            "Seeded cross-language citation parse-input corpus for Property 8 "
            "(media-harness-search-qa-rendering task 3.4). INPUT STRINGS ONLY — no expected "
            "parse result is stored; the agreement test (task 3.9) runs both the Python "
            "parse_citations and the TypeScript parseCitations over these inputs and compares. "
            "Regenerate with: python3 agent/scripts/gen_citation_corpus.py"
        ),
        "seed": seed,
        "count": count,
        "inputs": generate(seed, count),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--count", type=int, default=COUNT)
    ap.add_argument("--stdout", action="store_true", help="print the corpus, do not write")
    a = ap.parse_args()

    doc = build_document(a.seed, a.count)
    text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    if a.stdout:
        print(text, end="")
        return
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(
        f"wrote {os.path.relpath(OUT)}: {doc['count']} input strings, seed {doc['seed']}"
    )


if __name__ == "__main__":
    main()

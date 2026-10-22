#!/usr/bin/env python3
"""Census the Improve wire: every key the producer emits, and whether anything renders it.

Run from the repo root. Exits 1 if any key is emitted but unreadable, so this can gate a change
rather than only describe one:

    python3 scripts/improve-wire-census.py            # summary + the unread lists
    python3 scripts/improve-wire-census.py --all      # every key with its verdict

Why a key census and not a byte measure: bytes are dominated by a few long prose fields, so a
payload can be 90% "rendered" by weight while most of its FACTS have no consumer. A key is one
fact. Counting keys is what answers "does a reader ever see this".

Three verdicts, and only the first is done work:

    READ      declared in a wire type AND named in a component or the fetch layer
    UNREAD    declared, nothing names it            -> a consumer was never written
    UNTYPED   emitted by the producer, declared in no wire type -> the UI *cannot* read it

Comments are stripped from every input before matching. That matters more than it sounds: a key
mentioned only in a docstring reads as wired to a naive grep, and most of this file's value is
refusing to count that.

Known limits, stated so a reader does not over-trust the number:

* A bare word-boundary match cannot tell a wire read from a local variable of the same name, so
  READ is optimistic and UNREAD is a floor. It also cannot tell a read from a WORD INSIDE A
  USER-FACING STRING: `'runs not attributed'` in a footnote scored `attributed` as read while
  nothing rendered `scorecard.attribution` at all, so 0 of its 5 fields were on screen where this
  reported 3. Before trusting a READ verdict on a short or common name, grep it and look at the
  match.
* The producer side matches dict-literal keys, so a key built by interpolation is missed.
* UNTYPED counts camelCase only, because the producers also build snake_case dicts for the log
  summary that never travel. A wire key that is a single lowercase word therefore slips past it —
  `discoverable` is one, emitted and undeclared today. Grep those by hand when adding one.
* **This is a census of NAMES, not of paths.** A key declared on one interface satisfies the check
  for every other object that carries the same name, so a field added to a second place is invisible
  here. Real example: `symptom` is declared on `ResidueEntry`, so emitting it on a pattern too reads
  as already typed. Same for `window`, `state`, `alarms`, `platform`, `blastRadius`, `runs`. When a
  change puts an existing name on a NEW object, check that object's interface by hand — the number
  below will not move.

Treat every count here as a floor, never a ceiling — in both directions.
"""
from __future__ import annotations

import glob
import re
import sys

#: Wire types live in TWO files: the contract mirror, and the fetch layer's own envelope
#: (`ImproveData`, which carries top-level keys the mirror does not). Reading only the first
#: reported an already-declared key as untyped.
TYPE_FILES = ("ui/src/improve-api.ts", "ui/src/improve-data.ts")
#: Everything that can put a pixel on screen, plus the fetch layer that normalises for them.
CONSUMER_GLOBS = ("ui/src/components/improve/*.ts", "ui/src/components/improve/*.tsx")
CONSUMER_FILES = ("ui/src/improve-data.ts",)
#: The two mappers that build the served item. Both, because the global and per-actor halves
#: carry different keys and a field on only one of them is the usual asymmetry.
PRODUCERS = ("agent/src/improve/dto.py", "agent/src/improve/aws_lambda.py")


#: A DynamoDB primary-key expression is not a payload, and the producer scan cannot tell the two
#: apart by shape — both are dict literals with string keys. `Key={"actorId": ..., "sk": ...}` names
#: the ITEM's partition and sort keys; the served payload is the item's `bundle` attribute alone
#: (`ui/server/app.ts` spreads `item.bundle`, and `capability_api.improve_dto.with_actor_bundle`
#: builds from the same place), so `actorId` never reaches a browser and no wire type may declare it.
#: Stripped rather than allowlisted by name, so the next key attribute added is excluded for the same
#: reason instead of being reported as an unreadable wire key.
#:
#: Flat by DynamoDB's own rule — a key attribute is a scalar — so a non-greedy match to the first
#: closing brace cannot swallow a nested payload.
_DDB_KEY_EXPRESSION = re.compile(r"\bKey=\{[^{}]*\}")


def uncommented(path: str) -> str:
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    return re.sub(r"^\s*(//|#).*$", "", text, flags=re.M)


def main() -> int:
    # Members of declared interfaces only, at any depth — a nested inline literal
    # (`registryMeta: { componentCount: number }`) declares a wire key just as a top-level member
    # does, and missing those reported already-typed fields as untyped. `improve-data.ts` also holds runtime code, whose object
    # literals would otherwise read as wire declarations.
    declared_set: set[str] = set()
    for path in TYPE_FILES:
        for body in re.findall(r"\binterface\s+\w+[^{]*\{(.*?)\n\}", uncommented(path), flags=re.S):
            declared_set |= set(re.findall(r"[{;\n]\s*([A-Za-z]\w*)\??:", body))
    declared = sorted(declared_set)

    consumers = [p for g in CONSUMER_GLOBS for p in glob.glob(g)] + list(CONSUMER_FILES)
    consumers = [p for p in consumers if ".test." not in p and "improve-api" not in p]
    # improve-data.ts is BOTH a type source and a consumer (it normalises for the components).
    blob = "".join(uncommented(p) for p in consumers)

    emitted: set[str] = set()
    for path in PRODUCERS:
        body = _DDB_KEY_EXPRESSION.sub("", uncommented(path))
        emitted |= set(re.findall(r'"([A-Za-z]\w*)":\s', body))

    named = lambda key: re.search(rf"\b{re.escape(key)}\b", blob) is not None
    read = [k for k in declared if named(k)]
    unread = [k for k in declared if not named(k)]
    # camelCase ONLY. The producers also build snake_case dicts for the log summary and the local
    # artifact; neither travels on the wire, and counting them swamped the real finding at 60 entries
    # of which most were noise.
    #
    # This does NOT cover a DynamoDB item attribute, which this comment used to claim: those are
    # camelCase like a wire key, so `actorId` off a `Key=` expression was reported here as a key the
    # UI could not read. `_DDB_KEY_EXPRESSION` is what excludes it, above.
    untyped = sorted(k for k in emitted if k not in declared and re.search(r"[a-z][A-Z]", k))

    print(f"wire keys declared  : {len(declared)}")
    print(f"read by a consumer  : {len(read)}")
    print(f"TYPED but unread    : {len(unread)}")
    print(f"EMITTED, not typed  : {len(untyped)}")
    if "--all" in sys.argv:
        print()
        for key in declared:
            print(f"  {'READ  ' if named(key) else 'UNREAD'}  {key}")
    print("\n--- declared, no consumer names it ---")
    print("  " + (", ".join(unread) or "(none)"))
    print("\n--- emitted by the producer, absent from the types ---")
    print("  " + (", ".join(untyped) or "(none)"))
    return 1 if unread or untyped else 0


if __name__ == "__main__":
    raise SystemExit(main())

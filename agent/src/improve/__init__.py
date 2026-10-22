"""Improve — the Observe → Evaluate → Improve learning loop.

Additive package. Nothing here modifies the runtime request path; the loop reads
telemetry/evidence and (in later phases) proposes governed edits to versioned
harness entities. See ideas/learning-framework.md for the design and
ideas/implementation-plan.md for the phased build.

Phase M1 (this slice) implements the OBSERVE + EVALUATE stages on the universal
contract types (contract.py), so a run can FAIL honestly. No proposer.
"""

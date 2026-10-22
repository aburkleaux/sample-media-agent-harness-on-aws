"""OBSERVE stage — reconstruct a faithful, cross-runtime Run per session.

For Phase M1 the source is harvested span data (offline); a live CloudWatch
Logs Insights query against ``aws/spans`` is the same shape and is the M2
durable path. Either way the trajectory is stitched by session id across all
six runtimes (learning-framework.md Part 3; implementation-plan REQ-004).
"""

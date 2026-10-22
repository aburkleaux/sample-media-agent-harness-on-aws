"""Render domain package.

Helper modules for the render pipeline — manifest assembly, type/mime mapping,
review/validation, skill-catalog reads, intent extraction, and projection —
grouped here (C1 modularization). The ``render_agent`` runtime entrypoint stays
at ``agent/src`` top level (referenced by the AgentCore runtime config); it
imports these helpers via ``from render.<module> import ...``.
"""

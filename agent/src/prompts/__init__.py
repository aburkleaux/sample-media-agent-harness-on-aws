"""Centrally managed static agent prompts.

Each module holds the system prompt(s) for one agent, extracted verbatim from the
entrypoints so they live in one reviewable place. Dynamic assembly (e.g. the orchestrator's
runtime tool/skill catalog and lab inventory) stays in the agent code; only the static text
lives here. Skills, custom-agent personas, and hooks remain data in the Lab (not here).
"""

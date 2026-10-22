"""Planning domain package.

Plan data model + schema helpers grouped here (C1 modularization): the DAG node
schema (``dag_schema``), the high-level plan reader/shape (``high_level_plan``),
and the plan manifest (``plan_manifest``). The planning ENTRYPOINT
(``planning_agent``) and the load-bearing DAG execution tool
(``plan_and_execute_tool`` — a registered tool name, read by path in tests) stay
at ``agent/src`` top level and import these via ``from planning.<mod> import ...``.
"""

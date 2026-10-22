---
name: project-knowledge-graph
description: "Use to answer questions about who, where and what is in the active project's media and how they connect — people, scenes, shots, locations, organizations, topics, files, members, tags — by turning the question into graph tool calls (graph_schema, graph_find, graph_neighbours, graph_paths, graph_context, graph_query), and to enrich media search hits with graph context."
---

# Project knowledge graph

The active project has a **knowledge graph**: nodes for the project itself, its members (`User`), tags, files
(`Asset`), and the entities extracted from its media (`Scene`, `Shot`, `Person`, `Location`, `Organization`,
`Topic`), joined by typed, directed relationships. It is the graph extension of project search: use it to answer
relationship questions search cannot ("who appears with whom", "which scenes are at this place", "how is X connected
to Y") and to explain search hits. Each rule's bold ID is stable.

## Scope

**KG-S1** The graph tools only ever read the active project's graph; the harness supplies the project. Never try to
query another project, and never mention project ids, tokens or credentials.

**KG-S2** Rows marked `sample: true` belong to the starter **sample** graph that every new project gets. They are
illustrative only: never present a sample node as a fact about the user's media. If a question can only be
answered from sample rows, say the project's own media has not been mapped into the graph yet.

## From a question to tool calls

**KG-Q1** Call `graph_schema` once per conversation before writing `graph_query`; it lists labels, relationship
types with their direction (`from` → `to`), and row counts.

**KG-Q2** Resolve every name in the question to a node id with `graph_find` first (`types` narrows it, e.g.
`["Person"]`). If several nodes match, prefer non-sample ones and say which you used; if none match, say so
instead of guessing.

**KG-Q3** Prefer the typed tools — they are exact and cheap:
- what is connected to X → `graph_neighbours` (`relationships`, `direction`, `hops` 1–2)
- how X relates to Y → `graph_paths` (≤ 3 hops)
- what is in these files / search hits → `graph_context` with their `asset_ids`

**KG-Q4** Use `graph_query` (read-only Cypher) only for counting, ranking, filtering or multi-step patterns the
typed tools cannot express. Recipes:
- most frequent co-stars of a person:
  `MATCH (a:Person)-[:APPEARS_IN]->(s:Scene)<-[:APPEARS_IN]-(b:Person) WHERE a.id = $id AND b.id <> a.id RETURN b.name AS name, count(s) AS scenes ORDER BY scenes DESC`
- people in a file: `MATCH (a:Asset)-[:HAS_SCENE]->(s:Scene)<-[:APPEARS_IN]-(p:Person) WHERE a.id = $id RETURN DISTINCT p.name AS name`
- scenes at a location: `MATCH (s:Scene)-[:LOCATED_AT]->(l:Location) WHERE l.id = $id RETURN s.name AS scene`

## Writing graph_query

**KG-C1** One `MATCH … [WHERE …] RETURN …`. Label every node (`(p:Person)`), type every relationship
(`[:APPEARS_IN]`), and write each pattern in its schema direction — an undirected `-[:R]-` returns outgoing edges
only. When both ends share a label, add `b.id <> a.id`.

**KG-C2** Not supported: `OPTIONAL MATCH`, `WITH`, `UNION`, any write, `shortestPath` (use `graph_paths`).
Variable-length paths at most `*1..3`. Use `count()`, `collect()`, `DISTINCT`, `ORDER BY`, `LIMIT`.

**KG-C3** Pass values as `$name` parameters in `params`, never pasted into the query text. Results are capped at
200 rows; if `truncated` is true, say the answer covers the top rows.

**KG-C4** If a call returns an error code (e.g. `unlabelled_node`, `unknown_label`, `query_failed`), fix the query
from the message and retry at most twice; then answer with what the typed tools returned.

## Answering

**KG-A1** Ground every relationship you state in a tool result from this turn, naming the nodes (e.g. "Uma Eriksen
appears in 12 scenes with Dev Castillo"). For a file, name it by its display name.

**KG-A2** Combine with search: when media search returns hits and the question is about people, places or topics,
call `graph_context` with the hits' `asset_ids` and use it to explain or connect them; cite the search hits as the
media-search rules require.

**KG-A3** Errors: `graph_off` — the project has no knowledge graph (the owner can enable it on the project's Graph
tab); `graph_seeding` — it is still being built, try again shortly; `policy_denied` — only the active project can be
queried. Never retry a `project_access_ended` error.

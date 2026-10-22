"""Graph schema: one Lance table per node label and per relationship type.

lance-graph resolves every Cypher label to a table and cannot plan an unlabelled node, so the schema is
explicit and closed. All node tables share one column set, and so do all edge tables, which keeps
``merge_insert`` and the payload encoder uniform.

``User`` is a Harness account (a Project member); ``Person`` is someone who appears in media. They are
deliberately different labels.

Lineage (asset-lineage Phase 3, R6): ``DERIVED_FROM`` answers "what came from this clip" over asset →
asset edges built from the ASSET row's ``derivedFrom`` list, and ``PRODUCED_BY`` / ``OUTPUT_OF`` /
``RAN_IN`` attach an asset to the session and the M2C job that made it.

New labels and relationship types are APPENDED, never inserted: the SPA resolves a node's colour by
this tuple's INDEX (``ui/src/components/projects/graph/graphTheme.ts`` → ``--graph-type-<n>``), so
inserting one in the middle silently recolours every label after it.
"""
from __future__ import annotations

NODE_LABELS: tuple[str, ...] = (
    "Project", "User", "Tag", "Asset",
    "Scene", "Shot", "Person", "Location", "Organization", "Topic",
    "Session", "Job",
)

# type -> (source label, destination label)
RELATIONSHIPS: dict[str, tuple[str, str]] = {
    "OWNS": ("User", "Project"),
    "MEMBER_OF": ("User", "Project"),
    "TAGGED": ("Project", "Tag"),
    "HAS_ASSET": ("Project", "Asset"),
    "HAS_SCENE": ("Asset", "Scene"),
    "HAS_SHOT": ("Scene", "Shot"),
    "APPEARS_IN": ("Person", "Scene"),
    "LOCATED_AT": ("Scene", "Location"),
    "ABOUT": ("Scene", "Topic"),
    "AFFILIATED_WITH": ("Person", "Organization"),
    "KNOWS": ("Person", "Person"),
    # Lineage. `DERIVED_FROM` points from the DERIVED asset to its input, so "what came from this clip"
    # is the incoming direction and "what went into this cut" the outgoing one.
    "DERIVED_FROM": ("Asset", "Asset"),
    "PRODUCED_BY": ("Asset", "Session"),
    "OUTPUT_OF": ("Asset", "Job"),
    "RAN_IN": ("Job", "Session"),
}

# Bumped when the column set or the table list changes incompatibly; recorded in _meta.json. Adding a
# table (a new label / relationship) is NOT incompatible: the reader fills a table a snapshot predates
# with an empty one, and the writer creates it on the next sync.
SCHEMA_VERSION = 1
LAYOUT = "v1"

NODE_COLUMNS: tuple[str, ...] = ("id", "name", "sample", "kind", "status", "props")
EDGE_COLUMNS: tuple[str, ...] = ("id", "src", "dst", "sample", "props")


def node_table(label: str) -> str:
    return f"nodes/{label}"


def edge_table(rel: str) -> str:
    return f"edges/{rel}"


TABLES: tuple[str, ...] = tuple(node_table(l) for l in NODE_LABELS) + tuple(edge_table(r) for r in RELATIONSHIPS)


def is_node_table(table: str) -> bool:
    return table.startswith("nodes/")


def table_label(table: str) -> str:
    """``nodes/Asset`` -> ``Asset``; ``edges/OWNS`` -> ``OWNS``."""
    return table.split("/", 1)[1]


def arrow_schema(table: str):
    import pyarrow as pa

    if is_node_table(table):
        return pa.schema([("id", pa.string()), ("name", pa.string()), ("sample", pa.bool_()),
                          ("kind", pa.string()), ("status", pa.string()), ("props", pa.string())])
    return pa.schema([("id", pa.string()), ("src", pa.string()), ("dst", pa.string()),
                      ("sample", pa.bool_()), ("props", pa.string())])


def graph_config():
    import lance_graph as lg

    b = lg.GraphConfig.builder()
    for label in NODE_LABELS:
        b = b.with_node_label(label, "id")
    for rel in RELATIONSHIPS:
        b = b.with_relationship(rel, "src", "dst")
    return b.build()

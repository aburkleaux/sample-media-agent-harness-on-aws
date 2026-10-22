"""A small, deterministic demo media graph (~300 nodes), every row ``sample = True``.

It shows the media schema (Asset → Scene → Shot, Person APPEARS_IN Scene, Scene LOCATED_AT / ABOUT, …) on a
brand-new Project before any extraction rules exist. It hangs off the Project node through sample
``HAS_ASSET`` edges, and the writer never deletes sample rows during a sync, so future rules can replace it
wholesale (``delete("sample = true")``) without touching project-derived rows.
"""
from __future__ import annotations

import random

from . import ids
from .starter import Rows, edge, empty_rows, node
from .schema import edge_table, node_table

DEMO_SEED = 1

_FIRST = "Ava Ben Chloe Dev Elena Farid Grace Hiro Isla Jonas Kira Leo Maya Noah Omar Priya Rosa Sami Tara Uma".split()
_LAST = "Adler Brooks Castillo Dube Eriksen Gupta Haddad Ito Larsen Moreau Okafor Rossi Silva Tanaka Weber".split()
_PLACES = "Harbour Market Station Studio Stadium Cafe Rooftop Library Airport Beach Bridge Museum Arena Plaza".split()
_ORGS = "Broadcaster Studio Club Agency Newspaper Label Charity".split()
_TOPICS = ("election climate football finance music fashion health tech space food travel art law weather "
           "film science").split()
_GENRES = ["documentary", "news", "drama", "sport", "music"]
_SHOTS = ["wide", "medium", "close-up", "aerial", "insert"]
_VERBS = ["interviews", "argues with", "celebrates with", "chases", "performs for", "meets"]

ASSETS, SCENES_PER_ASSET, PEOPLE, LOCATIONS, ORGS, TOPICS = 6, 10, 50, 18, 10, 24


def demo_rows(project_id: str, seed: int = DEMO_SEED) -> Rows:
    rng = random.Random(seed)
    out = empty_rows()
    p = ids.project_node(project_id)

    def add_node(label: str, n: int, name: str, **kw) -> str:
        nid = ids.sample_node(label, n)
        out[node_table(label)].append(node(nid, name, sample=True, **kw))
        return nid

    def add_edge(rel: str, src: str, dst: str, **kw) -> None:
        out[edge_table(rel)].append(edge(rel, src, dst, sample=True, **kw))

    people = [add_node("Person", i, f"{rng.choice(_FIRST)} {rng.choice(_LAST)}") for i in range(PEOPLE)]
    places = [add_node("Location", i, f"{rng.choice(_PLACES)} {rng.choice(_LAST)}") for i in range(LOCATIONS)]
    orgs = [add_node("Organization", i, f"{rng.choice(_LAST)} {rng.choice(_ORGS)}") for i in range(ORGS)]
    topics = [add_node("Topic", i, _TOPICS[i % len(_TOPICS)] + ("" if i < len(_TOPICS) else f" {i}"))
              for i in range(TOPICS)]

    scene_n = shot_n = 0
    for a in range(ASSETS):
        genre = _GENRES[a % len(_GENRES)]
        asset = add_node("Asset", a, f"Sample {genre} #{a + 1}", kind="video", status="sample", genre=genre)
        add_edge("HAS_ASSET", p, asset)
        cast = rng.sample(people, k=8)
        home = rng.choice(places)
        t = 0
        for _ in range(SCENES_PER_ASSET):
            dur = rng.randint(20_000, 180_000)
            lead = cast[rng.randrange(len(cast))]
            lead_name = out[node_table("Person")][people.index(lead)]["name"]
            scene = add_node("Scene", scene_n, f"{lead_name} {rng.choice(_VERBS)} a guest",
                             startMs=t, endMs=t + dur)
            add_edge("HAS_SCENE", asset, scene)
            for _k in range(rng.randint(1, 3)):
                shot = add_node("Shot", shot_n, rng.choice(_SHOTS))
                add_edge("HAS_SHOT", scene, shot)
                shot_n += 1
            for person in {lead, *rng.sample(cast, k=rng.randint(0, 2))}:
                add_edge("APPEARS_IN", person, scene, confidence=round(rng.uniform(0.6, 0.99), 2))
            add_edge("LOCATED_AT", scene, home if rng.random() < 0.7 else rng.choice(places))
            for topic in set(rng.sample(topics, k=rng.randint(1, 2))):
                add_edge("ABOUT", scene, topic)
            t += dur
            scene_n += 1

    for person in people:
        if rng.random() < 0.7:
            add_edge("AFFILIATED_WITH", person, rng.choice(orgs))
    pairs = set()
    for _ in range(PEOPLE):
        a, b = rng.sample(people, k=2)
        if (a, b) not in pairs:
            pairs.add((a, b))
            add_edge("KNOWS", a, b)
    for rows in out.values():
        rows.sort(key=lambda r: r["id"])
    return out

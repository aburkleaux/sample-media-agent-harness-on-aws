"""Template Assembler — pure plan→``OMSTemplate`` assembly (R5, R6, R10).

This module is the "assembly seam" of the `Persistence_Tool`: it turns a
fully-mapped :class:`~persistence.models.ResolutionReport` (every step resolved
to exactly one ``service.action``) plus its source ``HighLevelPlan`` into a
durable :class:`~persistence.models.OMSTemplate`. It is pure logic — no boto3,
no MCP, no S3/DDB — so it is directly unit- and property-testable.

This slice contributes ``assemble`` (task 4.1), its supporting deterministic
``task_id`` derivation, intent-scoped ``build_conditions`` (task 4.4), and the
stable-content ``derive_template_id`` (task 4.6):

- One :class:`~persistence.models.OMSTask` per mapped step, carrying
  ``task_id``, ``service``, ``action``, ``depends_on``, ``required``,
  ``timeout_seconds``, and ``config`` (R5.2, Property 9).
- ``task_id`` is a deterministic function of ``service`` + ``action`` (R5.4): a
  slug of ``f"{service}__{action}"``. When the same ``service.action`` repeats,
  a stable ordinal suffix keeps ids unique while remaining deterministic given
  the ordered mapped-step list.
- Each step's sub-goal ``depends_on`` edges are translated into the
  corresponding task ids so the plan DAG is preserved *exactly* — no edge
  dropped or added (R5.3, Property 7). A sub-goal that maps to several tasks
  (several tools) translates to all of them.
- Capability tasks are placed in ``stages.fulfill.tasks``; a ``stages.validate``
  task list is always present (R5.1).
- ``build_conditions`` scopes conditions to the discovered workflow's intent —
  the participating services plus intent keywords mined from the plan's
  goal/reasoning — and *refuses* to emit empty / match-all conditions by raising
  :class:`~persistence.models.EmptyConditionsError` (R10.1, R10.3, Property 12).
- ``derive_template_id`` is a stable hash over the ordered, normalized
  ``(service, action, sorted depends_on)`` tuples plus the scoped condition
  intent, so equivalent workflows yield the same id (enabling upsert) and
  different workflows differ (R6.1, underpins Property 10).

Requirements: 5.1, 5.2, 5.3, 5.4, 6.1, 10.1, 10.3
Properties: 7, 9, 12
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter

from planning.high_level_plan import HighLevelPlan

from .models import (
    EmptyConditionsError,
    MappedStep,
    OMSTask,
    OMSTemplate,
    ResolutionReport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Separator placed between the slugged ``service`` and ``action`` in a task id.
_TASK_ID_SEPARATOR: str = "__"

#: Suffix separator appended before the ordinal when a ``service.action`` repeats
#: (``<base>__2``, ``<base>__3`` ...), keeping repeated ids unique yet deterministic.
_TASK_ID_ORDINAL_SEPARATOR: str = "__"

#: Default per-task timeout applied to every assembled task so it is fully
#: populated. Real per-capability timeouts are a later concern.
_DEFAULT_TASK_TIMEOUT_SECONDS: int = 3600

#: The two stage buckets every assembled template carries (R5.1).
_STAGE_VALIDATE: str = "validate"
_STAGE_FULFILL: str = "fulfill"

#: Condition keys under which the scoped workflow intent is published (R10.1).
_CONDITION_SERVICES_KEY: str = "services"
_CONDITION_INTENT_KEY: str = "intent_keywords"

#: Word tokenizer for mining intent keywords. Applied to *accent-normalized*,
#: lower-cased text (see :func:`_normalize_text`) so accented words stay whole
#: ("mécanisme" -> "mecanisme") instead of fragmenting ("canisme").
_WORD_RE = re.compile(r"[a-z0-9]+")

#: Minimum token length kept as an intent keyword (drops noise like "a"/"to").
#: Deliberately kept at 3 rather than raised to 4: meaningful domain tokens are
#: 3 characters ("sfx", "wav", "mix", "hls", "srt"), so noise is filtered by the
#: stopword / low-signal sets instead of by length.
_MIN_KEYWORD_LEN: int = 3

#: Upper bound on emitted intent keywords, keeping conditions bounded and stable
#: (frequency-ranked selection with an alphabetical tie-break).
_MAX_INTENT_KEYWORDS: int = 12

#: Tokens that, standing alone, would make a condition value match-all rather
#: than scope anything — refused so conditions never match every order (R10.3).
_WILDCARD_TOKENS: frozenset[str] = frozenset({"", "*", "all", "any", "any_order", "wildcard"})

#: Very common English tokens that carry no scoping intent; excluded from the
#: mined keyword set so conditions stay meaningful (R10.1).
_STOPWORDS_EN: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "then",
        "will", "shall", "should", "would", "could", "must", "need", "needs",
        "use", "using", "used", "via", "each", "all", "any", "are", "was",
        "were", "has", "have", "had", "not", "but", "its", "his", "her",
        "their", "them", "they", "our", "your", "you", "output", "input",
        "step", "steps", "task", "tasks", "goal", "goals", "plan", "workflow",
        "user", "users", "order", "orders", "run", "runs", "get", "set",
        "make", "made", "when", "where", "which", "what", "how", "why",
    }
)

#: Very common French tokens that carry no scoping intent. Plans are frequently
#: reasoned about in French, so these must be filtered exactly like the English
#: ones. Written ACCENT-STRIPPED ("etre", "ete", "deja", "apres") because they
#: are matched against tokens produced *after* :func:`_normalize_text`.
_STOPWORDS_FR: frozenset[str] = frozenset(
    {
        # Short function words. Most are already dropped by _MIN_KEYWORD_LEN,
        # but they are listed so the set documents the full FR stopword intent
        # and stays correct if the length floor ever changes.
        "le", "la", "un", "du", "de", "d", "au", "et", "ou", "or", "ni", "ce",
        "il", "on", "je", "tu", "se", "sa", "ma", "ta", "ton", "pas", "ne",
        "oui", "non", "si", "en", "y", "a", "ca",
        # Articles / determiners / possessives
        "les", "des", "une", "aux", "cet", "cette", "ces", "son", "ses",
        "leur", "leurs", "notre", "nos", "votre", "vos", "mon", "mes", "tes",
        "chaque", "quelques", "quelque", "certains", "certaines", "plusieurs",
        "tout", "toute", "tous", "toutes", "aucun", "aucune", "autre", "autres",
        "meme", "memes", "tel", "telle", "telles", "tels",
        # Pronouns
        "elle", "elles", "ils", "nous", "vous", "lui", "eux", "cela", "celui",
        "celle", "ceux", "celles", "qui", "que", "quoi", "dont", "quel",
        "quelle", "quels", "quelles",
        # Prepositions / conjunctions / adverbs
        "avec", "sans", "sous", "sur", "dans", "par", "pour", "vers", "chez",
        "entre", "apres", "avant", "lors", "lorsque", "pendant", "depuis",
        "car", "donc", "ainsi", "mais", "puis", "ensuite", "aussi", "alors",
        "encore", "deja", "toujours", "jamais", "plus", "moins", "tres",
        "bien", "peu", "trop", "assez", "beaucoup", "afin", "selon", "comme",
        "quand", "ici", "enfin", "cas", "part", "fois", "etc", "ceci",
        # Auxiliaries / very common verb forms (low-value, non-domain)
        "est", "sont", "etre", "ete", "etait", "etaient", "sera", "seront",
        "soit", "avoir", "aura", "auront", "avait", "avaient", "eu", "ont",
        "peut", "peuvent", "pourra", "doit", "doivent", "devra", "faut",
        "faire", "fais", "font", "va", "vont",
        "fait", "faites", "agit", "permet", "permettre", "permettant",
        "utilise", "utiliser", "utilisant", "appel", "appelle", "appeler",
        "adapte", "adaptee", "adaptees", "adaptes", "adapter",
        "accompagne", "accompagnee", "accompagnees", "accompagner",
        "associe", "associee", "associees", "associes", "associer",
        "attendu", "attendue", "attendues", "attendus", "attend",
        "necessaire", "necessaires", "possible", "possibles",
        # Plan/workflow scaffolding words (French mirrors of the EN list)
        "etape", "etapes", "tache", "taches", "objectif", "objectifs",
        "plan", "commande", "commandes", "utilisateur", "utilisateurs",
        "resultat", "resultats", "sortie", "sorties", "entree", "entrees",
    }
)

#: Generic, domain-neutral scaffolding vocabulary (EN + FR). These words appear
#: in almost every plan's prose ("parametres", "sortie attendue", "exemple"), so
#: they carry no signal for MATCHING an incoming order to a template — they only
#: crowd out real domain terms. Accent-stripped, like :data:`_STOPWORDS_FR`,
#: because they are matched post-:func:`_normalize_text`.
_LOW_SIGNAL: frozenset[str] = frozenset(
    {
        "plan", "planning", "etape", "etapes", "step", "steps",
        "workflow", "tool", "tools", "outil", "outils",
        "parametre", "parametres", "parameter", "parameters",
        "valeur", "valeurs", "value", "values",
        "sortie", "sorties", "output", "outputs",
        "entree", "entrees", "input", "inputs",
        "resultat", "resultats", "result", "results",
        "appel", "appels", "call", "calls",
        "job", "jobs", "statut", "status",
        "aucun", "requis", "optionnel", "default", "defaut",
        "exemple", "example", "note", "notes",
        "attendu", "attendue", "attendues", "attendus",
    }
)

#: Combined filter applied to normalized tokens (EN + FR stopwords + low-signal).
_STOPWORDS: frozenset[str] = _STOPWORDS_EN | _STOPWORDS_FR | _LOW_SIGNAL


# ---------------------------------------------------------------------------
# Deterministic task-id derivation (R5.4)
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Lower-case slug: non-alphanumeric runs collapse to a single ``-``.

    Mirrors the house ``re.sub(r"[^a-z0-9]+", "-", ...)`` slug approach used in
    ``main_lab_tools._slugify_skill_name`` so external identifiers stay tidy.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _base_task_id(service: str, action: str) -> str:
    """The base task id for a ``service.action`` — a pure function of both.

    Deterministic and order-independent: ``f"{slug(service)}__{slug(action)}"``
    (R5.4, Property 9). Repeats of the same ``service.action`` are disambiguated
    by :func:`derive_task_ids` with a stable ordinal suffix.
    """
    return f"{_slugify(service)}{_TASK_ID_SEPARATOR}{_slugify(action)}"


def derive_task_ids(mapped: list[MappedStep]) -> list[str]:
    """Assign a deterministic, unique ``task_id`` to each mapped step, in order.

    The id of the first occurrence of a given ``service.action`` is its base id
    (:func:`_base_task_id`); each subsequent occurrence appends a stable ordinal
    (``<base>__2``, ``<base>__3`` ...). The result is a pure function of the
    ordered ``(service, action)`` sequence — re-running yields identical ids —
    so the assembler and its tests can agree on the mapping (R5.4, Property 9).

    Args:
        mapped: The resolved steps, in their stable report order.

    Returns:
        A list of task ids aligned positionally with ``mapped``.
    """
    counts: dict[str, int] = {}
    task_ids: list[str] = []
    for step in mapped:
        base = _base_task_id(step.service, step.action)
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        if seen == 0:
            task_ids.append(base)
        else:
            # Second+ occurrence -> stable ordinal suffix (2-based, human-readable).
            task_ids.append(f"{base}{_TASK_ID_ORDINAL_SEPARATOR}{seen + 1}")
    return task_ids


# ---------------------------------------------------------------------------
# Condition scoping (R10.1, R10.3) — Property 12
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """Accent-normalize then lower-case ``text`` for tokenizing.

    Decomposes with NFKD and drops combining marks, so accented words survive
    the ASCII tokenizer whole: "mécanisme" -> "mecanisme", "séparation" ->
    "separation". Without this the ``[a-z0-9]+`` tokenizer splits on the
    accented character and emits fragments ("canisme", "accompagn").
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def _extract_intent_keywords(plan: HighLevelPlan) -> list[str]:
    """Mine a stable, bounded set of intent keywords from the plan's prose.

    Draws from the plan's overall ``reasoning`` plus each sub-goal's
    ``description`` / ``reasoning`` (the closest thing the plan carries to a
    goal statement), then:

    1. **Accent-normalizes** and lower-cases the joined text
       (:func:`_normalize_text`) *before* tokenizing, so accented French words
       stay whole ("mécanisme" -> "mecanisme") instead of fragmenting.
    2. Drops **EN + FR stopwords** (:data:`_STOPWORDS`), tokens shorter than
       :data:`_MIN_KEYWORD_LEN`, and purely-numeric tokens.
    3. Selects by **relevance, not alphabet**: candidates are ranked by
       frequency (descending) across the mined text, tie-broken alphabetically
       (ascending) for determinism, and the top :data:`_MAX_INTENT_KEYWORDS` are
       kept.
    4. Emits the selected set **sorted alphabetically**, so the list is stable
       and order-independent — equivalent plans yield identical keyword sets
       (R10.1).

    Purely deterministic: no randomness, no clock, no I/O.

    Note:
        These keywords feed ``conditions``, which feed
        :func:`derive_template_id`. Changing extraction therefore changes the
        derived id for the same plan: re-persisting an equivalent plan lands on
        a *new* template id rather than upserting the previously-saved one. That
        is expected and acceptable — the id remains a pure, stable function of
        the plan under the current extraction rules.
    """
    parts: list[str] = [getattr(plan, "reasoning", "") or ""]
    for sub_goal in getattr(plan, "sub_goals", None) or []:
        parts.append(getattr(sub_goal, "description", "") or "")
        parts.append(getattr(sub_goal, "reasoning", "") or "")

    tokens = _WORD_RE.findall(_normalize_text(" ".join(parts)))
    counts = Counter(
        token
        for token in tokens
        if len(token) >= _MIN_KEYWORD_LEN
        and token not in _STOPWORDS
        and not token.isdigit()
    )
    # Most frequent first, alphabetical tie-break -> deterministic selection.
    selected = sorted(counts, key=lambda token: (-counts[token], token))[
        :_MAX_INTENT_KEYWORDS
    ]
    return sorted(selected)


def _has_scoping_value(value: object) -> bool:
    """True when ``value`` narrows the match (i.e. is not empty / wildcard).

    Recurses through lists/tuples/sets and dicts; a bare string counts only when
    it is a non-wildcard, non-empty token. This is what lets
    :func:`_is_match_all` tell "scope to the audio service" apart from an
    all-empty / ``"*"`` condition that would match every order (R10.3).
    """
    if isinstance(value, str):
        return value.strip().lower() not in _WILDCARD_TOKENS
    if isinstance(value, (list, tuple, set)):
        return any(_has_scoping_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_scoping_value(item) for item in value.values())
    # Numbers / bools / other truthy scalars narrow the match; None does not.
    return value is not None


def _is_match_all(conditions: dict) -> bool:
    """True when ``conditions`` is empty or carries no actual scoping value.

    An empty dict, or a dict whose every value is empty / wildcard, would match
    all orders — exactly what R10.3 forbids the assembler from emitting.
    """
    if not conditions:
        return True
    return not any(_has_scoping_value(value) for value in conditions.values())


def build_conditions(plan: HighLevelPlan, services: set[str]) -> dict:
    """Scope conditions to the discovered workflow's intent (R10.1, R10.3).

    The conditions narrow future orders to the workflow that was actually
    discovered: the set of participating ``services`` (the capabilities the
    template orchestrates) plus intent keywords mined from the plan's
    goal/reasoning. Both are stable, order-independent functions of their inputs
    so equivalent workflows produce identical conditions (underpinning the
    deterministic ``template_id`` in :func:`derive_template_id`).

    The assembler *refuses* to emit a match-all template: if neither a
    participating service nor an intent keyword survives scoping — i.e. the
    resulting conditions would be empty or match every order — this raises
    :class:`EmptyConditionsError` rather than returning empty / match-all
    conditions (R10.3, Property 12).

    Args:
        plan: The source plan, mined for intent keywords.
        services: The participating ``service`` names from the mapped steps.

    Returns:
        A non-empty, non-match-all conditions dict scoped to the workflow.

    Raises:
        EmptyConditionsError: If conditions cannot be scoped safely (they would
            be empty or match all orders).
    """
    scoped_services = sorted(
        {svc.strip() for svc in services if svc and svc.strip().lower() not in _WILDCARD_TOKENS}
    )
    keywords = _extract_intent_keywords(plan)

    conditions: dict = {}
    if scoped_services:
        conditions[_CONDITION_SERVICES_KEY] = scoped_services
    if keywords:
        conditions[_CONDITION_INTENT_KEY] = keywords

    if _is_match_all(conditions):
        raise EmptyConditionsError(
            "refusing to emit empty / match-all conditions: no participating "
            "service or intent keyword could scope the workflow"
        )
    return conditions


# ---------------------------------------------------------------------------
# Deterministic template-id derivation (R6.1) — underpins Property 10
# ---------------------------------------------------------------------------


def derive_template_id(steps: list[OMSTask], conditions: dict) -> str:
    """Derive a deterministic ``template_id`` from the workflow content (R6.1).

    Hashes a canonical projection of the workflow: the *ordered*, normalized
    ``(service, action, sorted depends_on)`` tuples of every task, plus the
    scoped condition intent. Because the projection is a pure function of the
    content (task order preserved, dependency lists sorted, JSON keys sorted),
    two equivalent workflows produce byte-identical payloads and therefore the
    same id — enabling the save driver's upsert idempotence (Property 10) — while
    any change to a service, action, edge, or condition yields a different id.

    Args:
        steps: The assembled tasks, in their stable emission order.
        conditions: The scoped conditions from :func:`build_conditions`.

    Returns:
        A stable ``wf-<hash>`` template id.
    """
    normalized_tasks = [
        {
            "service": task.service,
            "action": task.action,
            "depends_on": sorted(task.depends_on),
        }
        for task in steps
    ]
    payload = json.dumps(
        {"tasks": normalized_tasks, "conditions": conditions},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"wf-{digest}"


# ---------------------------------------------------------------------------
# Assembly (R5.1, R5.2, R5.3, R5.4)
# ---------------------------------------------------------------------------


def assemble(report: ResolutionReport, plan: HighLevelPlan) -> OMSTemplate:
    """Assemble an ``OMSTemplate`` from a fully-mapped resolution report.

    Pure (no I/O). Builds one :class:`OMSTask` per mapped step, derives a
    deterministic ``task_id`` from ``service`` + ``action`` (R5.4), and
    translates each step's sub-goal ``depends_on`` edges into the corresponding
    task ids so the plan DAG is preserved exactly (R5.3, Property 7). Capability
    tasks are placed in ``stages.fulfill``; a ``stages.validate`` task list is
    always present (R5.1). ``conditions`` are scoped to the workflow's intent by
    :func:`build_conditions` (which raises rather than emit match-all
    conditions), and ``template_id`` is the stable content hash from
    :func:`derive_template_id`.

    Args:
        report: The resolution report; only ``report.mapped`` is consumed here.
        plan: The source plan, used for naming/description and condition scoping.

    Returns:
        A structurally complete :class:`OMSTemplate` (R5.1, R5.2, Property 9).
    """
    mapped = list(report.mapped)

    # 1. Deterministic, unique task ids aligned with the ordered mapped steps.
    task_ids = derive_task_ids(mapped)

    # 2. sub_goal_id -> [task_id]. A sub-goal with several tools maps to several
    #    tasks, so an incoming edge must fan out to all of them (R5.3).
    subgoal_to_task_ids: dict[str, list[str]] = {}
    for step, task_id in zip(mapped, task_ids):
        subgoal_to_task_ids.setdefault(step.step.sub_goal_id, []).append(task_id)

    # 3. Build one task per mapped step, translating sub-goal edges to task edges.
    tasks: list[OMSTask] = []
    services: set[str] = set()
    for step, task_id in zip(mapped, task_ids):
        services.add(step.service)
        # Translate each depended-on sub-goal id to every task id it produced,
        # preserving order and de-duplicating (no edge added, none dropped).
        depends_task_ids: list[str] = []
        seen: set[str] = set()
        for dep_sub_goal_id in step.step.depends_on:
            for dep_task_id in subgoal_to_task_ids.get(dep_sub_goal_id, []):
                if dep_task_id not in seen:
                    seen.add(dep_task_id)
                    depends_task_ids.append(dep_task_id)
        tasks.append(
            OMSTask(
                task_id=task_id,
                service=step.service,
                action=step.action,
                depends_on=depends_task_ids,
                required=True,
                timeout_seconds=_DEFAULT_TASK_TIMEOUT_SECONDS,
                config={},
            )
        )

    # 4. Intent-scoped conditions (raises on match-all) + deterministic id.
    conditions = build_conditions(plan, services)
    template_id = derive_template_id(tasks, conditions)

    # 5. Human-facing metadata, kept non-empty and deterministic.
    service_list = ", ".join(sorted(services)) if services else "no services"
    name = f"Persisted workflow ({service_list})"
    description = (
        plan.reasoning.strip()
        if getattr(plan, "reasoning", "").strip()
        else f"Workflow assembled from {len(tasks)} discovered capability step(s)."
    )

    # 6. Stages: capability tasks fulfill; validate carries preflight (empty now).
    stages = {
        _STAGE_VALIDATE: {"tasks": []},
        _STAGE_FULFILL: {"tasks": [task.to_dict() for task in tasks]},
    }

    return OMSTemplate(
        template_id=template_id,
        name=name,
        description=description,
        conditions=conditions,
        stages=stages,
    )


__all__ = [
    "assemble",
    "derive_task_ids",
    "build_conditions",
    "derive_template_id",
]

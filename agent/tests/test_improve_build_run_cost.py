"""The cost arithmetic, and the hand-entered rates it runs on.

Two separable things are tested here, and they are kept separable on purpose:

  the ARITHMETIC   four separately-priced terms per model, cross-region prefixes normalised, and
                   PARTIAL per session with the shortfall counted in tokens. Exercised against
                   ILLUSTRATIVE rates patched over the module's table (`priced`), so a rate
                   correction on the price page can never make a formula test fail — and so a
                   formula test can never pass merely because its fixture happens to hold the same
                   numbers the module does.
  the RATES        `_BEDROCK_RATES_USD_PER_MTOK` itself: that every model this population uses has
                   a complete row, what a real span actually costs at those numbers, and that
                   `costBasis` tells a reader where they came from and what they cannot see.

Token counts below are REAL — the exact ``gen_ai.usage.*`` values from staging ``chat`` spans. The
illustrative rates are deliberately round and deliberately NOT any shipped rate, so that a test
using them is visibly testing the formula rather than the price list.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import COST_UNMEASURED_REASON, UNMEASURED  # noqa: E402
from improve.observe import build_run as br  # noqa: E402

# One real span's usage, from staging: total 90,570 = input 4 + output 831 + cacheRead 60,671 +
# cacheWrite 29,064. `input_tokens` here is the DERIVED uncached figure, not the recorded attribute.
REAL_SPAN = {"total_tokens": 90570, "output_tokens": 831, "cache_read_tokens": 60671,
             "cache_write_tokens": 29064, "input_tokens": 4, "llm_calls": 1}

# Every distinct model id the harvested population's `byModel` blocks carry, RAW — i.e. with the
# cross-region routing prefix the telemetry records. Five ids over four models, which is the whole
# reason `_normalise_model` exists and the reason the rate table cannot be keyed on what is here.
POPULATION_MODEL_IDS = (
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-sonnet-5",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

# The four token terms and the rate key each one is priced at. The pairing is the thing most easily
# got wrong, so it is spelled out here and checked term by term below.
TERMS = (("input_tokens", "input"), ("output_tokens", "output"),
         ("cache_read_tokens", "cache_read"), ("cache_write_tokens", "cache_write"))

# Round ILLUSTRATIVE rates, USD per million tokens. None of these is a shipped Bedrock rate for any
# model in the table — checked by `test_the_illustrative_rates_are_not_the_shipped_ones` — so a test
# that patches them cannot accidentally be asserting the price list.
RATES = {"anthropic.claude-opus-4-8": {"input": 7.0, "output": 40.0,
                                       "cache_read": 0.7, "cache_write": 9.0},
         "anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 4.0, "output": 22.0,
                                                      "cache_read": 0.4, "cache_write": 6.0}}

# A model id no rate table here has a row for. Used wherever a test needs "a model that appeared in
# the population after the rates were typed" — deliberately NOT one of the four real ones, so the
# test cannot be misread as a claim that a shipped model is unpriced.
UNPRICED_MODEL = "global.anthropic.claude-opus-5"

# What one `REAL_SPAN` contributes to the priced/unpriced token counts: the four PRICED categories,
# which reconstruct the span's own total exactly because uncached input is derived as the remainder.
# Spelled as the sum of the four rather than as `total_tokens` because the counts follow the terms
# that were actually priced, and a fixture asserting the total would pass even if they did not.
SPAN_PRICED_TERM_TOKENS = sum(REAL_SPAN[tokens_key] for tokens_key, _ in TERMS)


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK", RATES)
    monkeypatch.setattr(br, "_RATE_PROVENANCE", "example.invalid/prices read 2001-01-01")


# --- the shipped rates ---------------------------------------------------------------------

def test_the_shipped_table_prices_every_model_id_this_population_uses():
    """A model with no row costs the figure its own tokens. So every id must resolve.

    Keyed on NORMALISED ids, so this is also the test that a routing prefix in the telemetry does
    not defeat the table: five raw ids collapse onto four rows. A single mistyped key here — a
    missing suffix, a `.` for a `-` — would not raise anywhere; it would quietly move that model's
    tokens into the unpriced count and understate every total it appears in.
    """
    for raw in POPULATION_MODEL_IDS:
        row = br._BEDROCK_RATES_USD_PER_MTOK.get(br._normalise_model(raw))
        assert row is not None, f"{raw} normalises to something the table has no row for"
        for _, rate_key in TERMS:
            assert isinstance(row.get(rate_key), float), f"{raw} has no {rate_key} rate"
            assert row[rate_key] > 0, f"{raw} prices {rate_key} at nothing"
    assert len({br._normalise_model(m) for m in POPULATION_MODEL_IDS}) == 4
    # And every session in the population is therefore priceable IN FULL, not just each model
    # alone: nothing is left over, which is the state the shipped table is supposed to be in.
    usd, priced, unpriced = br._cost_usd({raw: REAL_SPAN for raw in POPULATION_MODEL_IDS})
    assert usd is not UNMEASURED and usd > 0
    assert unpriced == 0
    assert priced == len(POPULATION_MODEL_IDS) * SPAN_PRICED_TERM_TOKENS


def test_the_shipped_rates_price_a_real_span_and_one_rate_over_the_total_would_be_2x():
    """The figure a reader will actually see, from real token counts at the real rates.

    Opus 4.8 at $5.00 / $25.00 / $0.50 / $6.25 per million, over one real span:

        uncached input       4 x  5.00 / 1e6 = 0.00002
        output             831 x 25.00 / 1e6 = 0.020775
        cache read      60,671 x  0.50 / 1e6 = 0.0303355
        cache write     29,064 x  6.25 / 1e6 = 0.18165
                                              ----------
                                               0.2327805

    Asserted as the literal, not recomputed from the table, so a rate edited by hand breaks this
    test instead of silently agreeing with itself.
    """
    got, priced, unpriced = br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN})
    assert got == pytest.approx(0.2327805)
    # The figure covers the whole span, and says so — this is the "N of N tokens priced" case.
    assert (priced, unpriced) == (SPAN_PRICED_TERM_TOKENS, 0)

    # Cache writes are 78% of that bill off 32% of the tokens, which is why the categories are
    # priced apart and why the TTL caveat in `costBasis` is about the term that matters most.
    assert 29064 * 6.25 / 1_000_000 / got == pytest.approx(0.78, abs=0.01)

    # The mistake the four terms exist to prevent, quantified at the real prices: 90,570 tokens at
    # the input rate is nearly double the truth on this one span.
    naive = 90570 * 5.00 / 1_000_000
    assert naive > got * 1.9


# The two models the live population actually spends on, measured today: 9 runs, 8 with usage,
# 6,813,939 tokens over 139 LLM calls, and every session prices at $16.74 in total. Both have a row,
# so partial pricing must move NO number here — its value is prospective.
STAGING_MODELS = ("global.anthropic.claude-opus-4-8", "global.anthropic.claude-sonnet-5")


def test_a_fully_priced_session_costs_bit_for_bit_what_all_or_nothing_pricing_charged_it():
    """The regression guard on the change itself: today's population must not move.

    Every model in the measured population has a rate row, so the $16.74 over eight sessions is a
    figure partial pricing cannot alter — and the proof is arithmetic rather than a fixture of that
    total: the all-or-nothing formula is recomputed here from the shipped table, term by term in the
    same order and with the same per-term division by a million, and compared EXACTLY rather than
    approximately. `pytest.approx` would hide the one way this could quietly drift, which is a
    regrouped sum landing on a different float in the last bits.
    """
    session = {model: REAL_SPAN for model in STAGING_MODELS}

    all_or_nothing = 0.0
    for model in session:
        rates = br._BEDROCK_RATES_USD_PER_MTOK[br._normalise_model(model)]
        for tokens_key, rate_key in TERMS:
            all_or_nothing += REAL_SPAN[tokens_key] * rates[rate_key] / 1_000_000

    usd, priced, unpriced = br._cost_usd(session)
    assert usd == all_or_nothing, "partial pricing changed a fully-priced session's total"
    assert (priced, unpriced) == (2 * SPAN_PRICED_TERM_TOKENS, 0)


def test_the_priced_and_unpriced_counts_partition_the_session_whatever_the_table_holds(monkeypatch):
    """Every token is in exactly one of the two counts. That is what makes "N of M" readable.

    The denominator a reader compares the figure against is `priced + unpriced`, so a token that
    fell out of both would silently shrink M and make a short figure read as complete — the exact
    failure the counts exist to prevent, reintroduced one level down. Checked over the three states
    a model can be in (priced, no row, incomplete row) in one session, since a partition that holds
    for each alone can still fail when they are mixed.
    """
    incomplete = "us.anthropic.claude-sonnet-4-6"
    session = {STAGING_MODELS[0]: REAL_SPAN, UNPRICED_MODEL: REAL_SPAN, incomplete: REAL_SPAN}
    table = {br._normalise_model(STAGING_MODELS[0]):
             br._BEDROCK_RATES_USD_PER_MTOK[br._normalise_model(STAGING_MODELS[0])],
             br._normalise_model(incomplete): {"input": 3.0, "output": 15.0}}

    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK", table)
    usd, priced, unpriced = br._cost_usd(session)

    assert priced + unpriced == 3 * SPAN_PRICED_TERM_TOKENS
    assert (priced, unpriced) == (SPAN_PRICED_TERM_TOKENS, 2 * SPAN_PRICED_TERM_TOKENS)
    assert usd == pytest.approx(0.2327805), "only the model with a complete row may be priced"


def test_the_shipped_cost_basis_names_its_source_its_date_and_the_ttl_it_cannot_see():
    """The rates are hand-entered, so `costBasis` is the only thing that can make them auditable.

    It reaches the UI verbatim, and it is where a reader learns four things they cannot get from the
    number itself: which page, WHEN it was read (a hand-entered rate goes stale silently, and a
    figure with no date cannot be recognised as stale), that the region column was checked, and
    that the cache-write rate is the 5-minute-TTL one — the telemetry carries no TTL, so a run on a
    1-hour TTL is understated by up to 1.6x and nothing in the data would say so.

    A fifth thing since pricing became partial: that the figure CAN be short, and which numbers
    beside it say by how much. A basis that stayed silent on that would describe an all-or-nothing
    total the code no longer produces, and would leave a reader with no way to tell a complete
    figure from an incomplete one.
    """
    basis = br.cost_basis()
    assert basis != COST_UNMEASURED_REASON
    assert "ESTIMATE, not billed truth" in basis
    assert "https://aws.amazon.com/bedrock/pricing/" in basis
    assert "2026-08-31" in basis
    assert "every region" in basis
    assert "5-MINUTE-TTL" in basis and "1-hour" in basis and "1.6x" in basis
    # The partial-pricing rule is stated to the reader, not merely enforced in code — including
    # WHERE to look, since the words alone would not tell anyone whether THIS figure is short.
    assert "Pricing is PARTIAL" in basis
    assert "priced and unpriced TOKEN counts beside the figure" in basis
    # ...and that nothing-priced is still the sentinel rather than a free session.
    assert "UNMEASURED, never $0" in basis
    # The old all-or-nothing promise must be GONE, not merely joined by the new sentence: it is
    # rendered verbatim in the UI, so leaving it would tell a reader the opposite of the truth.
    assert "rather than a partial price" not in basis


def test_the_illustrative_rates_are_not_the_shipped_ones():
    """Guards the separation this module is built on.

    Every other test here either patches `RATES` (to test the formula) or reads the module's own
    table (to test the prices). Those two only stay distinguishable while the fixture's numbers
    differ from the real ones: if they coincided, a formula test could pass against a table it was
    supposed to have replaced, and a table test could pass against a fixture. That is the "a
    fixture that fails to load looks exactly like a passing test" failure, one layer up.
    """
    shipped = {rate for row in br._BEDROCK_RATES_USD_PER_MTOK.values() for rate in row.values()}
    illustrative = {rate for row in RATES.values() for rate in row.values()}
    assert not (shipped & illustrative)
    assert br._normalise_model(UNPRICED_MODEL) not in br._BEDROCK_RATES_USD_PER_MTOK


# --- partial per session, with the shortfall counted in tokens ------------------------------

def test_a_model_the_shipped_table_cannot_price_costs_its_own_tokens_and_not_the_session(caplog):
    """The reversal, against the REAL table rather than a patched one.

    All-or-nothing was the first answer here, and its concern was right: a smaller-but-plausible
    number captioned "cost" understates the total while looking authoritative. But refusing the
    session threw away every model beside the unpriced one — on the measured population, one new
    model id in the busiest session would have cost the reported bill 72% of itself
    ($12.11 of $16.74 over eight sessions). So the unpriced model is skipped, its tokens are
    counted, and what makes that honest rather than merely larger is the count: the caller publishes
    it so the figure reads "N of M tokens priced".
    """
    priceable = {"global.anthropic.claude-opus-4-8": REAL_SPAN}
    alone, priced_alone, _ = br._cost_usd(priceable)
    assert alone == pytest.approx(0.2327805)

    with caplog.at_level("WARNING"):
        usd, priced, unpriced = br._cost_usd({**priceable, UNPRICED_MODEL: REAL_SPAN})

    # The priced model is priced at EXACTLY what it costs on its own: the unpriced model beside it
    # neither suppresses the figure nor contributes to it.
    assert usd == pytest.approx(alone)
    assert usd is not UNMEASURED
    assert (priced, unpriced) == (priced_alone, SPAN_PRICED_TERM_TOKENS)
    # Half the session's tokens went unpriced, and that ratio is the whole warning to the reader.
    assert priced + unpriced == 2 * SPAN_PRICED_TERM_TOKENS
    assert "no Bedrock rate for model" in caplog.text
    assert UNPRICED_MODEL in caplog.text
    # The log names the size of the hole too, so an operator reading it knows whether to care.
    assert str(SPAN_PRICED_TERM_TOKENS) in caplog.text


def test_a_session_no_model_of_which_prices_is_unmeasured_and_never_zero(caplog):
    """Partial pricing must not slide into pricing nothing and calling it $0.00.

    This is the case the sentinel still exists for, and it is the one where a zero would be most
    convincing: the arithmetic ran, found no usable rate, and has a perfectly good `0.0` in hand.
    A cost of zero may never stand in for "could not price" — the UI reserves `$0.00` for a session
    that used a priced model and spent nothing, which is a different statement.
    """
    with caplog.at_level("WARNING"):
        usd, priced, unpriced = br._cost_usd({UNPRICED_MODEL: REAL_SPAN})
    assert usd is UNMEASURED
    assert not isinstance(usd, float), "a float here is a price, and no price was established"
    # ...and the tokens are still counted, so the wire can say NONE of them were priced rather than
    # falling silent about a session that really did spend.
    assert (priced, unpriced) == (0, SPAN_PRICED_TERM_TOKENS)


def test_a_session_that_recorded_no_per_model_counts_is_unmeasured_not_free():
    """No `byModel` block is nothing to price, and $0.00 would read as a free run.

    Roughly a third of harvested sessions record no LLM usage at all, so this is the ordinary case
    rather than a corner — and it is the one where a zero would be most convincing.

    Both counts are 0 because nothing was priced and nothing was SKIPPED either: such a session has
    no tokens to attribute to either half, and inventing an unpriced count for it would inflate the
    shortfall the wire reports with sessions that never spent.
    """
    assert br._cost_usd({}) == (UNMEASURED, 0, 0)
    assert br._cost_usd(None) == (UNMEASURED, 0, 0)


def test_an_unpriced_model_costs_only_its_own_tokens_under_a_patched_table(priced, caplog):
    """The same rule under a patched table, so it is a property of `_cost_usd` and not of the rows.

    Haiku is in the illustrative table and the third model is not, so this also pins that the
    skipped model is the one WITHOUT a row rather than, say, the last one seen.
    """
    haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    with caplog.at_level("WARNING"):
        usd, tokens_priced, tokens_unpriced = br._cost_usd({
            "global.anthropic.claude-opus-4-8": REAL_SPAN, haiku: REAL_SPAN,
            UNPRICED_MODEL: REAL_SPAN})

    two_priced, _, _ = br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN,
                                     haiku: REAL_SPAN})
    assert usd == pytest.approx(two_priced)
    assert (tokens_priced, tokens_unpriced) == (2 * SPAN_PRICED_TERM_TOKENS,
                                               SPAN_PRICED_TERM_TOKENS)
    assert "no Bedrock rate for model" in caplog.text


def test_a_rate_row_missing_a_term_refuses_the_whole_model_rather_than_pricing_three_of_four(
        monkeypatch, caplog):
    """A half-filled rate row must not silently price cache reads at nothing.

    This is the one refusal partial pricing does NOT relax, and the asymmetry is the point: a model
    priced on three of four categories is a different and worse failure than an unpriced one,
    because its tokens land in the PRICED count and vouch for a figure that is missing most of the
    bill. Cache reads are ~80% of prompt tokens on this population, so an omitted `cache_read` term
    would not be a rounding error.
    """
    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK", {
        "anthropic.claude-opus-4-8": {"input": 7.0, "output": 40.0},
        "anthropic.claude-haiku-4-5-20251001-v1:0": RATES[
            "anthropic.claude-haiku-4-5-20251001-v1:0"]})
    haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    with caplog.at_level("WARNING"):
        usd, tokens_priced, tokens_unpriced = br._cost_usd({
            "global.anthropic.claude-opus-4-8": REAL_SPAN, haiku: REAL_SPAN})

    # Opus's tokens are UNPRICED, not partly priced: the complete row beside it is the only one that
    # contributes, so the figure is haiku's alone.
    complete_only, _, _ = br._cost_usd({haiku: REAL_SPAN})
    assert usd == pytest.approx(complete_only)
    assert (tokens_priced, tokens_unpriced) == (SPAN_PRICED_TERM_TOKENS, SPAN_PRICED_TERM_TOKENS)
    assert "has no 'cache_read' term" in caplog.text

    # And with no complete row anywhere, the incomplete one yields the sentinel rather than a
    # three-category price.
    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK",
                        {"anthropic.claude-opus-4-8": {"input": 7.0, "output": 40.0}})
    assert br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN}) == (
        UNMEASURED, 0, SPAN_PRICED_TERM_TOKENS)


# --- the arithmetic, on illustrative rates -------------------------------------------------

def test_the_four_terms_are_priced_separately_and_each_at_its_own_rate(priced):
    """The formula, against a real span's real token counts and the illustrative rates.

    Correct: 4 uncached input, 831 output, 60,671 cache read, 29,064 cache write, each at its own
    rate. The per-term deltas below are what pin the PAIRING rather than just the sum — swap any two
    rates and the total moves, but only these say which count is priced at which rate.
    """
    got, priced, unpriced = br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN})
    expected = (4 * 7.0 + 831 * 40.0 + 60671 * 0.7 + 29064 * 9.0) / 1_000_000
    assert got == pytest.approx(expected)
    assert (priced, unpriced) == (SPAN_PRICED_TERM_TOKENS, 0)

    for tokens_key, rate_key in TERMS:
        more = {**REAL_SPAN, tokens_key: REAL_SPAN[tokens_key] + 1_000_000}
        dearer, more_priced, _ = br._cost_usd({"global.anthropic.claude-opus-4-8": more})
        assert dearer - got == pytest.approx(RATES["anthropic.claude-opus-4-8"][rate_key]), (
            f"a million more {tokens_key} must cost exactly the {rate_key} rate")
        # The priced-token count follows the same four terms the price does, so a term that grew
        # cannot be priced without also being counted.
        assert more_priced == priced + 1_000_000, tokens_key


def test_cross_region_prefixes_normalise_to_one_rate_row(priced):
    """`global.` and `us.` are routing prefixes on the SAME model, and both occur here.

    Keying rates on the raw id would price one and silently drop the other — and a dropped model
    reads as an unmeasured session, so the bug would look like thin coverage rather than a defect.
    """
    assert br._normalise_model("global.anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"
    assert br._normalise_model("us.anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"
    assert br._normalise_model("anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"
    # A bare id with no prefix is left alone rather than having its first segment eaten.
    assert br._normalise_model("some-vendor.model-1") == "some-vendor.model-1"
    assert br._normalise_model("") == ""

    both, both_priced, both_unpriced = br._cost_usd({
        "global.anthropic.claude-opus-4-8": REAL_SPAN, "us.anthropic.claude-opus-4-8": REAL_SPAN})
    one, _, _ = br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN})
    assert both == pytest.approx(one * 2)
    # Neither prefix lands in the unpriced count, which is now how a table keyed on the raw id
    # would announce itself: half the tokens skipped rather than a silently absent figure.
    assert (both_priced, both_unpriced) == (2 * SPAN_PRICED_TERM_TOKENS, 0)


def test_a_multi_model_session_sums_each_model_at_its_own_rate(priced):
    """One session can span two models — a real one in the fixtures does.

    `dispatch_tool` inherits the turn's model and the orchestrator's catalogue lets the LLM choose,
    so a session is not single-model and a session-level rate would be a fiction.
    """
    haiku = {"total_tokens": 5399, "output_tokens": 228, "cache_read_tokens": 0,
             "cache_write_tokens": 5169, "input_tokens": 2, "llm_calls": 1}
    got, priced, unpriced = br._cost_usd({
        "global.anthropic.claude-opus-4-8": REAL_SPAN,
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": haiku})
    expected = ((4 * 7.0 + 831 * 40.0 + 60671 * 0.7 + 29064 * 9.0)
                + (2 * 4.0 + 228 * 22.0 + 0 * 0.4 + 5169 * 6.0)) / 1_000_000
    assert got == pytest.approx(expected)
    assert (priced, unpriced) == (SPAN_PRICED_TERM_TOKENS + 5399, 0)
    # Each model priced at ITS OWN row, not at the first one found: haiku's tokens are 6% of opus's
    # here and its share of the bill is smaller still.
    opus_only, _, _ = br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN})
    assert got != pytest.approx(opus_only)


def test_a_populated_table_labels_cost_an_estimate_and_carries_its_provenance(priced):
    """A computed figure must never present as billed truth, whatever the rates are.

    The table is hand-entered under every configuration, so `costBasis` always names its source and
    the date it was read. That is what makes a stale rate visible in the UI instead of silently
    wrong, and it is a property of `cost_basis()` rather than of the particular string shipped.
    """
    basis = br.cost_basis()
    assert basis != COST_UNMEASURED_REASON
    assert "ESTIMATE, not billed truth" in basis
    assert "example.invalid/prices read 2001-01-01" in basis
    assert "Pricing is PARTIAL" in basis


def test_provenance_without_rates_or_rates_without_provenance_stays_unmeasured(monkeypatch):
    """Half-configured is not configured. Either omission leaves the honest reason in place."""
    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK", RATES)
    monkeypatch.setattr(br, "_RATE_PROVENANCE", "")
    assert br.cost_basis() == COST_UNMEASURED_REASON
    assert "rate table" in br.cost_basis()

    monkeypatch.setattr(br, "_BEDROCK_RATES_USD_PER_MTOK", {})
    monkeypatch.setattr(br, "_RATE_PROVENANCE", "somewhere, sometime")
    assert br.cost_basis() == COST_UNMEASURED_REASON
    # An empty table prices nothing at all, rather than pricing everything at zero — and it reports
    # every token as unpriced, so "0 of 90,570 tokens priced" is what the wire says instead of
    # falling back to a bare sentinel that could equally mean "this session did not spend".
    assert br._cost_usd({"global.anthropic.claude-opus-4-8": REAL_SPAN}) == (
        UNMEASURED, 0, SPAN_PRICED_TERM_TOKENS)

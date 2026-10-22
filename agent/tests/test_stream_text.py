"""The model's text, sorted into answer and step notes (Lab 1 dry run: `…orientation.### Lab 1`, and a wall of
"Let me check…" narration above every step)."""
from stream_text import LEAD_IN_MAX, NOTE_MAX, ProseStream, SegmentJoiner

TOOL = object()


def _run(events, note_max=NOTE_MAX):
    p, out = ProseStream(note_max), []
    for e in events:
        out += p.tool_started() if e is TOOL else p.text(e)
    out += p.end()
    answer = "".join(v for k, v in out if k == "text")
    notes = [v for k, v in out if k == "note"]
    return answer, notes


def test_short_narration_before_a_tool_becomes_a_note():
    answer, notes = _run(["Let me check ", "the project.", TOOL, "I'll open the diagram.", TOOL, "### Step 1\n\nBody"])
    assert answer == "### Step 1\n\nBody"
    assert notes == ["Let me check the project.", "I'll open the diagram."]


def test_a_plain_answer_with_no_tools_is_untouched():
    assert _run(["Hel", "lo ", "world"]) == ("Hello world", [])


def test_long_text_before_a_tool_is_answer_and_gets_a_paragraph_break_after_it():
    long = "x" * (NOTE_MAX + 1)
    answer, notes = _run([long, TOOL, "### Step 2"])
    assert answer == long + "\n\n### Step 2" and notes == []


def test_long_text_streams_once_it_cannot_be_a_lead_in():
    p = ProseStream(note_max=5)
    assert p.text("abc") == []
    assert p.text("def") == []                             # could still be a lead-in to a heading
    assert p.text("\n## H") == [("text", "abcdef\n## H")]  # has a heading: it is the answer
    assert p.text("g") == [("text", "g")]


def test_whitespace_only_before_a_tool_is_dropped():
    assert _run(["\n\n", TOOL, "Answer"]) == ("Answer", [])


def test_joiner_completes_existing_breaks_without_doubling():
    def join(events):
        j, out = SegmentJoiner(), []
        for e in events:
            if e is TOOL:
                j.tool_started()
            else:
                out.append(j.text(e))
        return "".join(out)
    assert join(["a", TOOL, "b"]) == "a\n\nb"
    assert join(["a\n", TOOL, "b"]) == "a\n\nb"
    assert join(["a", TOOL, "\nb"]) == "a\n\nb"
    assert join(["a\n\n", TOOL, "b"]) == "a\n\nb"
    assert join([TOOL, "b"]) == "b"


def test_a_short_lead_in_before_the_first_heading_is_a_note():
    answer, notes = _run(["Everything's in place. Here's the welcome.\n\n### Welcome\n\nBody"])
    assert answer == "### Welcome\n\nBody" and notes == ["Everything's in place. Here's the welcome."]


def test_lead_in_detection_also_applies_once_a_long_answer_starts_streaming():
    body = "x" * (NOTE_MAX + 50)
    answer, notes = _run(["The job is done.\n\n## Results\n\n", body])
    assert answer.startswith("## Results") and notes == ["The job is done."]


def test_answers_without_a_heading_after_their_first_paragraph_are_untouched():
    assert _run(["Hello.\n\nMore text."]) == ("Hello.\n\nMore text.", [])
    assert _run(["### Title\n\nBody"]) == ("### Title\n\nBody", [])
    long_intro = "y" * (LEAD_IN_MAX + 5)
    assert _run([long_intro + "\n\n### H"])[1] == []


def test_a_longer_thinking_aloud_lead_in_is_still_a_note_when_a_heading_follows():
    lead = "The job returned at once, so I give one reply. I have the totals: " + "z" * 300
    answer, notes = _run([lead[:200], lead[200:], "\n\n### Step 3\n\nBody"])
    assert answer == "### Step 3\n\nBody" and notes == [" ".join(lead.split())]


def test_a_long_answer_without_a_heading_still_streams_once_it_passes_the_lead_in_limit():
    p = ProseStream()
    assert p.text("a" * (NOTE_MAX + 10)) == []           # could still be a lead-in
    out = p.text("b" * LEAD_IN_MAX)                        # past the limit: it is the answer
    assert out and out[0][0] == "text"

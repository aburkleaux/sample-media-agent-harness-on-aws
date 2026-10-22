"""Keep the model's answer readable across tool calls.

A turn's answer reaches the chat as ONE markdown string, but the model writes it in pieces with tool calls in
between: "Let me check the project." → tool → "I'll open the diagram." → tool → "### Step 1 …". Two problems:

* Streamed back to back the pieces FUSED — "…the project.### Step 1" — so a heading, list or table that
  followed a tool call rendered as literal text. `SegmentJoiner` puts a paragraph break at each seam.
* The short pieces are progress narration, not the answer. Prompt rules ("do not narrate") do not stop it, and
  above a long answer they read as a wall of clutter (Lab 1 dry run). `ProseStream` holds a piece back until it
  knows what it is: a SHORT piece followed by a tool call becomes a step note (shown with the turn's steps);
  anything longer, or the turn's last piece, is the answer and streams as before.

Holding back costs little: notes are short, and a piece starts streaming once it is past `NOTE_MAX` and can
no longer be a lead-in (it has a heading, or is past `LEAD_IN_MAX`).
"""
from __future__ import annotations

import re
from typing import Literal

PARAGRAPH = "\n\n"
#: A piece longer than this is answer text, whatever follows it. Narration notes are one or two sentences.
NOTE_MAX = 280
#: A lead-in paragraph directly before the answer's first heading is narration up to this length — a model
#: thinking aloud before its heading ran to ~400 characters ("…per rule 5 I give one combined reply…").
LEAD_IN_MAX = 600

Out = list[tuple[Literal["text", "note"], str]]

#: A first paragraph that is not itself a heading, then a blank line, then a markdown heading.
_LEAD_IN = re.compile(r"\A\s*((?!#)[^\n]+(?:\n(?!\s*\n)[^\n]+)*)\n\s*\n(?=#{1,6} )")


class SegmentJoiner:
    """Feed it every answer chunk (``text``) and every tool start (``tool_started``), in stream order."""

    def __init__(self) -> None:
        self._tail = ""          # the last characters emitted, enough to see a trailing blank line
        self._seam = False       # a tool started since the last text

    def tool_started(self) -> None:
        if self._tail:
            self._seam = True

    def text(self, chunk: str) -> str:
        if not chunk:
            return chunk
        if self._seam:
            self._seam = False
            if not self._tail.endswith(PARAGRAPH):
                lead = len(chunk) - len(chunk.lstrip("\n"))
                missing = max(0, 2 - lead - (1 if self._tail.endswith("\n") else 0))
                chunk = "\n" * missing + chunk
        self._tail = (self._tail + chunk)[-2:]
        return chunk


class ProseStream:
    """Sort the model's text into answer text and step notes. Call in stream order; emit what each returns."""

    def __init__(self, note_max: int = NOTE_MAX) -> None:
        self._join = SegmentJoiner()
        self._buf = ""
        self._streaming = False     # the current piece is already flowing as answer text
        self._note_max = note_max

    def text(self, chunk: str) -> Out:
        if not chunk:
            return []
        if self._streaming:
            return [("text", self._join.text(chunk))]
        self._buf += chunk
        if len(self._buf) > self._note_max and not self._may_be_lead_in():
            out, self._buf, self._streaming = self._buf, "", True
            return self._answer(out)
        return []

    def _may_be_lead_in(self) -> bool:
        """Keep holding while the buffer could still be a lead-in whose heading has not arrived yet."""
        b = self._buf
        return len(b) <= LEAD_IN_MAX and not b.lstrip().startswith("#") and "\n#" not in b

    def _answer(self, text: str) -> Out:
        """Emit `text` as answer text — minus a short lead-in before its first heading, which is a note.

        "Everything's in place. Here's Step 1." + `### Step 1 …`: the lead-in is the same narration as a
        pre-tool line, just written after the last tool call. Only a SHORT, heading-less first paragraph
        directly followed by a markdown heading counts; any other answer is untouched.
        """
        m = _LEAD_IN.match(text)
        if m and len(m.group(1).strip()) <= LEAD_IN_MAX:
            return [("note", " ".join(m.group(1).split())), ("text", self._join.text(text[m.end(1):].lstrip("\n")))]
        return [("text", self._join.text(text))]

    def tool_started(self) -> Out:
        out: Out = []
        if len(self._buf) > self._note_max:
            out += [("text", self._join.text(self._buf))]  # held as a possible lead-in, but it is answer text
        elif self._buf.strip():
            out.append(("note", " ".join(self._buf.split())))
        self._buf = ""
        self._streaming = False
        self._join.tool_started()
        return out

    def finish_answer(self) -> Out:
        """The model ended a message without calling a tool: what is held back IS the answer.

        Emits it exactly as :meth:`end` would (same lead-in handling), but at the moment the model
        stops rather than after the turn's post-processing, and switches to streaming so anything
        the model writes after it -- a continuation the event loop asked for -- is not held either.
        Cheap no-op when nothing is held.
        """
        out, self._buf = self._buf, ""
        self._streaming = True
        return self._answer(out) if out else []

    def end(self) -> Out:
        out, self._buf = self._buf, ""
        return self._answer(out) if out else []

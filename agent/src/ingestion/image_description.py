"""Image description through M2C ``bedrock-inference`` (Req 8.7–8.10; design Component 12). Pure.

One inline record per image. The prompt texts are fixed per deploy and identical for every image (Req 8.8); only
``record_id`` (the asset id) and the image bytes vary, plus ``idempotency_token`` on a rerun. The answer is read
from the run's ``response`` output (``response.jsonl``), matched by ``record_id``.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional, Sequence

SYSTEM = (
    "You describe a single image so it can be found later by text search. Report only what is visible. "
    "Answer with one JSON object and nothing else."
)
INSTRUCTION = (
    "Describe this image as one JSON object with exactly these members:\n"
    '- "description": 1-3 sentences on what the image shows (required, non-empty)\n'
    '- "objects": list of the notable objects, people, or animals, as short noun phrases\n'
    '- "setting": the place or environment, as a short phrase ("" if unclear)\n'
    '- "on_screen_text": every legible piece of text, joined with ", " ("" if none)\n'
    '- "brands": list of visible brand names or logos\n'
    '- "sentiment": the overall mood in one or two words\n'
    '- "entities": list of named people, places, organizations, or titles that are identifiable from the image '
    "or its text\n"
    "Return only the JSON object."
)


def request_arguments(asset_id: str, jpeg: bytes, model_chain: Sequence[str],
                      token: Optional[str] = None, thinking: Optional[str] = None) -> dict:
    args: dict = {
        "records": [{
            "record_id": asset_id,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": [
                {"text": INSTRUCTION},
                {"image": {"format": "jpeg", "source": {"bytes": base64.b64encode(jpeg).decode("ascii")}}},
            ]}],
        }],
        "use_batch": False,
        "temperature": 0,
    }
    if model_chain:
        args["model_chain"] = list(model_chain)
    if thinking:
        args["thinking"] = thinking
    if token:
        args["idempotency_token"] = token
    return args


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_response_line(lines: Sequence[dict], asset_id: str) -> tuple[Optional[dict], str]:
    """``(description object, "")`` or ``(None, why)`` (Req 8.9, 8.10)."""
    line = next((ln for ln in lines if isinstance(ln, dict) and ln.get("record_id") == asset_id), None)
    if line is None:
        return None, "the run returned no answer for this image"
    if line.get("status") != "SUCCESS":
        return None, f"the record did not succeed ({line.get('reason') or line.get('status') or 'no status'})"
    text = _FENCE.sub("", str(line.get("output_text") or "").strip())
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            obj = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        except ValueError:
            obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("description"), str) or not obj["description"].strip():
        return None, "the answer is not a JSON object with a non-empty description"
    return obj, ""


def image_capable(model: dict) -> bool:
    """``get_model`` → does it take image input?"""
    m = model.get("model") if isinstance(model.get("model"), dict) else model
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    return isinstance(caps.get("image"), dict) or "vision" in (m.get("model_type") or [])

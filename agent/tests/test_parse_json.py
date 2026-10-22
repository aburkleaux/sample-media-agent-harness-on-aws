"""Quick test for _parse_json_from_text with realistic agent output."""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Inline the function to test it directly
def _parse_json_from_text(text):
    if not text or not text.strip():
        return None
    first_brace = text.find("{")
    if first_brace == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(first_brace, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = text[first_brace:i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        if parsed.get("type") in ("step", "execution_summary"):
                            return _parse_json_from_text(text[i + 1:])
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                return _parse_json_from_text(text[i + 1:])
    return None

# Test 1: Simple JSON with markdown fence
t1 = '```json\n{"execution_mode": "complex", "sub_goals": []}\n```'
r1 = _parse_json_from_text(t1)
assert r1 is not None and r1["execution_mode"] == "complex", f"Test 1 FAILED: {r1}"
print("Test 1 PASSED: simple fence")

# Test 2: JSON + execution_summary after
t2 = '```json\n{"plan_id": "p1", "goal": "test", "nodes": []}\n```\n{"type": "execution_summary", "total_duration_ms": 100}'
r2 = _parse_json_from_text(t2)
assert r2 is not None and r2.get("plan_id") == "p1", f"Test 2 FAILED: {r2}"
print("Test 2 PASSED: skip execution_summary")

# Test 3: step event before the real JSON
t3 = '{"type": "step", "status": "running", "label": "cycle #1"}\n```json\n{"plan_id": "p2", "nodes": []}\n```'
r3 = _parse_json_from_text(t3)
assert r3 is not None and r3.get("plan_id") == "p2", f"Test 3 FAILED: {r3}"
print("Test 3 PASSED: skip step event")

# Test 4: JSON with escaped quotes and braces inside strings
t4 = '```json\n{"plan_id": "p3", "goal": "Compare \\"DynamoDB\\" vs {Valkey}", "nodes": [{"id": "n1", "prompt": "Research\\nLine 2"}]}\n```'
r4 = _parse_json_from_text(t4)
assert r4 is not None and r4.get("plan_id") == "p3", f"Test 4 FAILED: {r4}"
print("Test 4 PASSED: escaped quotes and braces in strings")

# Test 5: Realistic large Planning Agent output
t5 = '```json\n' + json.dumps({
    "execution_mode": "direct",
    "sub_goals": [{"id": "sg1", "description": "Research and compare DynamoDB vs Valkey (ElastiCache) for near-cache use case, covering latency, cost, scalability, operational complexity, consistency, eviction policies, and AWS-specific features with sub-4KB entry sizing.", "reasoning": "This is a research and comparison task that can be handled by a single dispatch with the aws-research skill.", "suggested_tools": ["http_request"], "depends_on": []}],
    "reasoning": "This goal requires deep research into two distinct AWS services.",
    "suggested_tools": {"sg1": ["http_request"]},
    "suggested_skills": {"sg1": ["aws-research"]},
    "direct_dispatch_params": {"prompt": "Compare DynamoDB vs Valkey for near-cache", "tools": ["http_request"], "skills": [{"name": "aws-research"}]}
}, indent=2) + '\n```\n{"type": "execution_summary", "total_duration_ms": 47000, "cycles": 1, "model": "us.anthropic.claude-sonnet-4-6"}'
r5 = _parse_json_from_text(t5)
assert r5 is not None and r5.get("execution_mode") == "direct", f"Test 5 FAILED: {r5}"
print("Test 5 PASSED: realistic Planning Agent output")

# Test 6: No JSON at all
t6 = "Just some text with no JSON"
r6 = _parse_json_from_text(t6)
assert r6 is None, f"Test 6 FAILED: {r6}"
print("Test 6 PASSED: no JSON returns None")

# Test 7: Empty
r7 = _parse_json_from_text("")
assert r7 is None, f"Test 7 FAILED"
print("Test 7 PASSED: empty returns None")

print("\nAll tests passed!")

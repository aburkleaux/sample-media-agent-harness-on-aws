"""The handler is built during Lambda init (session 66f2b85b: a cold first issuance ran past the proxy's 2 s
budget, so that chat turn had no search). Never fatal, and only the handler this function serves."""
import index


def _calls(monkeypatch, env):
    seen = []
    monkeypatch.setattr(index, "_production_issue_handler", lambda: seen.append("issue"))
    monkeypatch.setattr(index, "_production_refresh_handler", lambda: seen.append("refresh"))
    for k in ("AWS_LAMBDA_FUNCTION_NAME", "_HANDLER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    index._prewarm()
    return seen


def test_builds_only_the_served_handler_in_lambda(monkeypatch):
    assert _calls(monkeypatch, {"AWS_LAMBDA_FUNCTION_NAME": "f", "_HANDLER": "index.issue_handler"}) == ["issue"]
    assert _calls(monkeypatch, {"AWS_LAMBDA_FUNCTION_NAME": "f", "_HANDLER": "index.refresh_handler"}) == ["refresh"]


def test_does_nothing_outside_lambda(monkeypatch):
    assert _calls(monkeypatch, {"_HANDLER": "index.issue_handler"}) == []


def test_a_failure_is_not_fatal(monkeypatch):
    def boom():
        raise RuntimeError("missing env")
    monkeypatch.setattr(index, "_production_issue_handler", boom)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "f")
    monkeypatch.setenv("_HANDLER", "index.issue_handler")
    index._prewarm()  # no raise

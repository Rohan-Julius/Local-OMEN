"""Phase 5 (production plan): graceful errors. Each failure gets one line and
exit code 3, on every path that can hit it (urllib embedding call, the
`ollama` client, LiteLLM through ADK):

  Ollama down      "Ollama is not reachable at <host>; start it with `ollama serve`"
  model missing    "model <name> not found; run `ollama pull <name>`"
  invalid reply    one retry on both runners, then one line

No model, no network: everything is a fake.
"""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import ollama
import pydantic
import pytest

from omen import cli, config, errors, runners, vectors
from omen.contracts import TriageVerdict

HOST = "http://localhost:11434"
DOWN_MESSAGE = f"Ollama is not reachable at {HOST}; start it with `ollama serve`"


def one_line(exc: BaseException) -> str:
    text = str(exc)
    assert "\n" not in text
    return text


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class APIConnectionError(Exception):  # stands in for litellm's class of the same name
    pass


class NotFoundError(Exception):  # stands in for litellm's
    pass


class ConnectError(Exception):  # stands in for httpx's
    pass


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("[WinError 10061] refused"),
        ConnectionError("Failed to connect to Ollama"),  # what the ollama client raises
        urllib.error.URLError(ConnectionRefusedError()),
        APIConnectionError("OllamaException - Connection refused"),
        ConnectError("All connection attempts failed"),
    ],
    ids=["refused", "ollama-client", "urllib", "litellm", "httpx"],
)
def test_ollama_down_is_one_friendly_line(exc):
    err = errors.classify(exc)
    assert isinstance(err, errors.OllamaUnavailable)
    assert one_line(err) == DOWN_MESSAGE


@pytest.mark.parametrize(
    "exc,name",
    [
        (ollama.ResponseError("model 'gemma4:12b-it-q4_K_M' not found", 404), "gemma4:12b-it-q4_K_M"),
        (ollama.ResponseError("model \"embeddinggemma\" not found, try pulling it first", 404), "embeddinggemma"),
        (NotFoundError('OllamaException - {"error":"model \'qwen3:4b\' not found"}'), "qwen3:4b"),
        (urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}')), config.LLM_MODEL),
    ],
    ids=["ollama-client", "ollama-embed", "litellm", "urllib-404"],
)
def test_model_missing_names_the_model_and_the_pull_command(exc, name):
    err = errors.classify(exc)
    assert isinstance(err, errors.ModelMissing)
    assert one_line(err) == f"model {name} not found; run `ollama pull {name}`"


def test_classify_finds_the_cause_behind_a_wrapper():
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("agent failed") from inner
    except RuntimeError as outer:
        assert isinstance(errors.classify(outer), errors.OllamaUnavailable)


@pytest.mark.parametrize("exc", [ValueError("nothing to do with Ollama"), KeyError("x"), pydantic.ValidationError.from_exception_data("T", [])])
def test_unrelated_errors_are_not_classified(exc):
    assert errors.classify(exc) is None


def test_a_real_http_500_is_not_mistaken_for_a_missing_model_or_a_down_server():
    assert errors.classify(urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b"boom"))) is None


# ---------------------------------------------------------------------------
# every path
# ---------------------------------------------------------------------------


def test_embedding_call_ollama_down(monkeypatch):
    monkeypatch.setattr(vectors.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError(ConnectionRefusedError())))
    with pytest.raises(errors.OllamaUnavailable) as exc:
        vectors.embed_document(["x"])
    assert one_line(exc.value) == DOWN_MESSAGE


def test_embedding_call_model_missing(monkeypatch):
    def not_found(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"error":"model \\"embeddinggemma\\" not found"}'))

    monkeypatch.setattr(vectors.urllib.request, "urlopen", not_found)
    with pytest.raises(errors.ModelMissing) as exc:
        vectors.embed_document(["x"])
    assert one_line(exc.value) == f"model {config.EMBED_MODEL} not found; run `ollama pull {config.EMBED_MODEL}`"


@pytest.mark.asyncio
async def test_direct_runner_ollama_down(monkeypatch):
    class Client:
        def __init__(self, host=None, **kwargs):
            pass

        def chat(self, **kwargs):
            raise ConnectionError("Failed to connect to Ollama")

    monkeypatch.setattr(runners.ollama, "Client", Client)
    with pytest.raises(errors.OllamaUnavailable) as exc:
        await runners.DirectOllamaRunner().run_structured("i", "p", TriageVerdict)
    assert one_line(exc.value) == DOWN_MESSAGE


@pytest.mark.asyncio
async def test_direct_runner_model_missing_in_a_tool_loop(monkeypatch):
    class Client:
        def __init__(self, host=None, **kwargs):
            pass

        def chat(self, **kwargs):
            raise ollama.ResponseError(f"model '{config.LLM_MODEL}' not found", 404)

    monkeypatch.setattr(runners.ollama, "Client", Client)
    from omen.tools import ToolBudget

    with pytest.raises(errors.ModelMissing):
        await runners.DirectOllamaRunner().run_tooled("i", "p", {}, ToolBudget(), think=False)


@pytest.mark.asyncio
async def test_adk_runner_ollama_down_through_litellm(monkeypatch):
    async def fail(*a, **k):
        raise APIConnectionError("OllamaException - Connection refused")

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_structured_adk=fail, run_tooled_adk=fail))
    from omen.tools import ToolBudget

    with pytest.raises(errors.OllamaUnavailable):
        await runners.ADKRoleRunner().run_structured("i", "p", TriageVerdict)
    with pytest.raises(errors.OllamaUnavailable):
        await runners.ADKRoleRunner().run_tooled("i", "p", {}, ToolBudget(), think=False)


@pytest.mark.asyncio
async def test_adk_runner_model_missing_through_litellm(monkeypatch):
    async def fail(*a, **k):
        raise NotFoundError('OllamaException - {"error":"model \'gemma4:12b-it-q4_K_M\' not found"}')

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_structured_adk=fail))
    with pytest.raises(errors.ModelMissing) as exc:
        await runners.ADKRoleRunner().run_structured("i", "p", TriageVerdict)
    assert "ollama pull gemma4:12b-it-q4_K_M" in str(exc.value)


# ---------------------------------------------------------------------------
# schema-invalid replies: one retry on both runners, then one line
# ---------------------------------------------------------------------------

VALID = TriageVerdict(code_mechanism="m", verdict="NO_MATCH", confidence="low", reasoning="r")


class ReplyClient:
    """A fake Ollama client that answers with the given message contents in order."""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = 0

    def __call__(self, host=None, **kwargs):
        return self

    def chat(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(message=SimpleNamespace(content=self.contents.pop(0)))


@pytest.mark.asyncio
async def test_direct_runner_retries_an_invalid_reply_once_then_succeeds(monkeypatch):
    client = ReplyClient("this is not json", VALID.model_dump_json())
    monkeypatch.setattr(runners.ollama, "Client", client)
    assert await runners.DirectOllamaRunner().run_structured("i", "p", TriageVerdict) == VALID
    assert client.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["this is not json", '{"verdict": "MAYBE"}', "{}"], ids=["not-json", "bad-enum", "missing-fields"])
async def test_direct_runner_gives_up_after_the_retry_with_one_line(monkeypatch, bad):
    client = ReplyClient(bad, bad, VALID.model_dump_json())
    monkeypatch.setattr(runners.ollama, "Client", client)
    with pytest.raises(errors.SchemaInvalid) as exc:
        await runners.DirectOllamaRunner().run_structured("i", "p", TriageVerdict)
    assert client.calls == 2  # exactly one retry, not an open loop
    assert one_line(exc.value).startswith("the model's reply did not match the TriageVerdict schema after 2 attempts")


def _invalid_reply_error():
    try:
        TriageVerdict.model_validate({"verdict": "MAYBE"})
    except pydantic.ValidationError as e:
        return e


@pytest.mark.asyncio
async def test_adk_runner_retries_an_invalid_reply_once_then_succeeds(monkeypatch):
    attempts = []

    async def flaky(instruction, prompt, schema, think):
        attempts.append(1)
        if len(attempts) == 1:
            raise _invalid_reply_error()
        return VALID

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_structured_adk=flaky))
    assert await runners.ADKRoleRunner().run_structured("i", "p", TriageVerdict) == VALID
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_adk_runner_gives_up_after_the_retry_with_one_line(monkeypatch):
    attempts = []

    async def always_invalid(instruction, prompt, schema, think):
        attempts.append(1)
        raise _invalid_reply_error()

    monkeypatch.setattr(runners, "_agents", lambda: SimpleNamespace(run_structured_adk=always_invalid))
    with pytest.raises(errors.SchemaInvalid) as exc:
        await runners.ADKRoleRunner().run_structured("i", "p", TriageVerdict)
    assert len(attempts) == 2
    assert "after 2 attempts" in one_line(exc.value)


# ---------------------------------------------------------------------------
# the CLI: one line on stderr, exit 3, no traceback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected",
    [
        (ConnectionRefusedError("refused"), f"error: {DOWN_MESSAGE}"),
        (ollama.ResponseError("model 'x:1' not found", 404), "error: model x:1 not found; run `ollama pull x:1`"),
        (errors.SchemaInvalid("the model's reply did not match the T schema after 2 attempts (bad)"),
         "error: the model's reply did not match the T schema after 2 attempts (bad)"),
        (errors.LLMTimeout(600, "model call"), None),
        (RuntimeError("something else"), "error: RuntimeError: something else"),
    ],
    ids=["down", "missing", "schema", "timeout", "unknown"],
)
async def test_cli_prints_one_line_and_exits_three(monkeypatch, capsys, tmp_path, raised, expected):
    def boom(args):
        raise raised

    monkeypatch.setattr(cli, "cmd_reindex", boom)
    assert await cli.main(["reindex", "--store", str(tmp_path)]) == cli.EXIT_RUNTIME_ERROR
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err
    if expected:
        assert err.strip() == expected
    else:
        assert err.startswith("error: model call timed out after 600s")


def test_json_is_valid_for_the_schema_used_in_these_tests():
    assert json.loads(VALID.model_dump_json())["verdict"] == "NO_MATCH"

"""Phase 5 (production plan): hardware profiles and the warm-up call.
Decision function, profile values, precedence (flag > OMEN_PROFILE > auto),
and the warm-up's retry. No model, no Ollama.
"""
from __future__ import annotations

import pytest

from omen import cli, config, errors, profiles, warmup


def ps(*models):
    return {"models": [dict(m) for m in models]}


MODEL = config.LLM_MODEL


# ---------------------------------------------------------------------------
# the decision function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer,expected",
    [
        (ps({"name": MODEL, "size": 8_000_000_000, "size_vram": 0}), "cpu"),
        (ps({"name": MODEL, "size": 8_000_000_000, "size_vram": 8_000_000_000}), "gpu"),
        (ps({"name": MODEL, "size": 8_000_000_000, "size_vram": 2_000_000_000}), "gpu"),  # partial offload
        (ps({"name": "other:1b", "size_vram": 0}, {"name": MODEL, "size_vram": 5}), "gpu"),  # matches by model name
        (ps({"model": MODEL, "size_vram": 0}), "cpu"),  # `model` key instead of `name`
        (ps({"name": "other:1b", "size_vram": 0}), None),  # our model is not loaded
        (ps(), None),
        ({}, None),
        (None, None),  # Ollama did not answer
        (ps({"name": MODEL}), None),  # no size_vram field
    ],
)
def test_profile_from_ps(answer, expected):
    assert profiles.profile_from_ps(answer, MODEL) == expected


# ---------------------------------------------------------------------------
# profile values
# ---------------------------------------------------------------------------


def test_gpu_profile_keeps_the_values_omen_had_before():
    gpu = profiles.GPU
    assert gpu.deadline_seconds == config.TOOL_WALL_CLOCK_SECONDS == 90
    assert gpu.investigator_think is config.INVESTIGATOR_THINK_DEFAULT is True
    assert gpu.archivist_think is config.ARCHIVIST_THINK_DEFAULT is True


def test_cpu_profile_turns_think_off_and_is_much_more_patient():
    cpu, gpu = profiles.CPU, profiles.GPU
    assert (cpu.investigator_think, cpu.archivist_think) == (False, False)
    assert cpu.llm_call_timeout > 4 * gpu.llm_call_timeout
    assert cpu.deadline_seconds > 10 * gpu.deadline_seconds
    assert cpu.tool_seconds >= gpu.tool_seconds


def test_cpu_limits_cover_the_slowest_call_measured_on_cpu():
    """Measured on the CPU laptop: single structured calls of 110-170 s, a
    whole 7-turn investigation of ~820 s (Phase 3 and 4 live runs)."""
    assert profiles.CPU.llm_call_timeout >= 2 * 170
    assert profiles.CPU.deadline_seconds >= 2 * 820


def test_describe_names_every_value():
    text = profiles.CPU.describe()
    assert "profile cpu" in text and "think=False" in text and "600s" in text and "1800s" in text and "120s" in text


# ---------------------------------------------------------------------------
# precedence: flag > OMEN_PROFILE > auto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag,env,expected",
    [
        (None, None, "auto"),
        (None, "cpu", "cpu"),
        ("gpu", "cpu", "gpu"),  # the flag wins
        ("auto", "cpu", "auto"),
        (None, " GPU ", "gpu"),
    ],
)
def test_requested_name_precedence(flag, env, expected):
    environ = {"OMEN_PROFILE": env} if env is not None else {}
    assert profiles.requested_name(flag, environ) == expected


def test_an_unknown_profile_name_is_rejected():
    with pytest.raises(ValueError, match="unknown profile 'turbo'"):
        profiles.requested_name("turbo", {})
    with pytest.raises(ValueError):
        profiles.requested_name(None, {"OMEN_PROFILE": "turbo"})


def test_resolve_explicit_profiles_do_not_probe_ollama():
    def boom():
        raise AssertionError("must not probe")

    assert profiles.resolve("cpu", ps=boom, environ={}) is profiles.CPU
    assert profiles.resolve(None, ps=boom, environ={"OMEN_PROFILE": "gpu"}) is profiles.GPU


def test_resolve_auto_uses_the_ps_answer():
    cpu = lambda: ps({"name": MODEL, "size_vram": 0})  # noqa: E731
    gpu = lambda: ps({"name": MODEL, "size_vram": 9})  # noqa: E731
    assert profiles.resolve("auto", ps=cpu, environ={}) is profiles.CPU
    assert profiles.resolve(None, ps=gpu, environ={}) is profiles.GPU


def test_resolve_auto_assumes_gpu_and_warns_when_it_cannot_tell(caplog):
    with caplog.at_level("WARNING", logger="omen.profiles"):
        assert profiles.resolve("auto", ps=lambda: None, environ={}) is profiles.GPU
    assert "assuming gpu" in caplog.text and "--profile cpu" in caplog.text


def test_active_profile_follows_set_active_then_the_environment(monkeypatch):
    monkeypatch.delenv("OMEN_PROFILE", raising=False)
    assert profiles.active() is profiles.GPU  # default
    monkeypatch.setenv("OMEN_PROFILE", "cpu")
    assert profiles.active() is profiles.CPU  # tests and library users can opt in with the environment alone
    monkeypatch.setenv("OMEN_PROFILE", "auto")
    assert profiles.active() is profiles.GPU  # auto is only meaningful to the CLI, which resolves it
    profiles.set_active(profiles.CPU)
    monkeypatch.setenv("OMEN_PROFILE", "gpu")
    assert profiles.active() is profiles.CPU  # what the CLI resolved wins


def test_archivist_think_defaults_to_the_active_profile(monkeypatch):
    """archivist.learn_from_* leave `think` to the profile unless the caller says."""
    import asyncio

    from omen import archivist, store

    class Runner:
        def __init__(self):
            self.seen = []

        async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
            self.seen.append((think, budget.wall_clock_seconds))
            from omen.contracts import Transcript

            return Transcript()

    conn = store.connect()
    for profile, expected_think in ((profiles.CPU, False), (profiles.GPU, True)):
        profiles.set_active(profile)
        runner = Runner()
        asyncio.run(archivist.learn_from_postmortem(runner, conn, config.STORE_DIR, "x.md"))
        assert runner.seen == [(expected_think, profile.deadline_seconds)]
    asyncio.run(archivist.learn_from_postmortem(runner, conn, config.STORE_DIR, "x.md", think=False))  # explicit wins
    assert runner.seen[-1][0] is False


# ---------------------------------------------------------------------------
# --profile / OMEN_PROFILE at the CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["scan", "learn"])
async def test_a_bad_omen_profile_is_a_user_error_for_llm_commands(command, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("OMEN_PROFILE", "turbo")
    argv = ["scan", str(tmp_path)] if command == "scan" else ["learn", "x.md"]
    assert await cli.main(argv) == cli.EXIT_USER_ERROR
    assert "unknown profile 'turbo'" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_bad_omen_profile_does_not_break_commands_without_an_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("OMEN_PROFILE", "turbo")
    assert await cli.main(["memory", "list", "--store", str(tmp_path)]) == cli.EXIT_OK


@pytest.mark.asyncio
async def test_profile_flag_is_accepted_before_or_after_the_subcommand(tmp_path):
    for argv in (["--profile", "cpu", "memory", "list"], ["memory", "list", "--profile", "cpu"]):
        assert await cli.main([*argv, "--store", str(tmp_path)]) == cli.EXIT_OK
    with pytest.raises(SystemExit):
        await cli.main(["--profile", "turbo", "memory", "list"])


# ---------------------------------------------------------------------------
# warm-up
# ---------------------------------------------------------------------------


class FakeClient:
    """chat() raises the next item of `outcomes` if it is an exception, else returns it."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def crash():
    import ollama

    return ollama.ResponseError("llama-server process has terminated: CUDA error: shared object initialization failed", 500)


def test_warmup_retries_once_after_the_known_cuda_first_call_crash():
    client = FakeClient(crash(), {"message": "ok"})
    sleeps = []

    seconds = warmup.warm_up(client=client, sleep=sleeps.append)

    assert len(client.calls) == 2 and sleeps == [1.0]
    assert seconds >= 0


def test_warmup_gives_up_after_one_retry_and_raises_the_original_error():
    import ollama

    client = FakeClient(crash(), crash(), {"message": "never reached"})
    with pytest.raises(ollama.ResponseError):
        warmup.warm_up(client=client, sleep=lambda s: None)
    assert len(client.calls) == 2


def test_warmup_uses_the_same_options_as_real_calls_so_ollama_does_not_reload():
    client = FakeClient({"message": "ok"})
    warmup.warm_up(client=client)
    call = client.calls[0]
    assert call["model"] == config.LLM_MODEL and call["think"] is False
    assert call["options"]["num_gpu"] == config.NUM_GPU and call["options"]["num_ctx"] == config.NUM_CTX
    assert call["options"]["num_predict"] == 1  # a throwaway: one token


def test_warmup_does_not_retry_when_ollama_is_down():
    client = FakeClient(ConnectionError("Failed to connect to Ollama"), {"message": "never"})
    with pytest.raises(errors.OllamaUnavailable):
        warmup.warm_up(client=client, sleep=lambda s: pytest.fail("slept before retrying a hopeless call"))
    assert len(client.calls) == 1


def test_warmup_does_not_retry_when_the_model_is_missing():
    import ollama

    client = FakeClient(ollama.ResponseError(f"model '{config.LLM_MODEL}' not found", 404), {"message": "never"})
    with pytest.raises(errors.ModelMissing) as exc:
        warmup.warm_up(client=client, sleep=lambda s: pytest.fail("slept"))
    assert len(client.calls) == 1 and config.LLM_MODEL in str(exc.value)

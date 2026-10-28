import asyncio

import pytest
from app.llm import Completion, LLMChain, LLMError


class FakeProvider:
    def __init__(self, name, replies):
        self.name = name
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, system, user, max_tokens):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else "ok"
        if isinstance(r, Exception):
            raise r
        return Completion(r, prompt_tokens=10, completion_tokens=5)

    async def close(self):
        pass


def parse(content):
    if content != "ok":
        raise ValueError("bad")
    return "parsed"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


def test_primary_used_when_healthy():
    a, b = FakeProvider("a", ["ok"]), FakeProvider("b", ["ok"])
    usage = {}
    chain = LLMChain([a, b], usage_get=lambda k: usage.get(k, 0),
                     usage_add=lambda k, n: usage.__setitem__(k, usage.get(k, 0) + n))
    assert run(chain.run("s", "u", parse)) == ("parsed", "a")
    assert b.calls == 0
    assert sum(usage.values()) == 15


@pytest.mark.parametrize("kind", ["payment", "auth", "server", "timeout", "rate_limit"])
def test_failover_on_errors(kind):
    clock = Clock()
    a, b = FakeProvider("a", [LLMError(kind, "x")]), FakeProvider("b", ["ok"])
    chain = LLMChain([a, b], clock=clock)
    assert run(chain.run("s", "u", parse)) == ("parsed", "b")
    assert chain.state["a"].last_error.startswith(kind)
    # пока основной «остывает», запросы идут сразу к запасному
    assert run(chain.run("s", "u", parse)) == ("parsed", "b")
    assert a.calls == 1


def test_returns_to_primary_after_cooldown():
    clock = Clock()
    a, b = FakeProvider("a", [LLMError("server", "x"), "ok"]), FakeProvider("b", ["ok"])
    chain = LLMChain([a, b], clock=clock)
    run(chain.run("s", "u", parse))
    clock.t += 15 * 60 + 1
    assert run(chain.run("s", "u", parse)) == ("parsed", "a")


def test_invalid_json_retried_once_then_failover():
    a, b = FakeProvider("a", ["мусор", "мусор"]), FakeProvider("b", ["ok"])
    chain = LLMChain([a, b], clock=Clock())
    assert run(chain.run("s", "u", parse)) == ("parsed", "b")
    assert a.calls == 2


def test_invalid_json_then_valid_on_retry():
    a = FakeProvider("a", ["мусор", "ok"])
    chain = LLMChain([a], clock=Clock())
    assert run(chain.run("s", "u", parse)) == ("parsed", "a")


def test_monthly_limit_skips_provider():
    a, b = FakeProvider("a", ["ok"]), FakeProvider("b", ["ok"])
    chain = LLMChain([a, b], usage_get=lambda k: 10_000 if k.endswith(":a") else 0, monthly_token_limit=5_000)
    assert run(chain.run("s", "u", parse)) == ("parsed", "b")
    assert a.calls == 0


def test_all_down_returns_none():
    a = FakeProvider("a", [LLMError("payment", "no money")])
    chain = LLMChain([a], clock=Clock())
    assert run(chain.run("s", "u", parse)) == (None, None)
    assert chain.active is None
    assert run(LLMChain([]).run("s", "u", parse)) == (None, None)

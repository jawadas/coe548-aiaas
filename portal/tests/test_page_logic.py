"""The pure logic behind the pages: usage maths, chart geometry, key names, chat history."""

from __future__ import annotations

import datetime as dt

from app.gateway import DayUsage, KeyInfo, KeyUsage
from app.routes.chat import HISTORY_BUDGET, ChatMessage, trim_history
from app.routes.keys import name_error
from app.services import usage


def day(date: dt.date, prompt: int, completion: int, *, requests: int = 1, by_key=()) -> DayUsage:
    return DayUsage(
        date=date,
        requests=requests,
        successful=requests,
        failed=0,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        spend=0.0,
        avg_latency_ms=1500.0 if requests else None,
        by_key=list(by_key),
    )


def key(token: str, name: str) -> KeyInfo:
    return KeyInfo(
        token=token, name=name, masked="sk-...abcd", kind="api", alias=None, created_at=None,
        last_active=None, rpm_limit=10, tpm_limit=30000, blocked=False, user_id="portal-1",
    )  # fmt: skip


def test_date_range_includes_today() -> None:
    start, end = usage.date_range(7, dt.date(2026, 11, 18))
    assert (start, end) == (dt.date(2026, 11, 12), dt.date(2026, 11, 18))


def test_nice_ceiling_rounds_up_to_a_round_number() -> None:
    assert usage.nice_ceiling(0) == 1000
    assert usage.nice_ceiling(2200) == 2500
    assert usage.nice_ceiling(46200) == 50000
    assert usage.nice_ceiling(50000) == 50000
    assert usage.nice_ceiling(50001) == 100000


def test_totals_and_success_rate() -> None:
    days = [day(dt.date(2026, 11, 17), 10, 90, requests=3), day(dt.date(2026, 11, 18), 20, 80)]
    totals = usage.totals(days)
    assert (totals.requests, totals.prompt_tokens, totals.completion_tokens) == (4, 30, 170)
    assert totals.success_rate == 100
    assert usage.totals([]).success_rate is None
    assert usage.change_percent(12, 10) == 20
    assert usage.change_percent(5, 0) is None


def test_chart_stacks_fill_the_plot_and_mark_today() -> None:
    days = [day(dt.date(2026, 11, 17), 1000, 1500), day(dt.date(2026, 11, 18), 500, 2000)]
    chart = usage.build_chart(days)
    first, today = chart.bars
    assert today.is_today
    assert today.label == "Today"
    assert first.label == "17 Nov"
    assert first.prompt_height + first.completion_height == 100  # 2500 of a 2500 axis
    assert today.completion_y == 0
    assert today.prompt_y == 80
    assert chart.ticks[0] == (0, "2.5k")
    assert chart.ticks[-1][0] == 100


def test_by_key_names_known_keys_and_keeps_revoked_ones() -> None:
    entries = [
        KeyUsage("a" * 64, "alias-a", 3, 3, 0, 30, 270, 300),
        KeyUsage("b" * 64, "alias-b", 1, 1, 0, 10, 90, 100),
    ]
    rows = usage.by_key(
        [day(dt.date(2026, 11, 18), 40, 360, by_key=entries)], [key("a" * 64, "Course")]
    )
    assert [(row.name, row.total_tokens, row.share) for row in rows] == [
        ("Course", 300, 75.0),
        ("Revoked key", 100, 25.0),
    ]


def test_key_name_rules() -> None:
    existing = [key("a" * 64, "Course project")]
    assert name_error("Notebook", existing) is None
    assert name_error("", existing)
    assert name_error("x" * 41, existing)
    assert name_error("course PROJECT", existing)
    assert name_error("bad\x00name", existing)


def test_chat_history_keeps_the_system_prompt_and_the_newest_turns() -> None:
    big = "x" * (HISTORY_BUDGET // 2 + 10)
    messages = [
        ChatMessage(role="system", content="Be brief."),
        ChatMessage(role="user", content=big[:16000]),
        ChatMessage(role="assistant", content=big[:16000]),
        ChatMessage(role="user", content=big[:16000]),
        ChatMessage(role="assistant", content=big[:16000]),
        ChatMessage(role="user", content="latest question"),
    ]
    sent = trim_history(messages)
    assert sent[0] == {"role": "system", "content": "Be brief."}
    assert sent[-1]["content"] == "latest question"
    assert sum(len(m["content"]) for m in sent[1:]) <= HISTORY_BUDGET
    assert len(sent) < len(messages)

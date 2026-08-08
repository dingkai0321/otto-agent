"""The wake-word matcher is a pure function — so it gets deterministic evals.
Whisper mangles phrases in predictable ways; these cases pin the fuzziness."""

import pytest

from otto.gateway.voice import matches_wake

SHOULD_WAKE = [
    ("otto otto", "otto otto"),
    ("Otto, otto!", "otto otto"),            # punctuation
    ("ottootto", "otto otto"),               # whisper drops the space
    ("so anyway otto otto schedule it", "otto otto"),  # embedded in speech
    ("auto otto", "otto otto"),              # one-letter mangle → fuzzy match
    ("Hey Otto", "hey otto"),
    ("hey computer, what's up", "hey computer"),
    # regression from the first live session: whisper wrote the wake word in
    # kana — variants after a comma cover other scripts
    ("わくわく", "otto otto,わくわく"),
    ("わくわくわく", "otto otto,わくわく"),
    ("小助手你好", "otto otto,小助手"),
]

SHOULD_NOT_WAKE = [
    ("what a nice day", "otto otto"),
    ("wake up call at nine", "otto otto"),
    ("", "otto otto"),
    ("otto otto", ""),                        # no wake word configured
    ("walk to work", "otto otto"),
]


@pytest.mark.parametrize("heard,wake", SHOULD_WAKE, ids=[h for h, _ in SHOULD_WAKE])
def test_wakes(heard, wake):
    assert matches_wake(heard, wake)


@pytest.mark.parametrize("heard,wake", SHOULD_NOT_WAKE, ids=[h or "empty" for h, _ in SHOULD_NOT_WAKE])
def test_stays_asleep(heard, wake):
    assert not matches_wake(heard, wake)

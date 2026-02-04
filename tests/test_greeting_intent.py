import pytest

from services.context.ctx_reader import classify_intent


@pytest.mark.parametrize(
	"text",
	[
		"hello",
		"hi",
		"hey",
		"gm",
		"good morning",
		"Hello!",
		"GM!!!",
		"good morning :) ",
	],
)
def test_classify_intent_greeting_only(text: str) -> None:
	assert classify_intent(text) == "GREETING"


@pytest.mark.parametrize(
	"text",
	[
		"hello there",
		"hi spy",
		"gm what's the market doing",
		"hey can you check futures",
	],
)
def test_classify_intent_not_greeting_when_extra_words(text: str) -> None:
	assert classify_intent(text) != "GREETING"

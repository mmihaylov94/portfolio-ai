"""The shaping of conversation history: the window, and what the classifier sees."""

from portfolio_ai.assistant.memory import HistoryMessage, as_input, previous_exchange, window

CONVERSATION = [
    HistoryMessage("user", "Does he use Laravel?"),
    HistoryMessage("assistant", "Yes, it is his main framework."),
    HistoryMessage("user", "What about Vue?"),
    HistoryMessage("assistant", "Vue and Nuxt, mostly."),
]


def test_the_window_is_counted_in_exchanges() -> None:
    """25 in the setting means 25 questions and their answers -- 50 messages --
    because that is what n8n's number meant."""
    assert window(CONVERSATION, 1) == CONVERSATION[-2:]
    assert window(CONVERSATION, 2) == CONVERSATION
    assert window(CONVERSATION, 25) == CONVERSATION


def test_a_window_of_nothing_shows_nothing() -> None:
    assert window(CONVERSATION, 0) == []


def test_the_classifier_sees_the_last_question_and_its_answer() -> None:
    assert previous_exchange(CONVERSATION) == CONVERSATION[-2:]


def test_a_first_message_has_no_previous_exchange() -> None:
    assert previous_exchange([]) == []


def test_history_converts_to_the_shape_the_api_expects() -> None:
    assert as_input(CONVERSATION[:2]) == [
        {"role": "user", "content": "Does he use Laravel?"},
        {"role": "assistant", "content": "Yes, it is his main framework."},
    ]

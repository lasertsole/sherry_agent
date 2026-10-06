"""Token-usage capture of the AI message row builder.

The toolbar's usage panel divides the session's cached-prompt tokens by its
prompt tokens, so the builder has to find the cache number wherever the provider
puts it: langchain's normalized ``input_token_details.cache_read`` (OpenAI
shape) or the raw ``response_metadata.token_usage`` payload (DeepSeek's
``prompt_cache_hit_tokens``, Anthropic's ``cache_read_input_tokens``).
"""

from langchain_core.messages import AIMessage

from context_engine.store.core import AIMessageRowBuilder

pytestmark = __import__("pytest").mark.unit


def _row(message: AIMessage) -> dict:
    return AIMessageRowBuilder().build(message, "s1")


def test_reads_the_normalized_cache_read():
    row = _row(
        AIMessage(
            "hi",
            usage_metadata={
                "input_tokens": 1_000,
                "output_tokens": 10,
                "total_tokens": 1_010,
                "input_token_details": {"cache_read": 400},
            },
        )
    )

    assert row["cache_read_tokens"] == 400


def test_reads_deepseek_raw_cache_hit_tokens():
    row = _row(
        AIMessage(
            "hi",
            response_metadata={"token_usage": {"prompt_cache_hit_tokens": 517}},
        )
    )

    assert row["cache_read_tokens"] == 517


def test_reads_the_nested_openai_cached_tokens():
    row = _row(
        AIMessage(
            "hi",
            response_metadata={"token_usage": {"prompt_tokens_details": {"cached_tokens": 88}}},
        )
    )

    assert row["cache_read_tokens"] == 88


def test_reads_anthropic_cache_read_input_tokens():
    row = _row(AIMessage("hi", response_metadata={"usage": {"cache_read_input_tokens": 42}}))

    assert row["cache_read_tokens"] == 42


def test_without_any_cache_field_the_column_stays_null():
    row = _row(
        AIMessage(
            "hi",
            usage_metadata={"input_tokens": 1_000, "output_tokens": 10, "total_tokens": 1_010},
        )
    )

    assert row["cache_read_tokens"] is None


# ---------------------------------------------------------------------------
# Origin of injected AI carriers
# ---------------------------------------------------------------------------


def test_a_plain_answer_keeps_the_origin_column_null():
    """The chat renders a non-user origin as a neutral card: a model answer has none."""
    assert _row(AIMessage("普通回答"))["origin"] is None


def test_an_injected_ai_carrier_persists_its_metadata_origin():
    """ProjectDirNoticeMiddleware tags its notice; the column must survive into the row."""
    message = AIMessage(
        "[项目目录已切换] the working directory moved",
        metadata={"origin": "project_dir", "internal": True},
    )

    assert _row(message)["origin"] == "project_dir"

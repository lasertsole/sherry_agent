"""Unit tests for the restricted-execution AST guard (2026-11-09 audit H4)."""

from __future__ import annotations

import pytest

from agent.tools.pub_base.safe_exec import validate_restricted_code

pytestmark = [pytest.mark.unit]


class TestAllowed:
    @pytest.mark.parametrize(
        "source",
        [
            "print(sum(range(5)))",
            "from sherry_tools import read_file\nprint(read_file('x'))",
            "if __name__ == '__main__':\n    print('main')",
            "class Point:\n    x = 1\nprint(Point.x)",
            "print({'a': 1}.get('a'))",
        ],
    )
    def test_normal_task_code_passes(self, source):
        validate_restricted_code(source, label="test")


class TestRefused:
    @pytest.mark.parametrize(
        "source",
        [
            "print(().__class__.__base__.__subclasses__())",
            "print(''.__class__)",
            "print(type(1).__mro__)",
            "f = print.__globals__",
            "print(__builtins__)",
            "print(obj.__getattribute__('x'))",
        ],
    )
    def test_introspection_escape_refused(self, source):
        with pytest.raises(ValueError, match="refused"):
            validate_restricted_code(source, label="test")

    def test_syntax_error_is_a_refusal(self):
        with pytest.raises(ValueError, match="syntax error"):
            validate_restricted_code("def broken(:\n    pass", label="test")

    def test_message_names_the_offending_attribute(self):
        with pytest.raises(ValueError, match="__subclasses__"):
            validate_restricted_code("x.__subclasses__()", label="test")

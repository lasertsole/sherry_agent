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
            # Formatting itself is fine; only a dunder accessor inside the
            # literal template is refused (L11).
            "print('{}'.format(1))",
            "print('{0}'.format(1))",
            "print('{name}'.format(name='x'))",
            "print('{0!r:>10}'.format(1))",
            "print('{0.value}'.format(obj))",
            "template = '{0}'\nprint(template.format(1))",
            "print(f'{1}')",
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
            # A dunder reached through a literal format field: the AST cannot
            # see inside the string, so the format guard catches it (L11).
            "print('{0.__class__}'.format(1))",
            "print('{0[__class__]}'.format({}))",
            "print(str.format('{0.__init__}', 1))",
            "print('{0.__class__.__name__}'.format(1))",
            "print('{.__class__}'.format(1))",
            "print('{0:{1.__class__}}'.format(1, 2))",
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

    def test_format_message_names_the_replacement_field(self):
        with pytest.raises(ValueError, match="0.__class__"):
            validate_restricted_code("print('{0.__class__}'.format(1))", label="test")

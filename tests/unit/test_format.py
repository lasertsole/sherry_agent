"""Unit tests for pub_func/format/*.py — all 6 format modules."""

import pytest
import json
from pydantic import BaseModel
from pub_func.format.escape_xml import escape_xml
from pub_func.format.escape_prompt_braces import escape_prompt_braces
from pub_func.format.sanitize_content import sanitize_content
from pub_func.format.parse_markdown_json import parse_markdown_json
from pub_func.format.render_template import template_render, render_template_file


# --- escape_xml ---

class TestEscapeXml:
    def test_ampersand(self):
        assert escape_xml("a&b") == "a&amp;b"

    def test_less_than(self):
        assert escape_xml("a<b") == "a&lt;b"

    def test_greater_than(self):
        assert escape_xml("a>b") == "a&gt;b"

    def test_double_quote(self):
        assert escape_xml('a"b') == "a&quot;b"

    def test_all_special_chars(self):
        assert escape_xml('&<>"') == "&amp;&lt;&gt;&quot;"

    def test_empty_string(self):
        assert escape_xml("") == ""

    def test_no_special_chars(self):
        assert escape_xml("hello world") == "hello world"

    def test_already_escaped(self):
        assert escape_xml("&amp;") == "&amp;amp;"


# --- escape_prompt_braces ---

class TestEscapePromptBraces:
    def test_single_open(self):
        assert escape_prompt_braces("{") == "{{"

    def test_single_close(self):
        assert escape_prompt_braces("}") == "}}"

    def test_pair(self):
        assert escape_prompt_braces("{}") == "{{}}"

    def test_multiple(self):
        assert escape_prompt_braces("{a} {b}") == "{{a}} {{b}}"

    def test_empty(self):
        assert escape_prompt_braces("") == ""

    def test_no_braces(self):
        assert escape_prompt_braces("hello") == "hello"

    def test_nested(self):
        assert escape_prompt_braces("{{a}}") == "{{{{a}}}}"


# --- sanitize_content ---

class TestSanitizeContent:
    def test_removes_parentheses_content_cn(self):
        assert sanitize_content("hello（世界）world") == "hello world"

    def test_removes_parentheses_content_en(self):
        assert sanitize_content("hello (world) foo") == "hello foo"

    def test_removes_newlines(self):
        assert sanitize_content("line1\nline2") == "line1 line2"

    def test_removes_carriage_return(self):
        assert sanitize_content("line1\r\nline2") == "line1 line2"

    def test_collapses_tabs(self):
        assert sanitize_content("a\tb") == "a b"

    def test_collapses_spaces(self):
        assert sanitize_content("a   b") == "a b"

    def test_strips_think_tags(self):
        assert sanitize_content("before<think>reasoning</think>after") == "beforeafter"

    def test_strips_thinking_tags(self):
        assert sanitize_content("before<thinking>reasoning</thinking>after") == "beforeafter"

    def test_strips_multiline_think(self):
        content = "start<think>\nline1\nline2\n</think>end"
        result = sanitize_content(content)
        assert "line1" not in result
        assert "start" in result
        assert "end" in result

    def test_strips(self):
        assert sanitize_content("  hello  ") == "hello"

    def test_empty(self):
        assert sanitize_content("") == ""

    def test_combined(self):
        content = "Hello（test）\nworld   extra"
        result = sanitize_content(content)
        assert "(" not in result
        assert "\n" not in result
        assert "  " not in result


# --- parse_markdown_json --

class SampleModel(BaseModel):
    name: str
    value: int


class TestParseMarkdownJson:
    def test_raw_json(self):
        data = '{"name": "test", "value": 42}'
        result = parse_markdown_json(data, SampleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_json_in_code_block(self):
        content = 'Some text\n```json\n{"name": "test", "value": 42}\n```\nMore text'
        result = parse_markdown_json(content, SampleModel)
        assert result.name == "test"
        assert result.value == 42

    def test_json_block_with_surrounding_text(self):
        content = 'Here is the result:\n```json\n{"name": "hello", "value": 99}\n```'
        result = parse_markdown_json(content, SampleModel)
        assert result.name == "hello"
        assert result.value == 99

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            parse_markdown_json("not json at all", SampleModel)


# --- template_render ---

class TestTemplateRender:
    def test_simple_replacement(self):
        result = template_render("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_vars(self):
        result = template_render(
            "{{ a }} and {{ b }}",
            {"a": "X", "b": "Y"}
        )
        assert result == "X and Y"

    def test_missing_var_left_as_is(self):
        result = template_render("Hello {{ missing }}", {})
        assert result == "Hello {{ missing }}"

    def test_none_value_replaced_with_empty(self):
        result = template_render("Value: {{ x }}", {"x": None})
        assert result == "Value: "

    def test_int_value(self):
        result = template_render("Count: {{ n }}", {"n": 42})
        assert result == "Count: 42"

    def test_whitespace_tolerance(self):
        result = template_render("Hello {{name}}", {"name": "World"})
        assert result == "Hello World"

    def test_no_placeholders(self):
        result = template_render("No vars here", {})
        assert result == "No vars here"

    def test_empty_template(self):
        result = template_render("", {"a": 1})
        assert result == ""


# --- render_template_file ---

class TestRenderTemplateFile:
    def test_renders_file(self, tmp_path):
        template_file = tmp_path / "test.md"
        template_file.write_text("Hello {{ name }}!", encoding="utf-8")
        result = render_template_file(str(template_file), {"name": "Sherry"})
        assert result == "Hello Sherry!"

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            render_template_file(str(tmp_path / "nonexistent.md"), {})

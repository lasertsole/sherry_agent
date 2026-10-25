"""Tree-sitter symbol/call extraction — the grammar layer of the code index.

Node names below were measured against the pinned grammars
(tree-sitter 0.26.0, tree-sitter-python 0.25.0, tree-sitter-typescript
0.23.2, tree-sitter-rust 0.24.2, tree-sitter-go 0.25.0) with a one-shot probe,
NOT copied from a plan: Python ``function_definition`` / ``class_definition`` /
``call`` (callee name via ``function`` field: ``identifier`` or ``attribute``);
TypeScript ``function_declaration`` / ``class_declaration`` /
``method_definition`` / ``variable_declarator`` (arrow/function expressions) /
``call_expression`` (``member_expression`` property); Rust ``function_item`` /
``struct_item`` / ``enum_item`` / ``trait_item`` / ``impl_item`` /
``call_expression`` / ``macro_invocation``; Go ``function_declaration`` /
``method_declaration`` / ``type_spec`` with ``struct_type`` / ``interface_type``
/ ``call_expression`` (``selector_expression`` field).

This module is pure: bytes in, symbol/call definitions out. No disk, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from collections.abc import Callable

if TYPE_CHECKING:
    from tree_sitter import Node, Parser


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """One symbol definition; ``parent_index`` indexes into the same file's list."""

    name: str
    kind: str
    line_start: int
    line_end: int
    parent_index: int | None = None
    doc_string: str | None = None


@dataclass(frozen=True, slots=True)
class CallSite:
    """One call occurrence attributed to the enclosing symbol index."""

    caller_index: int
    callee_name: str
    line: int


@dataclass(frozen=True, slots=True)
class ImportSite:
    """One import occurrence in a file (module scope uses ``importer_index=-1``)."""

    importer_index: int
    imported_name: str
    line: int


@dataclass(frozen=True, slots=True)
class InheritSite:
    """One class/interface/trait inheritance edge."""

    child_index: int
    parent_name: str
    inherit_type: str
    line: int


@dataclass(frozen=True, slots=True)
class MixinSite:
    """One trait/interface/mixin composition edge (``implements`` / embedding)."""

    host_index: int
    mixin_name: str
    line: int


@dataclass(slots=True)
class ExtractionResult:
    """Everything one parsed file yielded."""

    symbols: list[SymbolDef] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportSite] = field(default_factory=list)
    inherits: list[InheritSite] = field(default_factory=list)
    mixins: list[MixinSite] = field(default_factory=list)
    error: str | None = None


# Grammar loaders are resolved lazily so importing this module never pays the
# tree-sitter import cost until the first real index build.
_GRAMMAR_LOADERS: dict[str, Callable[[], object]] = {}
_GRAMMAR_NAMES: tuple[str, ...] = ("python", "typescript", "tsx", "rust", "go")
_PARSERS: dict[str, Parser] = {}
_LANGUAGE_ALIASES: dict[str, str] = {"javascript": "typescript", "jsx": "tsx"}


def _load_grammars() -> dict[str, Callable[[], object]]:
    """Import the four grammar packages once and cache their capsule factories."""
    if _GRAMMAR_LOADERS:
        return _GRAMMAR_LOADERS
    import tree_sitter_go
    import tree_sitter_python
    import tree_sitter_rust
    import tree_sitter_typescript

    _GRAMMAR_LOADERS.update(
        {
            "python": tree_sitter_python.language,
            "typescript": tree_sitter_typescript.language_typescript,
            "tsx": tree_sitter_typescript.language_tsx,
            "rust": tree_sitter_rust.language,
            "go": tree_sitter_go.language,
        }
    )
    return _GRAMMAR_LOADERS


def canonical_language(language: str) -> str:
    """Map the config language aliases (``javascript``/``jsx``) to a real grammar."""
    return _LANGUAGE_ALIASES.get(language, language)


def supported_languages() -> tuple[str, ...]:
    """Languages with a live tree-sitter grammar in this build."""
    return _GRAMMAR_NAMES


def get_parser(language: str) -> Parser:
    """Return a cached parser for *language* (modelled on the measured API)."""
    from tree_sitter import Language, Parser

    canonical = canonical_language(language)
    parser = _PARSERS.get(canonical)
    if parser is None:
        loader = _load_grammars().get(canonical)
        if loader is None:
            raise ValueError(f"unsupported language: {language!r}")
        parser = Parser(Language(loader()))
        _PARSERS[canonical] = parser
    return parser


def _text(node: Node) -> str:
    """Decode a node's source span, tolerating malformed bytes."""
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


class _Ctx:
    """Mutable extraction state shared across one file's recursive walk."""

    __slots__ = (
        "symbols",
        "calls",
        "imports",
        "inherits",
        "mixins",
        "sym_stack",
        "class_stack",
        "impl_stack",
        "class_by_name",
    )

    def __init__(self) -> None:
        self.symbols: list[SymbolDef] = []
        self.calls: list[CallSite] = []
        self.imports: list[ImportSite] = []
        self.inherits: list[InheritSite] = []
        self.mixins: list[MixinSite] = []
        self.sym_stack: list[int] = []
        self.class_stack: list[int | None] = []
        self.impl_stack: list[int | None] = []
        self.class_by_name: dict[str, int] = {}


def _add_symbol(
    ctx: _Ctx,
    name: str,
    kind: str,
    line_start: int,
    line_end: int,
    parent: int | None,
    doc: str | None = None,
) -> int:
    idx = len(ctx.symbols)
    ctx.symbols.append(SymbolDef(name, kind, line_start, line_end, parent, doc))
    return idx


def _record_call(ctx: _Ctx, callee_name: str | None, line: int) -> None:
    if callee_name and ctx.sym_stack:
        ctx.calls.append(CallSite(ctx.sym_stack[-1], callee_name, line))


def _record_import(ctx: _Ctx, imported_name: str | None, line: int) -> None:
    name = (imported_name or "").strip()
    if name:
        importer = ctx.sym_stack[-1] if ctx.sym_stack else -1
        ctx.imports.append(ImportSite(importer, name, line))


def _record_inherit(
    ctx: _Ctx, child_index: int, parent_name: str | None, inherit_type: str, line: int
) -> None:
    name = clean_type_name(parent_name)
    if name:
        ctx.inherits.append(InheritSite(child_index, name, inherit_type, line))


def _record_mixin(ctx: _Ctx, host_index: int, mixin_name: str | None, line: int) -> None:
    name = clean_type_name(mixin_name)
    if name:
        ctx.mixins.append(MixinSite(host_index, name, line))


def clean_type_name(text: str | None) -> str:
    """Reduce a type expression to its final identifier (generics/paths stripped)."""
    value = (text or "").strip()
    if not value:
        return ""
    value = value.split("<", 1)[0].split("[", 1)[0]
    value = value.lstrip("&*")
    value = value.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return value.strip()


def _strip_quotes(text: str) -> str:
    return text.strip().strip("`\"'").strip()


def _field_text(node: Node, field_name: str) -> str:
    child = node.child_by_field_name(field_name)
    return _text(child) if child is not None else ""


def _python_doc(node: Node) -> str | None:
    """Extract a Python function/class docstring (first statement string)."""
    body = node.child_by_field_name("body")
    if body is None:
        return None
    for child in body.named_children:
        if child.type != "expression_statement":
            return None
        for inner in child.named_children:
            if inner.type == "string":
                raw = _text(inner)
                return raw.strip("\"'") or None
        return None
    return None


def _python_call_name(fn: Node | None) -> str | None:
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        return _text(attr) if attr is not None else None
    return None


def _python_node(node: Node, ctx: _Ctx, node_type: str) -> bool:
    if node_type == "import_statement":
        for child in node.named_children:
            if child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                _record_import(
                    ctx,
                    _text(name_node) if name_node is not None else _text(child),
                    node.start_point[0] + 1,
                )
            else:
                _record_import(ctx, _text(child), node.start_point[0] + 1)
        return True
    if node_type == "import_from_statement":
        module_name = _field_text(node, "module_name")
        recorded = False
        skipped_module = False
        for child in node.named_children:
            if not skipped_module and module_name and _text(child) == module_name:
                skipped_module = True
                continue
            if child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                _record_import(
                    ctx,
                    _text(name_node) if name_node is not None else _text(child),
                    node.start_point[0] + 1,
                )
                recorded = True
            elif child.type in ("dotted_name", "identifier"):
                _record_import(ctx, _text(child), node.start_point[0] + 1)
                recorded = True
        if not recorded and module_name:
            # ``from a.b import *`` keeps the module as the graph target.
            _record_import(ctx, module_name, node.start_point[0] + 1)
        return True
    if node_type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        parent = ctx.class_stack[-1] if ctx.class_stack else None
        kind = "method" if parent is not None else "function"
        line_start = node.start_point[0] + 1
        parent_node = node.parent
        if parent_node is not None and parent_node.type == "decorated_definition":
            line_start = parent_node.start_point[0] + 1
        idx = _add_symbol(
            ctx,
            _text(name_node),
            kind,
            line_start,
            node.end_point[0] + 1,
            parent,
            _python_doc(node),
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "python")
        ctx.sym_stack.pop()
        return True
    if node_type == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx,
            _text(name_node),
            "class",
            node.start_point[0] + 1,
            node.end_point[0] + 1,
            None,
            _python_doc(node),
        )
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for base in superclasses.named_children:
                if base.type in ("identifier", "attribute"):
                    _record_inherit(ctx, idx, _text(base), "class", node.start_point[0] + 1)
        ctx.class_by_name[_text(name_node)] = idx
        ctx.class_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "python")
        ctx.class_stack.pop()
        return True
    if node_type == "call":
        _record_call(
            ctx, _python_call_name(node.child_by_field_name("function")), node.start_point[0] + 1
        )
    return False


def _ts_call_name(fn: Node | None) -> str | None:
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return _text(prop) if prop is not None else None
    return None


def _ts_record_heritage(node: Node, ctx: _Ctx, child_index: int) -> None:
    """Record ``extends`` (inherit) and ``implements`` (mixin) clauses of a class."""
    line = node.start_point[0] + 1
    heritage = next(
        (child for child in node.named_children if child.type == "class_heritage"), None
    )
    if heritage is None:
        return
    for clause in heritage.named_children:
        if clause.type == "extends_clause":
            for parent in clause.named_children:
                if parent.type in ("identifier", "type_identifier", "generic_type"):
                    _record_inherit(ctx, child_index, _text(parent), "class", line)
        elif clause.type == "implements_clause":
            for mixin in clause.named_children:
                if mixin.type in ("type_identifier", "generic_type"):
                    _record_mixin(ctx, child_index, _text(mixin), line)


def _ts_node(node: Node, ctx: _Ctx, node_type: str) -> bool:
    if node_type == "import_statement":
        module = _strip_quotes(_field_text(node, "source"))
        recorded = False
        clause = next(
            (child for child in node.named_children if child.type == "import_clause"), None
        )
        if clause is not None:
            for child in clause.named_children:
                if child.type == "identifier":
                    _record_import(ctx, _text(child), node.start_point[0] + 1)
                    recorded = True
                elif child.type == "named_imports":
                    for spec in child.named_children:
                        if spec.type != "import_specifier":
                            continue
                        name_node = spec.child_by_field_name("name")
                        _record_import(
                            ctx,
                            _text(name_node) if name_node is not None else _text(spec),
                            node.start_point[0] + 1,
                        )
                        recorded = True
                elif child.type == "namespace_import":
                    for inner in child.named_children:
                        if inner.type == "identifier":
                            _record_import(ctx, _text(inner), node.start_point[0] + 1)
                            recorded = True
        if not recorded and module:
            _record_import(ctx, module, node.start_point[0] + 1)
        return True
    if node_type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx, _text(name_node), "function", node.start_point[0] + 1, node.end_point[0] + 1, None
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "typescript")
        ctx.sym_stack.pop()
        return True
    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx, _text(name_node), "class", node.start_point[0] + 1, node.end_point[0] + 1, None
        )
        _ts_record_heritage(node, ctx, idx)
        ctx.class_by_name[_text(name_node)] = idx
        ctx.class_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "typescript")
        ctx.class_stack.pop()
        return True
    if node_type == "interface_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx, _text(name_node), "interface", node.start_point[0] + 1, node.end_point[0] + 1, None
        )
        for child in node.named_children:
            if child.type != "extends_type_clause":
                continue
            for parent in child.named_children:
                if parent.type in ("type_identifier", "generic_type"):
                    _record_inherit(ctx, idx, _text(parent), "interface", node.start_point[0] + 1)
        ctx.class_by_name[_text(name_node)] = idx
        ctx.class_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "typescript")
        ctx.class_stack.pop()
        return True
    if node_type == "method_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        parent = ctx.class_stack[-1] if ctx.class_stack else None
        idx = _add_symbol(
            ctx,
            _text(name_node),
            "method",
            node.start_point[0] + 1,
            node.end_point[0] + 1,
            parent,
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "typescript")
        ctx.sym_stack.pop()
        return True
    if node_type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("arrow_function", "function_expression"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                idx = _add_symbol(
                    ctx,
                    _text(name_node),
                    "function",
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    None,
                )
                ctx.sym_stack.append(idx)
                for child in node.children:
                    _walk(child, ctx, "typescript")
                ctx.sym_stack.pop()
                return True
        return False
    if node_type == "call_expression":
        _record_call(
            ctx, _ts_call_name(node.child_by_field_name("function")), node.start_point[0] + 1
        )
    return False


def _rust_call_name(fn: Node | None) -> str | None:
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "field_expression":
        field_node = fn.child_by_field_name("field")
        return _text(field_node) if field_node is not None else None
    if fn.type == "scoped_identifier":
        name_node = fn.child_by_field_name("name")
        return _text(name_node) if name_node is not None else None
    return None


def _rust_use_targets(node: Node | None) -> list[str]:
    """Collect the imported path(s) from a Rust ``use`` argument."""
    if node is None:
        return []
    if node.type == "use_as_clause":
        # Keep the original path, not the local alias.
        for child in node.named_children:
            if child.type in ("scoped_identifier", "identifier"):
                return [_text(child)]
        return []
    if node.type == "scoped_use_list":
        targets: list[str] = []
        for child in node.named_children:
            if child.type == "use_list":
                targets.extend(
                    _text(item)
                    for item in child.named_children
                    if item.type in ("identifier", "scoped_identifier")
                )
        return targets
    if node.type in ("use_list", "scoped_identifier", "identifier"):
        if node.type == "use_list":
            return [
                _text(item)
                for item in node.named_children
                if item.type in ("identifier", "scoped_identifier")
            ]
        return [_text(node)]
    return [_text(node)]


def _rust_node(node: Node, ctx: _Ctx, node_type: str) -> bool:
    if node_type == "use_declaration":
        for target in _rust_use_targets(node.child_by_field_name("argument")):
            _record_import(ctx, target, node.start_point[0] + 1)
        return True
    if node_type == "function_item":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        parent = ctx.class_stack[-1] if ctx.class_stack else None
        kind = "method" if ctx.impl_stack else "function"
        idx = _add_symbol(
            ctx, _text(name_node), kind, node.start_point[0] + 1, node.end_point[0] + 1, parent
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "rust")
        ctx.sym_stack.pop()
        return True
    if node_type in ("struct_item", "enum_item", "trait_item", "union_item"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx, _text(name_node), "class", node.start_point[0] + 1, node.end_point[0] + 1, None
        )
        if node_type == "trait_item":
            bounds = next(
                (child for child in node.named_children if child.type == "trait_bounds"), None
            )
            if bounds is not None:
                for bound in bounds.named_children:
                    if bound.type == "type_identifier":
                        _record_inherit(ctx, idx, _text(bound), "trait", node.start_point[0] + 1)
        ctx.class_by_name[_text(name_node)] = idx
        ctx.class_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "rust")
        ctx.class_stack.pop()
        return True
    if node_type == "impl_item":
        type_name = clean_type_name(_field_text(node, "type"))
        trait_name = _field_text(node, "trait")
        host_index = ctx.class_by_name.get(type_name)
        if host_index is not None and trait_name:
            _record_mixin(ctx, host_index, trait_name, node.start_point[0] + 1)
        ctx.class_stack.append(host_index)
        ctx.impl_stack.append(host_index)
        for child in node.children:
            _walk(child, ctx, "rust")
        ctx.impl_stack.pop()
        ctx.class_stack.pop()
        return True
    if node_type == "call_expression":
        _record_call(
            ctx, _rust_call_name(node.child_by_field_name("function")), node.start_point[0] + 1
        )
    elif node_type == "macro_invocation":
        macro = node.child_by_field_name("macro")
        _record_call(ctx, _text(macro) if macro is not None else None, node.start_point[0] + 1)
    return False


def _go_receiver_type(node: Node) -> str | None:
    """Find the receiver struct name inside a Go method's ``receiver`` field."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    stack = [receiver]
    while stack:
        current = stack.pop()
        if current.type == "type_identifier":
            return _text(current)
        stack.extend(current.children)
    return None


def _go_call_name(fn: Node | None) -> str | None:
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "selector_expression":
        field_node = fn.child_by_field_name("field")
        return _text(field_node) if field_node is not None else None
    return None


def _go_import_targets(node: Node) -> list[str]:
    """Collect import paths from an ``import`` declaration (single or grouped)."""
    targets: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "import_spec":
            literal = next(
                (
                    child
                    for child in current.named_children
                    if child.type == "interpreted_string_literal"
                ),
                None,
            )
            if literal is not None:
                targets.append(_strip_quotes(_text(literal)))
            continue
        stack.extend(current.named_children)
    targets.reverse()
    return targets


def _go_node(node: Node, ctx: _Ctx, node_type: str) -> bool:
    if node_type == "import_declaration":
        for target in _go_import_targets(node):
            _record_import(ctx, target, node.start_point[0] + 1)
        return True
    if node_type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        idx = _add_symbol(
            ctx, _text(name_node), "function", node.start_point[0] + 1, node.end_point[0] + 1, None
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "go")
        ctx.sym_stack.pop()
        return True
    if node_type == "method_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        parent = ctx.class_by_name.get(_go_receiver_type(node) or "")
        idx = _add_symbol(
            ctx, _text(name_node), "method", node.start_point[0] + 1, node.end_point[0] + 1, parent
        )
        ctx.sym_stack.append(idx)
        for child in node.children:
            _walk(child, ctx, "go")
        ctx.sym_stack.pop()
        return True
    if node_type == "type_spec":
        type_node = node.child_by_field_name("type")
        if type_node is not None and type_node.type in ("struct_type", "interface_type"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                idx = _add_symbol(
                    ctx,
                    _text(name_node),
                    "class",
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    None,
                )
                line = node.start_point[0] + 1
                if type_node.type == "struct_type":
                    # An embedded field has a type but no field name; Go
                    # composes behaviour through these embeddings.
                    fields = next(
                        (
                            child
                            for child in type_node.named_children
                            if child.type == "field_declaration_list"
                        ),
                        None,
                    )
                    if fields is not None:
                        for field in fields.named_children:
                            if field.type != "field_declaration":
                                continue
                            if field.child_by_field_name("name") is not None:
                                continue
                            embedded = field.child_by_field_name("type")
                            _record_mixin(
                                ctx,
                                idx,
                                _text(embedded) if embedded is not None else _text(field),
                                line,
                            )
                else:
                    for elem in type_node.named_children:
                        if elem.type != "type_elem":
                            continue
                        for parent in elem.named_children:
                            if parent.type in ("type_identifier", "qualified_type", "generic_type"):
                                _record_inherit(ctx, idx, _text(parent), "interface", line)
                ctx.class_by_name[_text(name_node)] = idx
                return True
        return False
    if node_type == "call_expression":
        _record_call(
            ctx, _go_call_name(node.child_by_field_name("function")), node.start_point[0] + 1
        )
    return False


_DISPATCH: dict[str, Callable[[Node, _Ctx, str], bool]] = {
    "python": _python_node,
    "typescript": _ts_node,
    "tsx": _ts_node,
    "rust": _rust_node,
    "go": _go_node,
}


def _walk(node: Node, ctx: _Ctx, language: str) -> None:
    if _DISPATCH[language](node, ctx, node.type):
        return
    for child in node.children:
        _walk(child, ctx, language)


def extract_symbols(source: bytes, language: str) -> ExtractionResult:
    """Parse *source* and return its symbols + call sites (never raises on grammar errors)."""
    canonical = canonical_language(language)
    try:
        parser = get_parser(canonical)
    except (ValueError, ImportError) as exc:
        return ExtractionResult(error=f"unsupported language: {exc}")
    tree = parser.parse(source)
    if tree.root_node.has_error:
        # Fail-open per spec: a file with syntax errors is skipped entirely and
        # recorded in index_meta, never partially indexed.
        return ExtractionResult(error="syntax errors in file")
    ctx = _Ctx()
    _walk(tree.root_node, ctx, canonical)
    return ExtractionResult(
        symbols=ctx.symbols,
        calls=ctx.calls,
        imports=ctx.imports,
        inherits=ctx.inherits,
        mixins=ctx.mixins,
    )

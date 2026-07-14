"""AST-based rewriter that prevents silent exception swallowing in except blocks."""

import ast
from loguru import logger


class _NoSwallowRewriter(ast.NodeTransformer):

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        self.generic_visit(node)
        if not node.body:
            return node
        if self._has_raise(node):
            return node
        if len(node.body) == 1:
            stmt = node.body[0]
            if isinstance(stmt, ast.Pass):
                return self._replace_with_raise(node, stmt)
            if isinstance(stmt, ast.Return) and self._is_none_return(stmt):
                return self._replace_with_raise(node, stmt)
        raise_stmt = ast.Raise()
        ast.copy_location(raise_stmt, node.body[-1])
        ast.fix_missing_locations(raise_stmt)
        node.body.append(raise_stmt)
        return node

    @staticmethod
    def _has_raise(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                return True
        return False

    @staticmethod
    def _is_none_return(node: ast.Return) -> bool:
        val = node.value
        if val is None:
            return True
        if isinstance(val, ast.Constant) and val.value is None:
            return True
        return False

    @staticmethod
    def _replace_with_raise(handler: ast.ExceptHandler, orig: ast.stmt) -> ast.ExceptHandler:
        raise_stmt = ast.Raise()
        ast.copy_location(raise_stmt, orig)
        ast.fix_missing_locations(raise_stmt)
        handler.body = [raise_stmt]
        return handler


def rewrite_no_swallow(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    try:
        rewritten = _NoSwallowRewriter().visit(tree)
        ast.fix_missing_locations(rewritten)
        return ast.unparse(rewritten)
    except Exception as e:
        logger.debug("AST rewrite failed, using original: {}", e)
        return source

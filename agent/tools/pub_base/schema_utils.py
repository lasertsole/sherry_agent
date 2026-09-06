"""Schema descriptor utilities (audit 1.1.5).

langchain_core defines ``tool_call_schema`` as a bare instance ``@property``,
so class-level access (``MyTool.tool_call_schema``) would return the bare
property object instead of the schema model. This factory builds a descriptor
that forwards BOTH access forms to the inherited property getter (class access
synthesizes a throwaway instance), replacing the per-tool hardcoded copies.
"""

from typing import Any, Type


def class_or_instance_schema(parent_cls: type) -> Any:
    """Build a descriptor forwarding class/instance access to
    ``parent_cls.tool_call_schema``.

    Args:
        parent_cls: The base tool class whose ``tool_call_schema`` property
            should be invoked (e.g. ``ShellTool``, ``PythonREPLTool``).

    Returns:
        A descriptor instance suitable as a class attribute.
    """

    class _ClassOrInstanceSchema:
        def __get__(self, obj: Any, objtype: Type | None = None) -> Any:
            # Class access synthesizes a throwaway instance (cheap: all-defaults
            # pydantic model) so the inherited getter runs on a real self.
            target = obj if obj is not None else (objtype or parent_cls)()
            return parent_cls.tool_call_schema.__get__(target, type(target))

    return _ClassOrInstanceSchema()


__all__ = ["class_or_instance_schema"]

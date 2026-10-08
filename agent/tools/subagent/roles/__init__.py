"""Functional role definitions for sub-agents: built-in defaults + loader."""

from .loader import (
    RoleDefinition as RoleDefinition,
    get_roles_dir as get_roles_dir,
    invalidate_role_cache as invalidate_role_cache,
    load_all_role_definitions as load_all_role_definitions,
    load_role_definition as load_role_definition,
)

__all__ = [
    "RoleDefinition",
    "get_roles_dir",
    "invalidate_role_cache",
    "load_all_role_definitions",
    "load_role_definition",
]

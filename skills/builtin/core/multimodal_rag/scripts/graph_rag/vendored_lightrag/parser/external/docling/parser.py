"""Docling engine adapter (implements ExternalParserBase hooks)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from graph_rag.vendored_lightrag.constants import DOCLING_RAW_DIR_SUFFIX, PARSER_ENGINE_DOCLING
from graph_rag.vendored_lightrag.parser.external._base import ExternalParserBase

if TYPE_CHECKING:
    from graph_rag.vendored_lightrag.sidecar.ir import IRDoc


class DoclingParser(ExternalParserBase):
    engine_name = PARSER_ENGINE_DOCLING
    raw_dir_suffix = DOCLING_RAW_DIR_SUFFIX
    force_reparse_env = "LIGHTRAG_FORCE_REPARSE_DOCLING"

    def is_bundle_valid(self, raw_dir: Path, source_path: Path) -> bool:
        from graph_rag.vendored_lightrag.parser.external.docling import is_bundle_valid

        return is_bundle_valid(raw_dir, source_path)

    async def download_into(self, raw_dir: Path, source_path: Path, *, upload_name: str) -> None:
        from graph_rag.vendored_lightrag.parser.external.docling import DoclingRawClient

        # Map the canonical ``upload_name`` onto docling-serve's multipart
        # filename so the bundle's main JSON is named ``<canonical_stem>.json``
        # (the IR builder locates it via that canonical stem).
        await DoclingRawClient().download_into(raw_dir, source_path, upload_filename=upload_name)

    def build_ir(self, raw_dir: Path, document_name: str) -> "IRDoc":
        from graph_rag.vendored_lightrag.parser.external.docling import DoclingIRBuilder

        return DoclingIRBuilder().normalize_from_workdir(raw_dir, document_name=document_name)

    def validate_ir(self, ir: "IRDoc", *, file_path: str, raw_dir: Path) -> None:
        if not ir.blocks:
            raise ValueError(
                f"Docling IR builder produced zero blocks for {file_path} (raw_dir={raw_dir})"
            )

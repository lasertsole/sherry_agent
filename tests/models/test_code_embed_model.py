"""Unit tests for the code-specific ONNX embedder (no ONNX runtime needed)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeEmbedderProfile
from models.embed_model import code as code_pkg
from models.embed_model.code import core as code_core

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

PROFILE = CodeEmbedderProfile(
    name="fake-code",
    repo_id="example/fake-code",
    revision="rev",
    onnx_filename="onnx/model.onnx",
    tokenizer_files=("tokenizer.json",),
    dim=3,
    pooling="mean",
    max_length=8,
    license="Apache-2.0",
    natural_languages=("en",),
    size_bytes=3,
)


def test_availability_checks_size_and_tokenizer(tmp_path: Path, monkeypatch) -> None:
    weight_dir = tmp_path / "weights"
    tokenizer_dir = tmp_path / "tokenizer"
    tokenizer_dir.mkdir()
    (tokenizer_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    onnx = weight_dir / "onnx" / "model.onnx"
    onnx.parent.mkdir(parents=True)
    onnx.write_bytes(b"abc")
    monkeypatch.setattr(code_core, "_WEIGHT_DIR", weight_dir)
    monkeypatch.setattr(code_core, "_TOKENIZER_DIR", tokenizer_dir)
    monkeypatch.setattr(code_core, "_ENABLED_MARKER", weight_dir / ".enabled")

    assert code_pkg.code_embedder_available(PROFILE) is True
    assert code_pkg.code_embedder_enabled(PROFILE) is False
    code_pkg.enable_code_embedder(PROFILE)
    assert code_pkg.code_embedder_enabled(PROFILE) is True
    code_pkg.disable_code_embedder()
    assert code_pkg.code_embedder_enabled(PROFILE) is False
    onnx.write_bytes(b"ab")
    assert code_pkg.code_embedder_available(PROFILE) is False
    onnx.write_bytes(b"abc")
    (tokenizer_dir / "tokenizer.json").unlink()
    assert code_pkg.code_embedder_available(PROFILE) is False


def test_unknown_profile_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.setitem(CODE_INTEL_SEMANTIC, "code_intel_semantic_model", "nope")
    with pytest.raises(RuntimeError, match="unknown code embedder profile"):
        code_core.build_code_embed_model()


class _Tokenizer:
    def __call__(self, texts: list[str], **kwargs: object) -> dict[str, np.ndarray]:
        return {
            "input_ids": np.zeros((len(texts), 2), dtype=np.int64),
            "attention_mask": np.array([[1, 1], [1, 0]][: len(texts)], dtype=np.int64),
        }


class _Session:
    def run(self, output_names: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        hidden = np.array(
            [
                [[1.0, 0.0, 0.0], [3.0, 0.0, 0.0]],
                [[0.0, 2.0, 0.0], [9.0, 9.0, 9.0]],
            ]
        )
        return [hidden[: len(feeds["input_ids"])]]


def test_mean_pooling_and_l2_without_loading_onnx() -> None:
    embedder = code_core.JinaCodeEmbedder(PROFILE)
    embedder._session = _Session()
    embedder._tokenizer = _Tokenizer()
    embedder._input_names = {"input_ids", "attention_mask"}

    assert embedder.name == "fake-code"
    assert embedder.dim == 3
    assert embedder.embed_documents(["short", "padded"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert embedder.embed_query("short") == [1.0, 0.0, 0.0]
    embedder.close()
    assert embedder._session is None

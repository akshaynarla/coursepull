from __future__ import annotations

import subprocess
import sys

import pytest

from scraper.llm import parse_model_specs


def test_cli_entry_point_runs():
    out = subprocess.run([sys.executable, "-m", "scraper", "--help"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "usage: python -m scraper" in out.stdout and "--plan-only" in out.stdout


def test_model_specs():
    models = parse_model_specs(["all=hf:deepseek-ai/DeepSeek-V4.1-Flash-1017", "scout=ollama:qwen3.5:4b"])
    assert models["tutor"] == {"endpoint": "hf", "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017"}
    assert models["scout"] == {"endpoint": "ollama", "model": "qwen3.5:4b"}  # model names may contain ':'


@pytest.mark.parametrize("spec", ["scout", "scout=nowhere:model", "boss=hf:model", "all=hf:"])
def test_bad_model_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        parse_model_specs([spec])

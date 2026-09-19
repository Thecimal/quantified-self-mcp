from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from .models import Dimension

DEFAULT_PATH = Path(__file__).with_name("registry.yaml")


class DimensionPolicy(BaseModel):
    on_not_assessed: Literal["cap", "tolerate"]


class AnalysisSpec(BaseModel):
    name: str
    dimensions: dict[Dimension, DimensionPolicy]
    thresholds: dict[str, Any] = {}


def load_registry(path: Path | str = DEFAULT_PATH) -> dict[str, AnalysisSpec]:
    raw = yaml.safe_load(Path(path).read_text())
    return {name: AnalysisSpec(name=name, **body) for name, body in raw.items()}

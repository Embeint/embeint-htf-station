from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

StageOutputValue = str | int


@dataclass(frozen=True)
class StageOutput:
    stage_name: str
    name: str
    value: StageOutputValue


class StageContext:
    def __init__(self, dut_id: str, run_id: str | None = None) -> None:
        self.dut_id = dut_id
        self.run_id = run_id
        self._outputs: dict[str, StageOutput] = {}

    @property
    def outputs(self) -> Mapping[str, StageOutput]:
        return MappingProxyType(self._outputs)

    @property
    def output_values(self) -> Mapping[str, StageOutputValue]:
        return MappingProxyType({key: output.value for key, output in self._outputs.items()})

    def set_output(self, stage_name: str, name: str, value: StageOutputValue) -> None:
        output_name = _output_name(name)
        self._outputs[output_name] = StageOutput(
            stage_name=stage_name,
            name=output_name,
            value=value,
        )

    def get_output(self, name: str) -> StageOutput | None:
        output_name = _output_name(name)
        if output_name in self._outputs:
            return self._outputs[output_name]

        lowered = output_name.lower()
        for key, output in self._outputs.items():
            if key.lower() == lowered:
                return output
        return None

    def get_output_value(self, name: str) -> StageOutputValue | None:
        output = self.get_output(name)
        return output.value if output is not None else None


def _output_name(name: str) -> str:
    output_name = name.strip()
    if not output_name:
        raise ValueError("stage output name cannot be empty")
    return output_name


@dataclass(frozen=True)
class StageResult:
    name: str
    outcome: str
    started_at: datetime
    finished_at: datetime


class StageLogger(Protocol):
    async def log(self, level: str, msg: str) -> None: ...


class Stage(Protocol):
    async def run(self, logger: StageLogger, context: StageContext) -> StageResult: ...

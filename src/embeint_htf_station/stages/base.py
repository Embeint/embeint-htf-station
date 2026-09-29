from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import re
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

StageOutputValue = str | int
CAPTURE_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


@dataclass(frozen=True)
class StageObservation:
    id: str
    key: str
    value: str
    stage_index: int
    stage_name: str
    sequence: int
    observed_at: datetime
    verified: bool | None = None
    subtest: str | None = None


@dataclass(frozen=True)
class StageOutput:
    stage_name: str
    name: str
    value: StageOutputValue
    verified: bool | None = None
    subtest: str | None = None


class StageContext:
    def __init__(self, dut_id: str, run_id: str | None = None, secrets: Mapping[str, str] | None = None) -> None:
        self.dut_id = dut_id
        self.run_id = run_id
        self._secrets = MappingProxyType(dict(secrets or {}))
        self._outputs: dict[str, StageOutput] = {}
        self._reservations: dict[str, str] = {}
        self._reserved_values: dict[str, str] = {}
        self.prerequisites_passed = True
        self._stage_index = 0
        self._stage_name = ""
        self._stage_outputs: dict[str, StageOutput] = {}
        self._captures: list[StageObservation] = []
        self._capture_total = 0
        self._capture_issues: list[str] = []

    def begin_stage(self, index: int, name: str) -> None:
        self._stage_index = index
        self._stage_name = name
        self._stage_outputs = {}
        self._captures = []
        self._capture_issues = []

    def capture(
        self, key: str, value: StageOutputValue, *, verified: bool | None = None, subtest: str | None = None,
    ) -> bool:
        """Attach a scalar to this stage's DUT report. Verification defaults to the stage outcome."""
        def issue(message: str) -> bool:
            if message not in self._capture_issues and len(self._capture_issues) < 128:
                self._capture_issues.append(message)
            return False

        if not isinstance(key, str) or len(key) > 128 or CAPTURE_KEY.fullmatch(key) is None:
            return issue("capture key must be a lowercase dotted field name, at most 128 characters")
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            return issue(f"capture '{key}' requires a non-empty string or integer")
        text = str(value).strip()
        if len(text) > 512:
            return issue(f"capture '{key}' exceeds 512 characters")
        if subtest is not None and (not isinstance(subtest, str) or not subtest.strip() or len(subtest) > 128):
            return issue("capture subtest must contain 1 to 128 characters")
        if verified is not None and not isinstance(verified, bool):
            return issue(f"capture '{key}' verification must be a boolean")
        if any((item.key, item.value, item.verified, item.subtest) == (key, text, verified, subtest) for item in self._captures):
            return True
        if len(self._captures) >= 64 or self._capture_total >= 1024:
            return issue("capture limit reached: 64 observations per stage, 1024 per run")
        self._capture_total += 1
        self._captures.append(StageObservation(
            id=str(uuid4()), key=key, value=text, stage_index=self._stage_index,
            stage_name=self._stage_name, sequence=sum(item.key == key for item in self._captures),
            observed_at=datetime.now(UTC), verified=verified, subtest=subtest,
        ))
        return True

    def finish_captures(
        self, outcome: str, mappings: Mapping[str, str],
    ) -> tuple[tuple[StageObservation, ...], tuple[str, ...]]:
        issues: list[str] = []
        for key, source in mappings.items():
            output = self._stage_outputs.get(source)
            if output is None:
                issues.append(f"Capture '{key}': output '{source}' was not produced by this stage")
                continue
            self.capture(key, output.value, verified=output.verified, subtest=output.subtest)
        conflicting = {item.key for item in self._captures
                       if len({other.value for other in self._captures if other.key == item.key}) > 1}
        issues.extend(f"Capture '{key}': conflicting values; observations are unverified" for key in sorted(conflicting))
        return tuple(replace(item, verified=False if item.key in conflicting else (
            item.verified if item.verified is not None else outcome == "passed"
        )) for item in self._captures), tuple(dict.fromkeys([*self._capture_issues, *issues]))

    @property
    def secrets(self) -> Mapping[str, str]:
        """Server-managed values for this station, held only in memory."""
        return self._secrets

    def require_secret(self, name: str) -> str:
        """Return a secret or fail without including its value in the error."""
        try:
            return self._secrets[name]
        except KeyError:
            raise ValueError(f"station secret {name!r} is not configured") from None

    @property
    def reservations(self) -> Mapping[str, str]:
        return MappingProxyType(self._reservations)

    @property
    def reserved_values(self) -> Mapping[str, str]:
        return MappingProxyType(self._reserved_values)

    def set_reservation(self, stage_name: str, name: str, reservation_id: str, value: str) -> None:
        if name in self._reservations and (
            self._reservations[name] != reservation_id or self._reserved_values[name] != value
        ):
            raise ValueError(f"reservation for {name} changed within this run")
        self._reservations[name] = reservation_id
        self._reserved_values[name] = value
        self.set_output(stage_name, f"reservation.{name}", reservation_id)

    @property
    def outputs(self) -> Mapping[str, StageOutput]:
        return MappingProxyType(self._outputs)

    @property
    def output_values(self) -> Mapping[str, StageOutputValue]:
        return MappingProxyType({key: output.value for key, output in self._outputs.items()})

    def set_output(
        self, stage_name: str, name: str, value: StageOutputValue,
        *, verified: bool | None = None, subtest: str | None = None,
    ) -> None:
        output_name = _output_name(name)
        self._outputs[output_name] = StageOutput(
            stage_name=stage_name,
            name=output_name,
            value=value,
            verified=verified,
            subtest=subtest,
        )
        if stage_name == self._stage_name:
            self._stage_outputs[output_name] = self._outputs[output_name]

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
    observations: tuple[StageObservation, ...] = ()
    capture_issues: tuple[str, ...] = ()


class StageLogger(Protocol):
    async def log(self, level: str, msg: str) -> None: ...


class Stage(Protocol):
    async def run(self, logger: StageLogger, context: StageContext) -> StageResult: ...

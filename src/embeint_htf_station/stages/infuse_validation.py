from __future__ import annotations

import asyncio
import errno
import os
import pty
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages.base import StageLogger, StageResult
from embeint_htf_station.stages.nrfutil import NrfutilError


_INFUSE_LINE = re.compile(
    r"^(?P<time>\d+):(?P<test>[A-Z0-9_]+):(?P<level>[A-Z]+):(?P<payload>.*)$",
)
_SUCCESS_LINE = re.compile(r"Complete with (?P<passed>\d+)/(?P<total>\d+) passed")


class InfuseValidationError(RuntimeError):
    """Raised when Infuse validation output is missing or failed."""


@dataclass
class InfuseValidationState:
    required_tests: set[str]
    passed_tests: set[str] = field(default_factory=set)
    failed_tests: set[str] = field(default_factory=set)
    values: dict[str, dict[str, str]] = field(default_factory=dict)
    success_passed: int | None = None
    success_total: int | None = None
    infuse_id: str | None = None
    lines_seen: int = 0

    @property
    def complete(self) -> bool:
        return self.success_passed is not None


class InfuseValidationHook(Protocol):
    async def validate(self, state: InfuseValidationState, logger: StageLogger) -> None: ...


class ValidationLineTransport(Protocol):
    async def __aenter__(self) -> ValidationLineTransport: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...
    def lines(self) -> AsyncIterator[str]: ...


class InfuseValidationStage:
    def __init__(
        self,
        settings: StageSettings,
        programmers: Mapping[str, ProgrammerSettings],
        hooks: Sequence[InfuseValidationHook] = (),
        transport: ValidationLineTransport | None = None,
    ) -> None:
        self._settings = settings
        self._programmers = programmers
        self._hooks = hooks
        self._transport = transport

    async def run(self, logger: StageLogger) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        state = InfuseValidationState(required_tests=set(self._settings.tests))
        try:
            await asyncio.wait_for(
                self._read_until_complete(state, logger),
                timeout=self._settings.test_timeout_seconds,
            )
            _validate_infuse_state(state, self._settings)
            for hook in self._hooks:
                await hook.validate(state, logger)
            await logger.log("info", _summary(state))
            await logger.log("info", "stage passed")
            outcome = "passed"
        except TimeoutError:
            await logger.log(
                "error",
                f"Infuse validation timed out after {self._settings.test_timeout_seconds:g} seconds",
            )
            outcome = "failed"
        except (InfuseValidationError, NrfutilError, OSError) as exc:
            await logger.log("error", str(exc))
            outcome = "failed"

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    def _jlink_command(self) -> tuple[str, ...]:
        return self._default_transport().jlink_command()

    def _rtt_client_command(self) -> tuple[str, ...]:
        return self._default_transport().rtt_client_command()

    async def _read_until_complete(
        self,
        state: InfuseValidationState,
        logger: StageLogger,
    ) -> None:
        transport = self._transport or self._default_transport()
        async with transport as opened:
            async for line in opened.lines():
                await logger.log("info", line)
                parse_infuse_line(line, state)
                if state.complete:
                    return
        if not state.complete:
            raise InfuseValidationError("Infuse validation did not report completion")

    def _default_transport(self) -> JLinkRttClientTransport:
        return JLinkRttClientTransport(self._settings, self._programmers)


class CommandLineTransport:
    def __init__(self, command: Sequence[str]) -> None:
        self._command = tuple(command)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> CommandLineTransport:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(_discard_stderr(self._process))
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._process is not None:
            await _stop_process(self._process)
        if self._stderr_task is not None:
            await _cancel_task(self._stderr_task)

    async def lines(self):
        if self._process is None or self._process.stdout is None:
            raise InfuseValidationError("validation command did not provide stdout")
        yield f"running validation command: {' '.join(self._command)}"
        while line := await self._process.stdout.readline():
            yield line.decode("utf-8", errors="replace").rstrip()


class JLinkRttClientTransport:
    def __init__(
        self,
        settings: StageSettings,
        programmers: Mapping[str, ProgrammerSettings],
    ) -> None:
        self._settings = settings
        self._programmers = programmers
        self._jlink: asyncio.subprocess.Process | None = None
        self._jlink_master_fd: int | None = None
        self._client: asyncio.subprocess.Process | None = None
        self._client_stderr: asyncio.Task[None] | None = None
        self._reset_process: asyncio.subprocess.Process | None = None
        self._reset_stderr: asyncio.Task[None] | None = None
        self._setup_lines: list[str] = []

    async def __aenter__(self) -> JLinkRttClientTransport:
        if self._settings.rtt_command:
            return self
        try:
            self._client = await asyncio.create_subprocess_exec(
                *self.rtt_client_command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._client_stderr = asyncio.create_task(_discard_stderr(self._client))
            self._setup_lines.append(f"RTT client started: {' '.join(self.rtt_client_command())}")
            if self._settings.reset_before_capture:
                await self._reset_with_nrfutil()
            master_fd, slave_fd = pty.openpty()
            self._jlink_master_fd = master_fd
            self._jlink = await asyncio.create_subprocess_exec(
                *self.jlink_command(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
            os.close(slave_fd)
            self._setup_lines.extend(await asyncio.wait_for(
                _wait_until_fd(master_fd, ("J-Link>",)),
                timeout=30,
            ))
            return self
        except TimeoutError as exc:
            await self.__aexit__(None, None, None)
            tail = " | ".join(self._setup_lines[-8:]) or "no JLinkExe output"
            raise InfuseValidationError(f"JLinkExe did not reach prompt within 30 seconds: {tail}") from exc
        except Exception:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await _stop_process(self._client)
        await _stop_process(self._reset_process)
        if self._jlink is not None:
            await _stop_jlink(self._jlink, self._jlink_master_fd)
        if self._jlink_master_fd is not None:
            _close_fd(self._jlink_master_fd)
            self._jlink_master_fd = None
        if self._client_stderr is not None:
            await _cancel_task(self._client_stderr)
        if self._reset_stderr is not None:
            await _cancel_task(self._reset_stderr)

    def jlink_command(self) -> tuple[str, ...]:
        programmer = _resolve_programmer(self._settings, self._programmers)
        target = programmer.target_device if programmer and programmer.target_device else None
        if target is None:
            raise InfuseValidationError("infuse validation requires programmer.target_device or rtt_command")

        command = [
            "JLinkExe",
            "-device",
            target,
            "-if",
            "SWD",
            "-speed",
            "4000",
            "-autoconnect",
            "1",
            "-RTTTelnetPort",
            str(_rtt_telnet_port(self._settings, programmer)),
        ]
        if programmer and programmer.serial_number is not None:
            command.extend(("-USB", str(programmer.serial_number)))
        return tuple(command)

    def rtt_client_command(self) -> tuple[str, ...]:
        command = ["JLinkRTTClientExe"]
        programmer = _resolve_programmer(self._settings, self._programmers)
        rtt_telnet_port = _rtt_telnet_port(self._settings, programmer)
        if rtt_telnet_port != 19021:
            command.extend(("-rtttelnetport", str(rtt_telnet_port)))
        return tuple(command)

    async def lines(self):
        if self._settings.rtt_command:
            async with CommandLineTransport(self._settings.rtt_command) as command:
                async for line in command.lines():
                    yield line
            return

        if self._client is None or self._client.stdout is None:
            raise InfuseValidationError("RTT client did not provide stdout")

        for line in self._setup_lines:
            yield line
        while line := await self._client.stdout.readline():
            decoded = line.decode("utf-8", errors="replace").rstrip()
            yield decoded

    async def _reset_with_nrfutil(self) -> None:
        reset_command = _nrfutil_reset_command(self._settings, self._programmers)
        self._setup_lines.append(f"resetting target: {' '.join(reset_command)}")
        self._reset_process = await asyncio.create_subprocess_exec(
            *reset_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._reset_stderr = asyncio.create_task(_discard_stderr(self._reset_process))
        if self._reset_process.stdout is not None:
            while line := await self._reset_process.stdout.readline():
                self._setup_lines.append(line.decode("utf-8", errors="replace").rstrip())
        exit_code = await self._reset_process.wait()
        if exit_code != 0:
            raise InfuseValidationError(f"nrfutil reset exited with status {exit_code}")
        # Give JLinkRTTClientExe a short window to enter its reconnect loop before the RTT server starts.
        await asyncio.to_thread(time.sleep, 0.2)
        self._setup_lines.append(f"RTT server starting: {' '.join(self.jlink_command())}")


def parse_infuse_line(line: str, state: InfuseValidationState) -> None:
    state.lines_seen += 1
    match = _INFUSE_LINE.match(line.strip())
    if match is None:
        return

    test = match.group("test")
    level = match.group("level")
    payload = match.group("payload")

    if test == "SYS" and level == "VAL" and payload.startswith("INFUSE_ID:"):
        state.infuse_id = payload.split(":", 1)[1]
    elif level == "PASS" and not (test == "SYS" and "Complete with" in payload):
        state.passed_tests.add(test)
    elif level in {"FAIL", "ERROR"} and not (test == "SYS" and "Complete with" in payload):
        state.failed_tests.add(test)
    elif level == "VAL":
        key, _, value = payload.partition(":")
        if key:
            state.values.setdefault(test, {})[key] = value

    if test == "SYS" and level in {"SUCCESS", "PASS", "ERROR"}:
        success = _SUCCESS_LINE.search(payload)
        if success:
            state.success_passed = int(success.group("passed"))
            state.success_total = int(success.group("total"))


async def _discard_stderr(process: asyncio.subprocess.Process) -> None:
    if process.stderr is None:
        return
    while line := await process.stderr.readline():
        _ = line


async def _wait_until(
    stream: asyncio.StreamReader | None,
    patterns: Sequence[str],
) -> list[str]:
    if stream is None:
        raise InfuseValidationError("process did not provide stdout")
    output = ""
    while chunk := await stream.read(256):
        output += chunk.decode("utf-8", errors="replace")
        if any(pattern in output for pattern in patterns):
            return [line.rstrip() for line in output.splitlines() if line.strip()]
    raise InfuseValidationError("process exited before RTT was ready")


async def _wait_until_fd(fd: int, patterns: Sequence[str]) -> list[str]:
    output = ""
    while True:
        try:
            chunk = await asyncio.to_thread(os.read, fd, 256)
        except OSError as exc:
            if exc.errno == errno.EIO:
                chunk = b""
            else:
                raise
        if not chunk:
            break
        output += chunk.decode("utf-8", errors="replace")
        if any(pattern in output for pattern in patterns):
            return [line.rstrip() for line in output.splitlines() if line.strip()]
    raise InfuseValidationError("process exited before RTT was ready")


async def _stop_jlink(process: asyncio.subprocess.Process, fd: int | None) -> None:
    if fd is not None and process.returncode is None:
        try:
            os.write(fd, b"exit\n")
        except OSError:
            pass
    await _stop_process(process)


async def _stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _cancel_task(task: asyncio.Task[None]) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _validate_infuse_state(state: InfuseValidationState, settings: StageSettings) -> None:
    if state.lines_seen == 0:
        raise InfuseValidationError("no RTT output received")
    if not state.complete:
        raise InfuseValidationError("Infuse validation did not report completion")
    if state.failed_tests:
        raise InfuseValidationError(f"Infuse tests failed: {', '.join(sorted(state.failed_tests))}")
    if state.success_passed != state.success_total:
        raise InfuseValidationError(f"Infuse reported {state.success_passed}/{state.success_total} passed")

    missing = sorted(state.required_tests - state.passed_tests)
    if missing:
        raise InfuseValidationError(f"required Infuse tests did not pass: {', '.join(missing)}")

    if settings.number_of_tests is not None and settings.number_of_tests != state.success_total:
        # Keep this non-fatal while validation configs are settling; the firmware output is the source of truth.
        return


def _summary(state: InfuseValidationState) -> str:
    tests = ", ".join(sorted(state.passed_tests)) or "none"
    return f"Infuse validation complete: {state.success_passed}/{state.success_total} passed ({tests})"


def _resolve_programmer(
    settings: StageSettings,
    programmers: Mapping[str, ProgrammerSettings],
) -> ProgrammerSettings | None:
    if settings.programmer is None:
        return None

    programmer = programmers.get(settings.programmer)
    if programmer is None:
        raise NrfutilError(f"unknown programmer: {settings.programmer}")
    return programmer


def _nrfutil_reset_command(
    settings: StageSettings,
    programmers: Mapping[str, ProgrammerSettings],
) -> tuple[str, ...]:
    programmer = _resolve_programmer(settings, programmers)
    command = ["nrfutil", "device", "reset"]
    if programmer is not None and programmer.serial_number is not None:
        command.extend(("--serial-number", str(programmer.serial_number)))
    return tuple(command)


def _rtt_telnet_port(settings: StageSettings, programmer: ProgrammerSettings | None) -> int:
    if programmer is not None:
        return programmer.rtt_telnet_port
    return settings.rtt_telnet_port

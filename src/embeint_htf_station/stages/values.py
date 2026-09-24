from __future__ import annotations

import re
from typing import Any

from embeint_htf_station.stages.base import StageContext

_REFERENCE = re.compile(r"\$\{([^{}]+)\}")


def resolve_value(value: Any, context: StageContext) -> Any:
    """Resolve runtime references once, without eval or implicit secret lookup."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match[1]
            if key == "dut_id":
                return context.dut_id
            if key == "run_id":
                if context.run_id is None:
                    raise ValueError("run_id is unavailable")
                return context.run_id
            result = context.get_output_value(key.removeprefix("context."))
            if result is None:
                raise ValueError(f"Stage value {key} is unavailable")
            return str(result)
        return _REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {key: resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(resolve_value(item, context) for item in value)
    return value

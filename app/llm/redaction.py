from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

REDACTED = "[REDACTED]"
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class SecretRedactionFilter(logging.Filter):
    """Redact the configured LLM key and bearer credentials from log records."""

    def __init__(self, llm_api_key: SecretStr | str) -> None:
        super().__init__()
        self._secret = (
            llm_api_key.get_secret_value()
            if isinstance(llm_api_key, SecretStr)
            else llm_api_key
        )

    def redact_text(self, value: str) -> str:
        redacted = value.replace(self._secret, REDACTED) if self._secret else value
        return BEARER_PATTERN.sub(f"Bearer {REDACTED}", redacted)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact_value(record.msg)
        record.args = self._redact_value(record.args)
        for key, value in list(record.__dict__.items()):
            if key not in {"msg", "args", "exc_info"}:
                record.__dict__[key] = self._redact_value(value)
        if record.exc_info:
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self.redact_text(rendered)
        if record.stack_info:
            record.stack_info = self.redact_text(record.stack_info)
        return True


def install_redaction_filter(
    llm_api_key: SecretStr | str,
    logger: logging.Logger | None = None,
) -> SecretRedactionFilter:
    target = logger or logging.getLogger()
    redaction_filter = SecretRedactionFilter(llm_api_key)
    target.addFilter(redaction_filter)
    for handler in target.handlers:
        handler.addFilter(redaction_filter)
    return redaction_filter

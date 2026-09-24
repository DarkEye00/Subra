"""AI provider interface.

The rest of the code only needs "give me JSON matching this schema", so
providers are swappable (the platform will add image and video providers
later). ``AnthropicProvider`` is the only implementation for now.
"""

import json
import time
from dataclasses import dataclass


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class JsonResult:
    data: dict
    usage: Usage
    model: str
    duration_ms: int
    request_id: str | None = None


class ProviderError(Exception):
    """A request that produced no usable result.

    ``retryable`` is True for outages (rate limits, 5xx, network) where the same
    request may work later; ``usage`` is set when tokens were still billed.
    """

    def __init__(self, message, retryable=False, usage=None):
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage


class AnthropicProvider:
    def __init__(self, client=None, max_retries=4):
        if client is None:
            import anthropic

            # The SDK retries 408/409/429/5xx and connection errors with backoff.
            client = anthropic.Anthropic(max_retries=max_retries)
        self.client = client

    def complete_json(self, *, model, system, user, schema, effort, max_tokens=64000):
        import anthropic

        started = time.monotonic()
        try:
            # Streaming avoids HTTP timeouts on long plans; we only need the final message.
            with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                message = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise ProviderError(f"rate limited: {e}", retryable=True)
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}", retryable=e.status_code >= 500)
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"connection error: {e}", retryable=True)

        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        if message.stop_reason in ("refusal", "max_tokens"):
            raise ProviderError(f"stopped with {message.stop_reason}", retryable=False, usage=usage)
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"invalid JSON from model: {e}", usage=usage)
        return JsonResult(
            data=data,
            usage=usage,
            model=message.model,
            duration_ms=int((time.monotonic() - started) * 1000),
            request_id=getattr(message, "_request_id", None),
        )

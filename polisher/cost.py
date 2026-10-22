"""Token pricing (USD per million tokens). Update when Anthropic's prices change.

Cache writes use the 5-minute TTL rate (1.25x input); cache reads are ~0.1x
input (0.05x on Opus 5.5). Thinking tokens are billed as output tokens and are
already included in ``output_tokens``.
"""

PRICING = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    # Current generation (checked 22 Oct 2026). Haiku 5.5 rates apply to prompts up to 100K tokens.
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}


def cost_usd(model, usage, pricing=None):
    rates = (pricing or PRICING)[model]
    return (
        usage.input_tokens * rates["input"]
        + usage.output_tokens * rates["output"]
        + usage.cache_read_tokens * rates["cache_read"]
        + usage.cache_write_tokens * rates["cache_write"]
    ) / 1_000_000

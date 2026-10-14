"""Bounded, offline prompt analysis. OAuth credentials are not model-context tokens.

This module neither authenticates nor executes actions. It returns advisory metadata;
only the AI host/tokenizer knows actual context use, loaded tools and model limits.
Prompt text, hashes and credentials are never included in the result or persisted.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_PROMPT_BYTES = 32_768
MAX_TOOL_BYTES = 131_072
MAX_TOOLS = 64
MAX_CONTEXT = 2_000_000


def _integer(value: int, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_CONTEXT:
        raise ValueError(f"{name} is outside the supported integer range")
    return value


def _size(text: str, ceiling: int, name: str) -> int:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be text")
    # Bound before encoding as well as after: a Python caller can pass huge strings.
    if len(text) > ceiling:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")
    try:
        size = len(text.encode('utf-8'))
    except UnicodeEncodeError:
        raise ValueError(f"{name} must contain valid Unicode") from None
    if size > ceiling:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")
    return size


def _bounded_json(value: Any) -> None:
    stack = [(value, 0)]
    nodes = text_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 4096 or depth > 16:
            raise ValueError('tool definition is structurally too large')
        if isinstance(item, str):
            text_bytes += _size(item, MAX_TOOL_BYTES, 'tool definition text')
            if text_bytes > MAX_TOOL_BYTES:
                raise ValueError('tool definition text exceeds its byte limit')
        elif isinstance(item, dict):
            if len(item) > 4096 or any(not isinstance(k, str) for k in item):
                raise ValueError('tool definition keys must be bounded strings')
            stack.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, list):
            if len(item) > 4096:
                raise ValueError('tool definition array is too large')
            stack.extend((v, depth + 1) for v in item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ValueError('tool definition contains unsupported JSON values')
        elif isinstance(item, int) and item.bit_length() > 64:
            raise ValueError('tool definition integer is too large')


def analyze_prompt(prompt: str, *, context_window: int = 8192, reserved_output: int = 1024,
                   existing_context: int = 0, tool_definitions: list[dict[str, Any]] | None = None) -> dict:
    """Return deterministic advice without echoing input or performing any I/O.

    UTF-8 bytes / 4 rounded up is a rough estimate, not an exact token count,
    upper bound or billing measurement. Existing-context counts are host supplied.
    All supplied tools are counted; discovery/loading behaviour is client-specific.
    """
    size = _size(prompt, MAX_PROMPT_BYTES, 'prompt')
    if not prompt.strip():
        raise ValueError('prompt must not be empty')
    window = _integer(context_window, 'context_window', 1)
    reserve = _integer(reserved_output, 'reserved_output')
    existing = _integer(existing_context, 'existing_context')
    if reserve + existing >= window:
        raise ValueError('existing context and reserved output exhaust the context window')
    tools = [] if tool_definitions is None else tool_definitions
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ValueError('tool definitions must be a bounded list')
    tool_bytes = 0
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError('each tool definition must be an object')
        _bounded_json(tool)
        try:
            encoded = json.dumps(tool, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ValueError('tool definitions must be finite JSON objects') from None
        tool_bytes += _size(encoded, MAX_TOOL_BYTES, 'tool definition')
        if tool_bytes > MAX_TOOL_BYTES:
            raise ValueError('aggregate tool definitions exceed their UTF-8 byte limit')
    words = set(re.findall(r'\b[a-z]+\b', prompt.lower()))
    categories = {
        'research': {'research', 'find', 'sources', 'evidence', 'verify', 'fact'},
        'build': {'build', 'create', 'implement', 'code', 'design'},
        'diagnose': {'fix', 'debug', 'error', 'failure', 'broken', 'blocker', 'blockers'},
        'execute': {'deploy', 'delete', 'execute', 'publish', 'send', 'transfer', 'merge'},
    }
    intents = [name for name, markers in categories.items() if words & markers] or ['clarify']
    risks = []
    if words & {'password', 'secret', 'token', 'credential', 'credentials', 'apikey'}:
        risks.append('possible_credentials')
    if words & {'medical', 'medicine', 'diagnosis', 'legal', 'law', 'financial', 'investment'}:
        risks.append('high_stakes_requires_authoritative_evidence')
    if 'execute' in intents:
        risks.append('external_action_requires_separate_authorization')
    clarification = []
    if intents == ['clarify']:
        clarification.append('What outcome and evidence would establish success?')
    if 'execute' in intents:
        clarification.append('Which exact target and operations are authorized?')
    prompt_estimate, tools_estimate = (size + 3) // 4, (tool_bytes + 3) // 4
    available = window - reserve - existing
    fits = prompt_estimate + tools_estimate <= available
    return {
        'schema': 'li.token-machine/1', 'analysis_kind': 'offline_lexical_heuristic',
        'intents': intents, 'risk_flags': risks, 'clarification_questions': clarification,
        'recommended_steps': ['clarify_scope', 'gather_evidence', 'verify_evidence', 'prepare_reviewable_plan'],
        'context': {'prompt_utf8_bytes': size, 'tool_definition_utf8_bytes': tool_bytes,
                    'supplied_tool_count': len(tools), 'prompt_tokens_estimate': prompt_estimate,
                    'tool_tokens_estimate': tools_estimate, 'available_input_tokens': available,
                    'estimated_fit': fits, 'recommendation': 'verify_with_host_tokenizer' if fits else 'reduce_supplied_context'},
        'limitations': ['Estimates are not exact counts, safe upper bounds or billing measurements.',
                        'The client controls tool discovery, context loading and model tokenization.',
                        'LI sees only the prompt explicitly submitted to this operation.',
                        'Lexical intent and risk flags may miss cases or produce false positives.'],
        'authority': {'executes_actions': False, 'grants_permissions': False,
                      'handles_oauth_credentials': False, 'network_calls': False, 'known_provider_cost_usd': 0},
    }

"""Cost-aware model router with LiteLLM optional support.

Model spend is allowed only when RADAR_USE_LITELLM=1 and the selected provider
has credentials in the environment. Otherwise the router logs a no-cost fallback.
OpenAI GPT-5.x tiers prefer the native Responses API so reasoning effort,
verbosity, structured JSON, and prompt caching are explicit.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass

from storage import connect, record_llm_cost_event


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "llm_config.example.yaml"

PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
    "cohere": "COHERE_API_KEY",
}


@dataclass
class ModelResult:
    text: str
    model_name: str
    model_tier: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    status: str
    provider: str = ""
    api: str = ""
    reasoning_effort: str | None = None
    reasoning_mode: str | None = None
    verbosity: str | None = None
    operation: str | None = None
    prompt_cache_key: str | None = None
    frontier_escalation_reason: str | None = None
    structured_json: bool = False


def load_llm_config(path: pathlib.Path = CONFIG_PATH) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Cannot parse LLM config {path}: {exc}") from exc


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _cost_usd(prompt_tokens: int, completion_tokens: int, tier_cfg: dict) -> float:
    return (
        (prompt_tokens / 1_000_000.0) * float(tier_cfg.get("input_cost_per_1m", 0.0))
        + (completion_tokens / 1_000_000.0) * float(tier_cfg.get("output_cost_per_1m", 0.0))
    )


def _fallback_response(agent_name: str, prompt: str) -> str:
    return json.dumps(
        {
            "action": "propose_hunter_directive",
            "priority": 55,
            "title": f"{agent_name} fallback recommendation",
            "rationale": "No configured LiteLLM model call was made; fallback agent recommends continued evidence collection and route discovery.",
            "market_key": "fallback_llm_bridge",
            "directive": "continue_low_cost_research",
            "evidence": {"agent": agent_name, "prompt_chars": len(prompt), "mode": "fallback"},
            "proposed_change": "Keep deterministic radar running and configure LiteLLM/local model for richer inference.",
        },
        sort_keys=True,
    )


def _provider_key_env(model_name: str) -> str | None:
    if "/" not in model_name:
        return "OPENAI_API_KEY"
    provider = model_name.split("/", 1)[0].lower()
    if provider in {"ollama", "vllm", "sglang", "local"}:
        return None
    return PROVIDER_KEY_ENV.get(provider)


def _provider_name(model_name: str) -> str:
    if "/" not in model_name:
        return "openai"
    return model_name.split("/", 1)[0].lower()


def _provider_model_name(model_name: str) -> str:
    provider = _provider_name(model_name)
    if provider == "openai" and "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name


def _provider_ready(model_name: str) -> tuple[bool, str]:
    key_name = _provider_key_env(model_name)
    if key_name is None:
        return True, "provider_no_key_required"
    if os.environ.get(key_name):
        return True, f"provider_key_present:{key_name}"
    return False, f"fallback_missing_provider_key:{key_name}"


def _spent_today(agent_name: str | None = None) -> float:
    with connect() as conn:
        if agent_name:
            row = conn.execute(
                """
                select coalesce(sum(estimated_cost_usd), 0) as cost
                from llm_cost_events
                where agent_name = ? and substr(created_at, 1, 10) = date('now')
                """,
                (agent_name,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                select coalesce(sum(estimated_cost_usd), 0) as cost
                from llm_cost_events
                where substr(created_at, 1, 10) = date('now')
                """
            ).fetchone()
    return float(row["cost"] or 0.0)


def _budget_allows_call(agent_name: str, cfg: dict, agent_cfg: dict, tier_cfg: dict, prompt_tokens: int) -> tuple[bool, str]:
    agent_budget = float(agent_cfg.get("daily_budget_usd", cfg.get("daily_budget_usd", 0.0)))
    global_budget = float(cfg.get("daily_budget_usd", 0.0))
    estimated_completion_tokens = int(tier_cfg.get("estimated_completion_tokens", 1000))
    estimated_call_cost = _cost_usd(prompt_tokens, estimated_completion_tokens, tier_cfg)
    agent_spent = _spent_today(agent_name)
    global_spent = _spent_today()

    if agent_budget > 0 and agent_spent + estimated_call_cost > agent_budget:
        return False, f"agent_budget_guard:{agent_spent:.6f}+{estimated_call_cost:.6f}>{agent_budget:.6f}"
    if global_budget > 0 and global_spent + estimated_call_cost > global_budget:
        return False, f"global_budget_guard:{global_spent:.6f}+{estimated_call_cost:.6f}>{global_budget:.6f}"
    return True, "budget_ok"


def complete(
    agent_name: str,
    prompt: str,
    system: str = "",
    tier_override: str | None = None,
    operation: str | None = None,
    frontier_escalation_reason: str | None = None,
    reasoning_effort_override: str | None = None,
    structured_json: bool | None = None,
    max_output_tokens_override: int | None = None,
    timeout_seconds_override: float | None = None,
) -> ModelResult:
    cfg = load_llm_config()
    agent_cfg = cfg.get("agents", {}).get(agent_name, {"tier": "fast"})
    tier_name = tier_override or agent_cfg.get("tier", "fast")
    tier_cfg = cfg.get("tiers", {}).get(tier_name, cfg.get("tiers", {}).get("fast", {}))
    model_name = tier_cfg.get("model", "fallback")
    max_prompt_chars = int(tier_cfg.get("max_prompt_chars", 12000))
    prompt = prompt[:max_prompt_chars]
    prompt_tokens = estimate_tokens(system + prompt)
    provider = _provider_name(model_name)
    api = str(tier_cfg.get("api") or ("responses" if provider == "openai" else "litellm"))
    reasoning_effort = reasoning_effort_override or tier_cfg.get("reasoning_effort")
    reasoning_mode = tier_cfg.get("reasoning_mode")
    verbosity = tier_cfg.get("verbosity")
    prompt_cache_key = tier_cfg.get("prompt_cache_key") or f"radar:{agent_name}:{tier_name}"
    prompt_cache_retention = tier_cfg.get("prompt_cache_retention")
    timeout_seconds = timeout_seconds_override or tier_cfg.get("timeout_seconds") or cfg.get("timeout_seconds")
    if provider == "openai" and _provider_model_name(model_name).startswith("gpt-5.") and prompt_cache_retention == "in_memory":
        prompt_cache_retention = "24h"
    structured_json_enabled = bool(tier_cfg.get("structured_json", False) if structured_json is None else structured_json)
    operation = operation or "llm_completion"

    use_litellm = os.environ.get("RADAR_USE_LITELLM") == "1"
    if cfg.get("require_env_to_call_models", True) and not use_litellm:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            "fallback_no_cost",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
        )
        _log(agent_name, result)
        return result

    ready, provider_status = _provider_ready(model_name)
    if not ready:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            provider_status,
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
        )
        _log(agent_name, result)
        return result

    allowed, budget_status = _budget_allows_call(agent_name, cfg, agent_cfg, tier_cfg, prompt_tokens)
    if not allowed:
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            budget_status,
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
        )
        _log(agent_name, result)
        return result

    try:
        if provider == "openai" and api == "responses":
            text, actual_prompt_tokens, completion_tokens = _complete_openai_responses(
                model_name=_provider_model_name(model_name),
                prompt=prompt,
                system=system,
                reasoning_effort=reasoning_effort,
                reasoning_mode=reasoning_mode,
                verbosity=verbosity,
                structured_json=structured_json_enabled,
                max_output_tokens=int(
                    max_output_tokens_override
                    or tier_cfg.get("max_output_tokens", tier_cfg.get("estimated_completion_tokens", 4000))
                ),
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                timeout_seconds=timeout_seconds,
            )
            prompt_tokens = actual_prompt_tokens or prompt_tokens
        else:
            text, completion_tokens = _complete_litellm(
                model_name=model_name,
                prompt=prompt,
                system=system,
                reasoning_effort=reasoning_effort,
                structured_json=structured_json_enabled,
                temperature=tier_cfg.get("temperature"),
                timeout_seconds=timeout_seconds,
            )
        estimated_cost = _cost_usd(prompt_tokens, completion_tokens, tier_cfg)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            estimated_cost,
            f"model_call:{api}",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
        )
        _log(agent_name, result)
        return result
    except Exception as exc:  # noqa: BLE001
        text = _fallback_response(agent_name, prompt)
        completion_tokens = estimate_tokens(text)
        result = ModelResult(
            text,
            model_name,
            tier_name,
            prompt_tokens,
            completion_tokens,
            0.0,
            f"fallback_error:{exc}",
            provider=provider,
            api=api,
            reasoning_effort=reasoning_effort,
            reasoning_mode=reasoning_mode,
            verbosity=verbosity,
            operation=operation,
            prompt_cache_key=prompt_cache_key,
            frontier_escalation_reason=frontier_escalation_reason,
            structured_json=structured_json_enabled,
        )
        _log(agent_name, result)
        return result


def _complete_openai_responses(
    model_name: str,
    prompt: str,
    system: str,
    reasoning_effort: str | None,
    reasoning_mode: str | None,
    verbosity: str | None,
    structured_json: bool,
    max_output_tokens: int,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    timeout_seconds: float | None,
) -> tuple[str, int | None, int]:
    from openai import OpenAI

    client = OpenAI(timeout=float(timeout_seconds)) if timeout_seconds else OpenAI()
    text_cfg: dict = {}
    if verbosity:
        text_cfg["verbosity"] = verbosity
    if structured_json:
        text_cfg["format"] = {"type": "json_object"}

    kwargs: dict = {
        "model": model_name,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    if system:
        kwargs["instructions"] = system
    reasoning: dict = {}
    if reasoning_effort:
        reasoning["effort"] = reasoning_effort
    if reasoning_mode:
        reasoning["mode"] = reasoning_mode
    if reasoning:
        kwargs["reasoning"] = reasoning
    if text_cfg:
        kwargs["text"] = text_cfg
    if prompt_cache_key:
        kwargs["prompt_cache_key"] = prompt_cache_key
    if prompt_cache_retention:
        kwargs["prompt_cache_retention"] = prompt_cache_retention

    response = client.responses.create(**kwargs)
    text = getattr(response, "output_text", None) or _extract_response_text(response)
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else None
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else estimate_tokens(text)
    return text, input_tokens, output_tokens


def _extract_response_text(response: object) -> str:
    output = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in output:
        content = getattr(item, "content", None) or []
        for block in content:
            for attr in ("text", "value", "content"):
                text = getattr(block, attr, None)
                if isinstance(text, str) and text:
                    parts.append(text)
                    break
    if parts:
        return "\n".join(parts)
    try:
        raw = response.model_dump()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(_walk_text_values(raw))


def _walk_text_values(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        text_type = str(value.get("type") or "")
        text_value = value.get("text")
        if isinstance(text_value, str) and text_value and "reasoning" not in text_type:
            found.append(text_value)
        for key, child in value.items():
            if key in {"usage", "reasoning"}:
                continue
            found.extend(_walk_text_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_text_values(child))
    elif isinstance(value, str) and value.startswith("diff --git "):
        found.append(value)
    return found


def _complete_litellm(
    model_name: str,
    prompt: str,
    system: str,
    reasoning_effort: str | None,
    structured_json: bool,
    temperature: float | None,
    timeout_seconds: float | None,
) -> tuple[str, int]:
    import litellm  # type: ignore

    kwargs: dict = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if temperature is not None:
        kwargs["temperature"] = float(temperature)
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if structured_json:
        kwargs["response_format"] = {"type": "json_object"}
    if timeout_seconds:
        kwargs["timeout"] = float(timeout_seconds)
    response = litellm.completion(**kwargs)
    text = response["choices"][0]["message"]["content"]
    return text, estimate_tokens(text)


def _log(agent_name: str, result: ModelResult) -> None:
    with connect() as conn:
        record_llm_cost_event(
            conn,
            agent_name,
            result.model_tier,
            result.model_name,
            result.prompt_tokens,
            result.completion_tokens,
            result.estimated_cost_usd,
            result.status,
            provider=result.provider,
            api=result.api,
            reasoning_effort=result.reasoning_effort,
            verbosity=result.verbosity,
            operation=result.operation,
            prompt_cache_key=result.prompt_cache_key,
            frontier_escalation_reason=result.frontier_escalation_reason,
            structured_json=result.structured_json,
        )

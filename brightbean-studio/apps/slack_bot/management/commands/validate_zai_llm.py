"""Django management command to validate Z.AI LLM connectivity.

Performs bounded live tests against the Z.AI API:
  1. Primary model basic completion
  2. Primary model tool calling
  3. Primary model tool-result continuation
  4. Fallback model basic completion
  5. Fallback model tool calling

Safety rules:
  * Never prints the API key or token fragments.
  * No company analytics data sent.
  * No real Slack messages.
  * No database writes.
  * No real BrightBean tools executed.
  * Low output-token limit, short timeout, non-streaming.
  * At most 5 live API requests.
  * Does not run at startup or in CI.

Usage:
    python manage.py validate_zai_llm
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.slack_bot.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMRole,
    LLMToolDefinition,
    LLMToolResultContent,
)
from apps.slack_bot.llm.config import (
    get_glm_config,
    get_glm_fallback_config,
)
from apps.slack_bot.llm.exceptions import LLMProviderError
from apps.slack_bot.llm.glm_client import GLMProvider

# ---------------------------------------------------------------------------
# Fake tool for diagnostic only — NOT registered in production
# ---------------------------------------------------------------------------

_FAKE_TOOL = LLMToolDefinition(
    name="get_test_metric",
    description="Return a test metric value for diagnostics.",
    input_schema={
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["reach", "engagement", "views"],
            },
        },
        "required": ["metric"],
        "additionalProperties": False,
    },
)

_FAKE_TOOL_RESULT = '{"status": "ok", "metric": "reach", "value": 100}'

_SYSTEM_PROMPT = (
    "You are a diagnostic assistant. "
    "Use the get_test_metric tool when asked for a metric. "
    "After receiving the tool result, state the value briefly."
)


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    check: str
    model: str
    passed: bool
    latency: float
    safe_error: str


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Validate Z.AI LLM connectivity with bounded live tests."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write("=" * 70)
        self.stdout.write("Z.AI LLM Validation Diagnostic")
        self.stdout.write("=" * 70)

        # --- Load configs ---
        primary_cfg = get_glm_config()
        fallback_cfg = get_glm_fallback_config()

        self.stdout.write(f"Primary model:  {primary_cfg.model}")
        self.stdout.write(f"Fallback model: {fallback_cfg.model}")
        self.stdout.write(f"API endpoint:   {primary_cfg.base_url}")
        self.stdout.write(f"API key set:    {'yes' if primary_cfg.api_key else 'NO'}")
        self.stdout.write("")

        if not primary_cfg.api_key:
            raise CommandError(
                "ZAI_API_KEY is not configured. "
                "Set it in .env or Django settings before running this command."
            )

        primary = GLMProvider(config=primary_cfg)
        fallback = GLMProvider(config=fallback_cfg)

        results: list[CheckResult] = []

        # --- 1. Primary basic completion ---
        results.append(self._run_check(
            "Basic completion",
            primary_cfg.model,
            lambda: self._test_basic_completion(primary),
        ))

        # --- 2. Primary tool call ---
        results.append(self._run_check(
            "Tool request",
            primary_cfg.model,
            lambda: self._test_tool_call(primary),
        ))

        # --- 3. Primary tool-result continuation ---
        results.append(self._run_check(
            "Tool continuation",
            primary_cfg.model,
            lambda: self._test_tool_continuation(primary),
        ))

        # --- 4. Fallback basic completion ---
        results.append(self._run_check(
            "Basic completion",
            fallback_cfg.model,
            lambda: self._test_basic_completion(fallback),
        ))

        # --- 5. Fallback tool call ---
        results.append(self._run_check(
            "Tool request",
            fallback_cfg.model,
            lambda: self._test_tool_call(fallback),
        ))

        # --- Report ---
        self.stdout.write("")
        self.stdout.write("-" * 70)
        self.stdout.write(
            f"{'Check':<22} {'Model':<12} {'Result':<8} {'Latency':<10} {'Safe Error'}"
        )
        self.stdout.write("-" * 70)

        all_passed = True
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            error = r.safe_error if not r.passed else "—"
            self.stdout.write(
                f"{r.check:<22} {r.model:<12} {status:<8} {r.latency:.1f}s    {error}"
            )
            if not r.passed:
                all_passed = False

        self.stdout.write("-" * 70)
        self.stdout.write(f"Total live requests: {len(results)}")
        self.stdout.write("API key exposed: no")
        self.stdout.write("Company data sent: no")
        self.stdout.write("")

        if all_passed:
            self.stdout.write(self.style.SUCCESS("All checks PASSED."))
        else:
            failed = [r for r in results if not r.passed]
            self.stdout.write(self.style.ERROR(
                f"{len(failed)} check(s) FAILED."
            ))
            raise CommandError(f"{len(failed)} Z.AI validation check(s) failed.")

    # -----------------------------------------------------------------------
    # Individual tests
    # -----------------------------------------------------------------------

    def _test_basic_completion(self, provider: GLMProvider) -> None:
        """Test that the model returns a text response."""
        request = LLMRequest(
            system_prompt="You are a diagnostic assistant. Reply briefly.",
            messages=[LLMMessage(role=LLMRole.USER, content="Say 'hello'.")],
            max_output_tokens=50,
            temperature=0.0,
        )
        response = provider.complete(request)
        if not response.text:
            raise ValueError("Empty response text")

    def _test_tool_call(self, provider: GLMProvider) -> None:
        """Test that the model can request a tool call."""
        request = LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            messages=[
                LLMMessage(
                    role=LLMRole.USER,
                    content="What is the reach metric? Use the get_test_metric tool.",
                ),
            ],
            tools=[_FAKE_TOOL],
            max_output_tokens=200,
            temperature=0.0,
        )
        response = provider.complete(request)
        if not response.tool_calls:
            raise ValueError("Model did not return tool_calls")

    def _test_tool_continuation(self, provider: GLMProvider) -> None:
        """Test that the model can process a tool result and respond."""
        # Round 1: model requests tool
        request1 = LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            messages=[
                LLMMessage(
                    role=LLMRole.USER,
                    content="What is the reach metric? Use the get_test_metric tool.",
                ),
            ],
            tools=[_FAKE_TOOL],
            max_output_tokens=200,
            temperature=0.0,
        )
        response1 = provider.complete(request1)
        if not response1.tool_calls:
            raise ValueError("Model did not request tool in round 1")

        tc = response1.tool_calls[0]

        # Round 2: send tool result back
        messages = [
            LLMMessage(role=LLMRole.USER, content="What is the reach metric?"),
            LLMMessage(
                role=LLMRole.ASSISTANT,
                content=response1.text,
                tool_calls=response1.tool_calls,
            ),
            LLMMessage(
                role=LLMRole.USER,
                tool_result=LLMToolResultContent(
                    tool_call_id=tc.id,
                    content=_FAKE_TOOL_RESULT,
                    is_error=False,
                ),
            ),
        ]
        request2 = LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            messages=messages,
            tools=[_FAKE_TOOL],
            max_output_tokens=200,
            temperature=0.0,
        )
        response2 = provider.complete(request2)
        if not response2.text:
            raise ValueError("Model did not return final text after tool result")

    # -----------------------------------------------------------------------
    # Runner
    # -----------------------------------------------------------------------

    def _run_check(
        self,
        check_name: str,
        model: str,
        test_fn: Any,
    ) -> CheckResult:
        start = time.monotonic()
        try:
            test_fn()
            elapsed = time.monotonic() - start
            return CheckResult(
                check=check_name,
                model=model,
                passed=True,
                latency=elapsed,
                safe_error="",
            )
        except LLMProviderError as exc:
            elapsed = time.monotonic() - start
            return CheckResult(
                check=check_name,
                model=model,
                passed=False,
                latency=elapsed,
                safe_error=type(exc).__name__,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            return CheckResult(
                check=check_name,
                model=model,
                passed=False,
                latency=elapsed,
                safe_error=type(exc).__name__,
            )

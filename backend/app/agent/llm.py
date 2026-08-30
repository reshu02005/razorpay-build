"""
Google Gemini client: the reasoning engine, wrapped so it can never take the
application down with it.

Two ideas run through this module.

**The LLM is an optional dependency, not a requirement.** RecoverAI must clone,
install and run with zero credentials. So the ``google.genai`` import is guarded,
a missing API key is a supported configuration rather than an error, and *every*
failure mode -- no key, package not installed, network down, quota exceeded,
malformed response, model that will not stop calling tools -- is funnelled into
one exception, :class:`LLMUnavailable`. The orchestrator catches it and runs the
deterministic planner instead. A merchant's revenue-recovery pipeline does not
get to depend on a third-party API being up.

**The loop is bounded on every axis.** Steps, wall-clock time, and consecutive
turns without a tool call. An unbounded agent loop is a cost and latency incident
waiting to happen: a model that keeps asking for one more lookup will happily
spend a merchant's quota and leave a spinner running until an HTTP timeout kills
it. Each bound has a named constant or a setting, and hitting any of them
degrades to the rule-based planner rather than hanging.

The response parsing is written defensively -- ``getattr`` with fallbacks,
tolerating empty ``candidates`` -- because the SDK's surface has moved between
versions. That defensiveness is what turns an SDK change into a *degradation*
(this run falls back to rules, the UI says so) rather than an *outage* (every
analysis 500s until someone ships a fix).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.tools import TERMINAL_TOOL, ToolRegistry, ToolSpec
from app.config import Settings
from app.domain.schemas import AgentRecoveryPlan

# The SDK is an optional dependency. Importing it inside a try/except is not
# defensive clutter: `pip install -r requirements.txt` may legitimately have been
# skipped, or run behind a firewall that blocked one wheel, and the correct
# behaviour is a labelled rule-based run -- not an ImportError at start-up that
# makes the whole API unimportable.
try:  # pragma: no cover - exercised by whichever environment lacks the package
    from google import genai
    from google.genai import types
except Exception:  # noqa: BLE001 - any import-time failure means "unavailable"
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


#: Low, but not zero. A payment decision should be reproducible enough that two
#: analyses of the same failure agree; some sampling still helps the model
#: recover when its first phrasing of a rationale is poor.
_TEMPERATURE = 0.2

#: The whole conversation must finish within this multiple of the single-call
#: timeout. Eight healthy Gemini Flash calls comfortably fit inside twice one
#: call's worst case, so a loop that needs more than that is misbehaving -- and a
#: deterministic answer now beats a model's answer in three minutes.
_LOOP_BUDGET_MULTIPLIER = 2.0

#: Consecutive model turns with no function call before we give up. One is not
#: enough: models sometimes narrate a step before acting, and a single nudge
#: usually recovers it. Two in a row means it has stopped working the problem.
_MAX_EMPTY_TURNS = 2

#: Sent once when the model replies with prose instead of a tool call.
_NUDGE = (
    "You must continue by calling a tool. When you have gathered enough evidence, "
    f"call {TERMINAL_TOOL} with your final recommendation. Do not reply with prose."
)


class LLMUnavailable(RuntimeError):
    """
    The LLM path could not produce a valid plan, for any reason.

    Deliberately one exception rather than a hierarchy. Every caller does the
    same thing with it -- fall back to the rule-based planner and record the
    reason on the case -- so splitting it into ``QuotaExceeded``,
    ``TimeoutError``, ``ParseError`` would create branches nobody would ever
    write differently. The distinguishing detail lives in the message, which is
    written to be readable by a merchant in the UI's "degraded" badge.
    """


@dataclass
class LLMStep:
    """
    One executed tool call inside the reasoning loop.

    Emitted through the ``on_step`` callback as it happens rather than returned
    in a batch at the end, so that a run which later fails still leaves behind
    the steps it did complete. A partial trace is evidence; a discarded one is
    not.

    Attributes:
        step: 1-based position in the run.
        tool_name: Tool the model asked for.
        arguments: Arguments as the model supplied them, before validation.
        result: Whatever the tool returned, including error dicts.
        ok: False when the tool returned an ``error`` key.
        error: The error message, or ``None``.
        latency_ms: Wall-clock duration of the tool call itself.
    """

    step: int
    tool_name: str
    arguments: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Defensive response parsing
# ---------------------------------------------------------------------------


def _extract_function_calls(response: Any) -> list[Any]:
    """
    Pull the function calls out of a Gemini response, whatever shape it arrived in.

    Recent SDK versions expose a flattened ``response.function_calls``; older ones
    require walking ``candidates -> content -> parts -> function_call``. Both are
    tried, and anything unrecognised yields an empty list, which the loop treats
    as "the model said nothing actionable" -- a state it already knows how to
    handle.

    Args:
        response: The object returned by ``chat.send_message``.

    Returns:
        A list of function-call objects, possibly empty. Never raises.
    """
    direct = getattr(response, "function_calls", None)
    if direct:
        return list(direct)

    calls: list[Any] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(fc)
    return calls


def _json_safe(value: Any) -> dict[str, Any]:
    """
    Coerce a tool result into something the SDK can serialise.

    Function responses are sent to Gemini as JSON. A ``datetime`` or a ``Decimal``
    that slipped into a tool result would otherwise raise deep inside the SDK,
    turning a cosmetic bug into a failed analysis; ``default=str`` degrades it to
    text instead. Non-dict results are wrapped, because the function-response
    part requires an object.

    Args:
        value: A tool result.

    Returns:
        A dict guaranteed to round-trip through ``json.dumps``.
    """
    try:
        return json.loads(json.dumps(value if isinstance(value, dict) else {"result": value}, default=str))
    except Exception:  # noqa: BLE001 - last resort: describe rather than fail
        return {"result": str(value)}


class GeminiClient:
    """
    Thin, bounded wrapper around Gemini function calling.

    Owns no application logic: it does not know what a recovery is, only how to
    run a tool loop against a :class:`~app.agent.tools.ToolRegistry` until that
    registry reports a submitted plan. Keeping the domain knowledge in the tools
    and the prompts means swapping the model provider touches this file alone.
    """

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: Active configuration. Read for the API key, the model name
                and the per-call timeout; never mutated.
        """
        self._settings = settings
        # Created on first use rather than in __init__: constructing a client is
        # pointless work on every request in the (supported, common) no-key case.
        self._client: Any | None = None

    @property
    def available(self) -> bool:
        """True when an LLM run is even worth attempting."""
        return bool(self._settings.gemini_api_key.strip()) and genai is not None

    @property
    def model(self) -> str:
        """The configured model id, recorded on the run for the audit trail."""
        return self._settings.gemini_model

    def unavailable_reason(self) -> str | None:
        """
        Explain, in one sentence a merchant can read, why the LLM path is off.

        Returns ``None`` when it is on. Surfaced in the UI's degraded badge, so
        "the AI is unavailable" is never left as an unexplained assertion.
        """
        if genai is None:
            return "The google-genai package is not installed; using the rule-based planner."
        if not self._settings.gemini_api_key.strip():
            return "No GEMINI_API_KEY is configured; using the rule-based planner."
        return None

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _get_client(self) -> Any:
        """Lazily construct and cache the SDK client."""
        if self._client is None:
            self._client = genai.Client(api_key=self._settings.gemini_api_key.strip())
        return self._client

    @staticmethod
    def _declaration(spec: ToolSpec) -> Any:
        """
        Convert one :class:`ToolSpec` into a Gemini function declaration.

        A tool with no parameters is declared *without* a ``parameters`` key at
        all rather than with an empty object: some SDK/API versions reject an
        object schema whose ``properties`` is empty, and ``get_recovery_policy``
        genuinely takes no arguments.
        """
        payload: dict[str, Any] = {"name": spec.name, "description": spec.description}
        if spec.parameters.get("properties"):
            payload["parameters"] = spec.parameters
        return types.FunctionDeclaration(**payload)

    def _build_config(self, declarations: list[Any], system_prompt: str) -> Any:
        """
        Build the generation config, tolerating SDK field drift.

        The required fields (system instruction, tools, temperature) are passed
        first; the nice-to-have ones are attempted together and dropped wholesale
        if the installed SDK does not know them. Losing an explicit timeout is a
        degradation the outer wall-clock budget already covers; failing to build
        a config at all would cost the whole LLM path.
        """
        base: dict[str, Any] = {
            "system_instruction": system_prompt,
            "tools": [types.Tool(function_declarations=declarations)],
            "temperature": _TEMPERATURE,
        }

        optional: dict[str, Any] = {}
        try:
            # Timeouts are expressed in milliseconds by the SDK's HTTP layer.
            optional["http_options"] = types.HttpOptions(
                timeout=int(self._settings.gemini_timeout_seconds * 1000)
            )
            # We execute tools ourselves so the trace is recorded and the results
            # are auditable. Automatic function calling would run them invisibly.
            optional["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )
        except Exception:  # noqa: BLE001 - unknown field names on this SDK version
            optional = {}

        try:
            return types.GenerateContentConfig(**base, **optional)
        except Exception:  # noqa: BLE001
            logger.warning("Gemini config rejected optional fields; retrying with the minimum set")
            return types.GenerateContentConfig(**base)

    # -----------------------------------------------------------------
    # The loop
    # -----------------------------------------------------------------

    def run_tool_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        registry: ToolRegistry,
        max_steps: int,
        on_step: Callable[[LLMStep], None],
    ) -> AgentRecoveryPlan:
        """
        Run the model against the registry until it submits a valid plan.

        Args:
            system_prompt: The agent's standing instructions.
            user_prompt: The case-specific brief for this payment.
            registry: Toolset for this analysis. Also the source of the stop
                condition -- the loop ends when ``registry.submitted_plan`` is set.
            max_steps: Hard ceiling on executed tool calls.
            on_step: Called once per executed tool call, as it happens, so a run
                that later fails still leaves a partial trace behind.

        Returns:
            The validated :class:`AgentRecoveryPlan` the model submitted.

        Raises:
            LLMUnavailable: on *any* failure -- no key, missing package, API or
                network error, timeout, or the loop finishing without a valid
                plan. No SDK exception is ever allowed to escape this method: the
                caller's only sensible reaction is to fall back, and forcing it to
                pattern-match on third-party exception types would spread the
                SDK's surface across the codebase.
        """
        if not self.available:
            raise LLMUnavailable(self.unavailable_reason() or "The LLM path is unavailable.")

        started = time.monotonic()
        deadline = started + self._settings.gemini_timeout_seconds * _LOOP_BUDGET_MULTIPLIER

        try:
            specs = registry.specs()  # Also runs the no-financial-tools assertion.
            chat = self._get_client().chats.create(
                model=self.model,
                config=self._build_config([self._declaration(s) for s in specs], system_prompt),
            )

            message: Any = user_prompt
            step = 0
            empty_turns = 0

            while step < max_steps:
                if time.monotonic() > deadline:
                    raise LLMUnavailable(
                        f"The AI did not finish within {deadline - started:.0f}s; "
                        "using the rule-based planner."
                    )

                response = chat.send_message(message)
                calls = _extract_function_calls(response)

                if not calls:
                    empty_turns += 1
                    if empty_turns >= _MAX_EMPTY_TURNS:
                        raise LLMUnavailable(
                            "The AI stopped calling tools without submitting a plan."
                        )
                    # Nudge once. Models occasionally narrate before acting, and
                    # abandoning the run on the first prose turn would throw away
                    # a run that was one sentence from finishing.
                    message = _NUDGE
                    continue

                empty_turns = 0
                parts: list[Any] = []

                for call in calls:
                    if step >= max_steps:
                        break
                    step += 1

                    name = getattr(call, "name", "") or ""
                    # The SDK returns a mapping-like object; copying it makes the
                    # arguments plain JSON before they are persisted to the trace.
                    raw_args = getattr(call, "args", None) or {}
                    arguments = dict(raw_args)

                    call_started = time.monotonic()
                    result = registry.call(name, arguments)
                    latency_ms = int((time.monotonic() - call_started) * 1000)

                    error = result.get("error")
                    on_step(
                        LLMStep(
                            step=step,
                            tool_name=name,
                            arguments=arguments,
                            result=result,
                            ok=error is None,
                            error=error if isinstance(error, str) else None,
                            latency_ms=latency_ms,
                        )
                    )

                    parts.append(
                        types.Part.from_function_response(
                            name=name or "unknown_tool",
                            response=_json_safe(result),
                        )
                    )

                    # Terminal condition. Checked against the registry rather than
                    # the result dict alone, so a plan only ends the loop once it
                    # has actually passed AgentRecoveryPlan validation.
                    if name == TERMINAL_TOOL and registry.submitted_plan is not None:
                        return registry.submitted_plan

                message = parts

            raise LLMUnavailable(
                f"The AI used all {max_steps} allowed steps without submitting a plan."
            )

        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate funnel; see the docstring
            logger.warning("Gemini tool loop failed: %s", exc, exc_info=True)
            raise LLMUnavailable(
                f"The AI service could not be reached ({type(exc).__name__}); "
                "using the rule-based planner."
            ) from exc

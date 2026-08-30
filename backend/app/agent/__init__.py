"""
The reasoning layer: failure taxonomy, prompts, tools, LLM client and planners.

This package answers one question -- *what should we do about this failed
payment?* -- and nothing else. It reads; it never moves money. The modules that
actually create orders live in ``app.payments`` and are only reachable from
``app.services.recovery_service`` after a human approval.

The package ``__init__`` is deliberately empty of re-exports.

    The obvious alternative was to hoist the common names here
    (``from app.agent import classify_error``). It was rejected because the
    modules in this package form a dependency chain -- ``orchestrator`` imports
    ``llm``, which imports ``tools``, which imports ``taxonomy`` -- and eager
    re-exports would drag the whole chain (including the optional
    ``google.genai`` import) into memory the moment anything touched
    ``app.agent``. Callers import the module they actually need:

        from app.agent.taxonomy import classify_error, PLAYBOOK
        from app.agent.rule_planner import plan_from_rules
"""

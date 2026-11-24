"""Run the installed Hermes model picker and persistence code in exactly one profile.

Invoked with the authenticated profile's HERMES_HOME; no credentials, endpoint
URLs, arbitrary configuration fields, or shell commands come from this job.
"""

from __future__ import annotations

import json
import sys


def run(job: dict) -> dict:
    try:
        from hermes_cli.config import load_config
        from hermes_cli.config_backend import ConfigLockedError
        from hermes_cli.inventory import build_model_options_payload, load_picker_context
        from hermes_cli.model_assignment import ModelAssignmentRejected, validated_main_model_selection
        from hermes_cli.model_switch import persist_model_selection
    except ImportError:
        return {"error": "unsupported"}

    ctx = load_picker_context()
    if job["action"] == "set":
        model, requested_provider = job["model"], job.get("provider")
        options = build_model_options_payload(ctx, include_unconfigured=False)
        providers = [row["slug"] for row in options["providers"]
                     if model in row.get("models", ())
                     and (requested_provider is None or row["slug"] == requested_provider)]
        if (not providers and model == ctx.current_model and ctx.current_provider
                and requested_provider in (None, ctx.current_provider)):
            providers = [ctx.current_provider]  # Existing defaults may outlive a changing catalog.
        if not providers:
            return {"error": "invalid_model"}
        provider = ctx.current_provider if ctx.current_provider in providers else providers[0]
        if not requested_provider and len(set(providers)) > 1 and ctx.current_provider not in providers:
            return {"error": "invalid_model"}
        try:
            selection = validated_main_model_selection(load_config(), provider, model)
            persist_model_selection(selection)
        except ModelAssignmentRejected:
            return {"error": "invalid_model"}
        except ConfigLockedError:
            return {"error": "locked"}
        ctx = load_picker_context()
    return {"can_set_default": True, "model": ctx.current_model, "provider": ctx.current_provider}


def main() -> None:
    try:
        result = run(json.load(sys.stdin))
    except Exception:
        # Avoid forwarding/logging config exceptions (which may contain secrets).
        result = {"error": "model_operation_failed"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()

"""OpenTelemetry setup: Azure Monitor when a connection string is set, a local provider otherwise.

Docs: learn.microsoft.com/agent-framework/agents/observability. configure_azure_monitor first,
then enable_instrumentation for the agent, chat and execute_tool spans. Question and answer
text stay out of telemetry (enable_sensitive_data=False).
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

_done = False


def setup() -> bool:
    """Idempotent. Returns True when traces go to Application Insights."""
    global _done
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if _done:
        return bool(conn)
    _done = True
    if conn:
        from agent_framework.observability import create_resource, enable_instrumentation
        from azure.monitor.opentelemetry import configure_azure_monitor

        os.environ.setdefault("OTEL_SERVICE_NAME", "ddt-copilot")
        configure_azure_monitor(connection_string=conn, resource=create_resource())
        enable_instrumentation(enable_sensitive_data=False)
        return True
    # Local runs and tests: a plain SDK provider so trace ids are real.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    return False

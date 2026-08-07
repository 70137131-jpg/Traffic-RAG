"""OpenTelemetry tracing setup (opt-in).

Off unless OTEL_ENABLED is truthy, so it adds no overhead by default. When on:
- Exports via OTLP/HTTP if OTEL_EXPORTER_OTLP_ENDPOINT is set (production: point at
  a collector / Grafana Tempo / Honeycomb / etc.).
- Otherwise prints spans to the console (handy for local verification).

The pipeline stages in rag_backend create spans unconditionally via the tracer
below; without a configured provider those spans are cheap no-ops.
"""

import logging
import os

from opentelemetry import trace

logger = logging.getLogger(__name__)

# Module tracer used by rag_backend to wrap retrieve/rerank/generate stages. Safe
# to use before (or without) setup_tracing() — it is a no-op until a provider is set.
tracer = trace.get_tracer("traffic_rag")

_configured = False


def tracing_enabled():
    return os.environ.get("OTEL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def setup_tracing(app=None):
    """Configure the global tracer provider and instrument Flask. Idempotent."""
    global _configured
    if _configured or not tracing_enabled():
        return False

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "traffic-rag")})
    provider = TracerProvider(resource=resource)

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        # Batch in production for throughput; the exporter reads OTEL_EXPORTER_* env.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        exporter_name = "otlp"
    else:
        # Simple processor flushes each span immediately — easy to see locally.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        exporter_name = "console"

    trace.set_tracer_provider(provider)

    if app is not None:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        # Don't trace the probe/metrics endpoints — they are noisy and low-value.
        FlaskInstrumentor().instrument_app(
            app, excluded_urls="/health,/ready,/metrics"
        )

    _configured = True
    logger.info("OpenTelemetry tracing enabled (exporter=%s).", exporter_name)
    return True

import os
import re
import time
from flask import Flask, request, jsonify
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

PROMPT_VERSION = os.environ.get('PROMPT_VERSION', 'v1.0.0')
GIT_SHA = os.environ.get('GIT_SHA', 'unknown')

# Total requests by outcome. `status` distinguishes policy rejections (200s) from
# invalid input and unhandled errors, so a 500 outage doesn't hide inside a "healthy"
# request-rate graph.
REQUEST_COUNT = Counter(
    'agent_requests_total',
    'Total number of requests to the agent API',
    ['prompt_version', 'route', 'status']
)

# Rejections by reason. A 200-with-rejected=true is invisible to status-code monitoring,
# and the reason tells an operator which attack class (if any) is driving a spike.
REJECTION_COUNT = Counter(
    'agent_rejections_total',
    'Total number of requests rejected by the safety classifier',
    ['prompt_version', 'reason']
)

# Buckets sized from observed traffic: the handler is pure regex over a short string
# (measured mean ~90us, no observation above 5ms), so the default buckets starting at
# 5ms put every request in one bucket and make p95 a constant. Upper range is kept for
# genuinely pathological latency (GC pause, resource exhaustion).
REQUEST_LATENCY = Histogram(
    'agent_request_latency_seconds',
    'Request latency in seconds',
    ['prompt_version', 'route'],
    buckets=[0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.1, 0.5, 1.0]
)

# Saturation - the one golden signal the other three can't stand in for. gunicorn
# runs --threads 8, so 8 is a hard concurrency ceiling: sustained in-flight near it
# means requests are queueing behind the thread pool. Latency shows that too, but
# later and as a symptom. No `route` label - the ceiling is per-process.
INFLIGHT = Gauge(
    'agent_inflight_requests',
    'Requests currently being handled (thread-pool saturation; ceiling is 8)',
    ['prompt_version']
)

# Identifies what's actually running, so "did a deploy cause this" is a PromQL query
# instead of a Slack archaeology exercise.
BUILD_INFO = Gauge(
    'agent_build_info',
    'Build information for the running agent (value is always 1; identity is in labels)',
    ['prompt_version', 'git_sha']
)
BUILD_INFO.labels(prompt_version=PROMPT_VERSION, git_sha=GIT_SHA).set(1)

# Deliberately not instrumented - these absences are decisions, not gaps:
#
# - Tokens and cost (OpenTelemetry's gen_ai.client.token.usage), and streaming
#   timings (time_to_first_chunk). This service has no model: generate_response()
#   returns one of four fixed strings and nothing streams. A token count here would
#   be invented, and someone would eventually alert on it. The mapping that does
#   hold: agent_request_latency_seconds is the local gen_ai.client.operation.duration,
#   and prompt_version does the job gen_ai.request.model would.
# - Latency split by status. The rejected path short-circuits on the first matching
#   pattern; the accepted path has to fail all 26. Measured over 200 requests each,
#   the difference was noise (p50 3.35ms vs 3.82ms end-to-end), so it isn't worth
#   quadrupling an 11-bucket histogram.
# - Per-client labels. Unbounded values belong in logs or traces, not metrics.
#
# The metric this service most needs and cannot have: the classifier's false-positive
# rate. Live traffic carries no ground truth, so nothing here separates "correctly
# rejected an attack" from "wrongly rejected a real user" - the question
# docs/incident-response.md opens with. See its §5.

# Rejection patterns - deterministic classification based on message content
REJECTION_PATTERNS = {
    'prompt_injection': [
        r'ignore\s+(all\s+)?(previous\s+)?instructions',
        r'system\s+prompt',
        r'disregard\s+(all\s+)?(previous\s+)?',
        r'forget\s+(all\s+)?(previous\s+)?instructions',
        r'new\s+instructions',
        r'override\s+(all\s+)?rules',
        r'jailbreak',
        r'bypass\s+(safety|filter|restriction)',
    ],
    'secrets_request': [
        r'password',
        r'api[\s_-]?key',
        r'secret[\s_-]?key',
        r'access[\s_-]?token',
        r'private[\s_-]?key',
        r'credentials',
        r'auth[\s_-]?token',
        r'bearer[\s_-]?token',
    ],
    'dangerous_action': [
        r'restart\s+prod',
        r'delete\s+(the\s+)?database',
        r'drop\s+table',
        r'rm\s+-rf',
        r'shutdown\s+server',
        r'execute\s+command',
        r'run\s+as\s+root',
        r'sudo\s+',
        r'format\s+(hard\s+)?drive',
        r'wipe\s+(all\s+)?data',
    ],
}


def classify_rejection(message: str) -> tuple[bool, str | None]:
    """
    Classify whether a message should be rejected and return the reason.
    Returns (rejected, reason) tuple.
    """
    message_lower = message.lower()

    for reason, patterns in REJECTION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, message_lower):
                return True, reason

    return False, None


def generate_response(message: str) -> str:
    """Generate a simple response for accepted messages."""
    responses = [
        f"I understand you're asking about: {message[:50]}...",
        "That's an interesting question. Let me help you with that.",
        "I'd be happy to assist with your request.",
        "Thank you for your question. Here's what I can tell you.",
    ]
    return responses[hash(message) % len(responses)]


def _invalid_request_response(error: str):
    return jsonify({
        'error': error,
        'rejected': True,
        'reason': 'invalid_request',
        'prompt_version': PROMPT_VERSION,
        'answer': None
    }), 400


@app.route('/ask', methods=['POST'])
def ask():
    """
    Main endpoint for asking the agent.
    Accepts JSON with 'message' field.
    Returns rejection status, reason, prompt version, and answer.
    """
    start_time = time.time()
    # Default to 'error': only overwritten below on a path that completes without
    # raising, so an unhandled exception is still counted (via `finally`) as an error
    # rather than silently inflating a healthy-looking request count.
    status = 'error'
    INFLIGHT.labels(prompt_version=PROMPT_VERSION).inc()

    try:
        # silent=True returns None instead of raising on missing/invalid content-type
        # or malformed JSON, so those cases fall into the same 400 path as a missing
        # field rather than surfacing as an uncaught 415/400 HTML error.
        data = request.get_json(silent=True)
        message = data.get('message') if isinstance(data, dict) else None

        if not isinstance(message, str):
            status = 'invalid_request'
            return _invalid_request_response('Missing required field: message')

        rejected, reason = classify_rejection(message)

        if rejected:
            status = 'rejected'
            REJECTION_COUNT.labels(prompt_version=PROMPT_VERSION, reason=reason).inc()
            response = {
                'rejected': True,
                'reason': reason,
                'prompt_version': PROMPT_VERSION,
                'answer': f"I cannot process this request due to: {reason}"
            }
        else:
            status = 'accepted'
            response = {
                'rejected': False,
                'reason': None,
                'prompt_version': PROMPT_VERSION,
                'answer': generate_response(message)
            }

        return jsonify(response), 200

    finally:
        latency = time.time() - start_time
        REQUEST_COUNT.labels(prompt_version=PROMPT_VERSION, route='/ask', status=status).inc()
        REQUEST_LATENCY.labels(prompt_version=PROMPT_VERSION, route='/ask').observe(latency)
        INFLIGHT.labels(prompt_version=PROMPT_VERSION).dec()


@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check endpoint."""
    REQUEST_COUNT.labels(prompt_version=PROMPT_VERSION, route='/healthz', status='accepted').inc()
    return jsonify({
        'status': 'healthy',
        'prompt_version': PROMPT_VERSION
    }), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    """Prometheus metrics endpoint."""
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    """Keep the JSON error contract on routing errors (404/405) and rejected bodies."""
    return jsonify({
        'error': exc.description,
        'rejected': True,
        'reason': 'invalid_request',
        'prompt_version': PROMPT_VERSION,
        'answer': None
    }), exc.code


@app.errorhandler(Exception)
def handle_unexpected_exception(exc: Exception):
    """Last-resort handler so a bug returns the same JSON contract as any other error."""
    return jsonify({
        'error': 'Internal server error',
        'rejected': True,
        'reason': 'internal_error',
        'prompt_version': PROMPT_VERSION,
        'answer': None
    }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Single worker, multiple threads: prometheus_client keeps counters in per-process
# memory, so pre-forked workers (gunicorn's default concurrency model) would each
# expose only their own partial counts - whichever worker answers a given /metrics
# scrape would make counters look like they reset. One worker keeps one registry;
# threads give real concurrency for this I/O-light app. Scale horizontally via
# replicas (see deployment/manifest.yml) instead of adding workers here.
#
# --access-logfile -: unlike Flask's dev server, gunicorn logs no per-request
# line by default. Without this, `docker compose logs agent-api` shows nothing
# but startup banners - discovered by walking the incident-response runbook
# against a live traffic spike and finding the log-grep step returned nothing.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --access-logfile - app:app"]

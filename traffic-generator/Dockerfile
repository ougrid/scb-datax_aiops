FROM python:3.11-slim

WORKDIR /app

# Without this, stdout is fully buffered (not a TTY) and this script's periodic
# print() summaries never flush - `docker compose logs` shows nothing.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "generator.py"]

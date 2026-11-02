# Deployment

TankoPress is a full-stack Python application. Static website hosting is not sufficient because the FastAPI backend performs scraping, downloads, PDF conversion, and export jobs.

## General requirements

Your deployment platform must provide:

- Python 3.11 or newer
- A persistent or writable application filesystem
- Playwright Chromium with required OS libraries
- Enough disk space for temporary exports
- WebSocket-free HTTP streaming support for Server-Sent Events
- HTTPS for public production use

## VPS deployment

Example Ubuntu setup:

```bash
sudo apt update
sudo apt install python3 python3-venv

git clone <your-github-repository-url>
cd <repository-directory>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Run the app as a service or process manager, for example:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Place a reverse proxy such as Caddy or Nginx in front of Uvicorn and enable HTTPS. Set an appropriate timeout for background downloads and streamed progress events.

## Render or Railway

Use a Python web service and run:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Before publishing, confirm that the build installs Chromium. Railway and Render container images may require a custom Dockerfile or Nixpacks configuration with Playwright OS dependencies.

## Docker outline

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && python -m playwright install --with-deps chromium
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Use HTTPS and persistent storage or object storage for production exports. In-memory job state is suitable for a single process; multi-instance deployments require a shared queue and job store.

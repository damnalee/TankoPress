# Contributing

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pytest
uvicorn main:app --reload
```

## Guidelines

- Keep scraper changes focused and covered by pagination tests.
- Do not commit generated exports, virtual environments, browser caches, or screenshots of secrets.
- Use the existing FastAPI and scraper abstractions.
- Preserve source-site terms and rate-limit safeguards.
- Test both merged PDF and ZIP exports before opening a pull request.
- Keep frontend changes responsive and accessible.

## Pull requests

Describe the behavior changed, include screenshots for visual updates, and list the commands used to validate the change.

import asyncio
import io
import json
import random
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from pdf_engine import PDFProcessor
from scraper import GenericPlaywrightProvider

app = FastAPI(title="TankoPress", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
provider = GenericPlaywrightProvider()
processor = PDFProcessor()
output_root = Path("output")
output_root.mkdir(exist_ok=True)

USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
)
jobs: dict[str, dict] = {}
sse_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


class DownloadRequest(BaseModel):
    chapters: list[dict]
    mode: str = "merged"
    image_quality: str = "original"
    manga_title: str = "TankoPress"


def public_job(job_id: str) -> dict:
    job = jobs[job_id]
    return {key: value for key, value in job.items() if key != "error_detail"}


def publish(job_id: str, update: dict) -> None:
    jobs[job_id].update(update)
    for queue in list(sse_subscribers.get(job_id, ())):
        try:
            queue.put_nowait(dict(jobs[job_id]))
        except asyncio.QueueFull:
            pass


async def download_image(client: httpx.AsyncClient, url: str, referer: str, semaphore: asyncio.Semaphore, timeout: int = 8) -> bytes:
    headers = {"Referer": referer, "User-Agent": random.choice(USER_AGENTS)}
    for attempt in range(4):
        try:
            async with semaphore:
                response = await client.get(url, headers=headers, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError("retryable status", request=response.request, response=response)
            response.raise_for_status()
            if not response.content.startswith((b"\xff\xd8", b"\x89PNG", b"RIFF", b"\x00\x00\x01")) and not response.headers.get("content-type", "").startswith("image/"):
                raise ValueError("Downloaded content is not an image")
            return response.content
        except (httpx.HTTPError, ValueError):
            if attempt == 3:
                raise
            await asyncio.sleep(0.75 * (2 ** attempt) + random.random() * 0.25)
    raise RuntimeError("Image download failed")


async def run_download_job(job_id: str, chapters: list[dict], mode: str, quality: str) -> None:
    try:
        job_dir = output_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        publish(job_id, {"state": "running", "log_message": "Job started", "started_at": time.time()})
        semaphore = asyncio.Semaphore(5)
        compiled_chapters: list[tuple[str, list[bytes]]] = []
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for chapter_index, chapter in enumerate(chapters, start=1):
                chapter_name = chapter.get("title") or f"Chapter {chapter.get('number', chapter_index)}"
                publish(job_id, {"current_chapter": chapter_name, "chapter_index": chapter_index, "pages_downloaded": 0, "total_pages": 0,
                                "log_message": f"Reading {chapter_name}"})
                page_urls = await provider.get_chapter_images(chapter["url"])
                publish(job_id, {"total_pages": len(page_urls), "log_message": f"Found {len(page_urls)} pages for {chapter_name}"})
                downloaded: list[bytes | None] = [None] * len(page_urls)
                next_position = 0
                async def fetch(position: int, page_url: str) -> None:
                    nonlocal next_position
                    try:
                        downloaded[position] = await download_image(client, page_url, chapter["url"], semaphore)
                        next_position += 1
                        percent = int(next_position / max(len(page_urls), 1) * 100)
                        publish(job_id, {"pages_downloaded": next_position, "overall_percent": int(((chapter_index - 1) + percent / 100) / len(chapters) * 100),
                                        "log_message": f"Downloaded page {next_position}/{len(page_urls)} • {chapter_name}"})
                    except Exception as exc:
                        raise RuntimeError(f"Failed to download page {position + 1}: {exc}") from exc
                tasks = [asyncio.create_task(fetch(position, url)) for position, url in enumerate(page_urls)]
                await asyncio.gather(*tasks)
                images = [image for image in downloaded if image is not None]
                compiled_chapters.append((chapter_name, images))
                publish(job_id, {"log_message": f"Compiled {chapter_name} ({len(images)} pages)"})
            safe_title = re.sub(r"[^A-Za-z0-9._ -]+", "_", jobs[job_id].get("title", "TankoPress")).strip() or "TankoPress"
        if mode == "zip":
            filename = f"{safe_title}.zip"
            final_path = job_dir / filename
            processor.create_zip(compiled_chapters, str(final_path), quality, safe_title)
            for helper in job_dir.glob(f"{filename}.*.pdf"):
                helper.unlink(missing_ok=True)
        else:
            numbered_chapters = sorted(
                (chapter for chapter in chapters if chapter.get("number") is not None),
                key=lambda chapter: float(chapter["number"]),
            )
            chapter_numbers = [str(chapter["number"]) for chapter in numbered_chapters]
            range_label = chapter_numbers[0] if len(chapter_numbers) == 1 else f"{chapter_numbers[0]}-{chapter_numbers[-1]}"
            filename = f"{safe_title} {range_label}.pdf"
            final_path = job_dir / filename
            processor.merge_chapters(compiled_chapters, str(final_path), quality)
        publish(job_id, {"state": "completed", "overall_percent": 100, "filename": filename,
                        "file_url": f"/api/file/{job_id}", "log_message": "Export complete. File ready to save."})
    except Exception as exc:
        publish(job_id, {"state": "failed", "error_detail": str(exc), "log_message": f"Job failed: {exc}"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/analyze")
async def analyze(payload: dict):
    url = payload.get("url", "").strip()
    if urlparse(url).scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Enter a valid http(s) manga URL")
    url = re.sub(r"/chapter(?:/[^/?#]+)?/?$", "", url, flags=re.I)
    try:
        info = await provider.get_manga_info(url)
        chapters = await provider.get_chapter_list(url)
        return {**info, "chapters": chapters, "total_chapters": len(chapters)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not analyze this catalog: {exc}") from exc


@app.post("/api/download")
async def download(payload: DownloadRequest, background_tasks: BackgroundTasks):
    if not payload.chapters:
        raise HTTPException(status_code=400, detail="Select at least one chapter")
    if payload.mode not in {"zip", "merged"}:
        raise HTTPException(status_code=400, detail="Invalid export mode")
    quality = "original"
    job_id = secrets.token_urlsafe(12)
    jobs[job_id] = {"job_id": job_id, "state": "queued", "current_chapter": "", "chapter_index": 0,
                    "total_chapters": len(payload.chapters), "pages_downloaded": 0, "total_pages": 0,
                    "overall_percent": 0, "log_message": "Waiting for worker", "title": payload.manga_title.strip() or "TankoPress"}
    background_tasks.add_task(run_download_job, job_id, payload.chapters, payload.mode, quality)
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        queue = asyncio.Queue(maxsize=100)
        sse_subscribers[job_id].add(queue)
        yield "data: " + json.dumps(public_job(job_id)) + "\n\n"
        try:
            while True:
                update = await queue.get()
                yield "data: " + json.dumps({key: value for key, value in update.items() if key != "error_detail"}) + "\n\n"
                if update["state"] in {"completed", "failed"}:
                    break
        finally:
            sse_subscribers[job_id].discard(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/file/{job_id}")
async def file(job_id: str, background_tasks: BackgroundTasks):
    job = jobs.get(job_id)
    if not job or job["state"] != "completed":
        raise HTTPException(status_code=404, detail="Export is not ready")
    path = next((output_root / job_id).glob(job["filename"]), None)
    if not path:
        raise HTTPException(status_code=404, detail="Export file was removed")
    media_type = "application/pdf" if path.suffix == ".pdf" else "application/zip"
    return FileResponse(path, media_type=media_type, filename=job["filename"])


@app.get("/api/proxy-image")
async def proxy_image(url: str = Query(...), referer: str = Query(default="")):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only http(s) image URLs are supported")
    headers = {"Referer": referer or url, "User-Agent": random.choice(USER_AGENTS)}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg")
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=415, detail="The source did not return an image")
            return StreamingResponse(io.BytesIO(response.content), media_type=content_type,
                                     headers={"Cache-Control": "public, max-age=86400"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Image could not be fetched") from exc

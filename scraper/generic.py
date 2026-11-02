import asyncio
import random
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import BaseMangaProvider


USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36",
)


class GenericPlaywrightProvider(BaseMangaProvider):
    async def _html(self, url: str) -> str:
        headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _rendered_html(self, url: str) -> str:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(3_500)
            html = await page.content()
            await browser.close()
            return html

    async def get_manga_info(self, url: str) -> dict:
        html = await self._html(url)
        soup = BeautifulSoup(html, "lxml")
        og_title = soup.select_one('meta[property="og:title"]')
        title_node = soup.select_one("h1") or og_title
        title = (title_node.get("content") if title_node and title_node.name == "meta" else title_node.get_text(" ", strip=True) if title_node else None) or urlparse(url).path.rstrip("/").split("/")[-1].replace("-", " ").title()
        og_image = soup.select_one('meta[property="og:image"]')
        cover_url = og_image["content"] if og_image and og_image.get("content") else ""
        if not cover_url:
            rendered_soup = BeautifulSoup(await self._rendered_html(url), "lxml")
            cover_node = rendered_soup.select_one('.poster img, .cover img, [class*="poster"] img, meta[property="og:image"]')
            if cover_node:
                cover_url = cover_node.get("content") or cover_node.get("src") or cover_node.get("data-src") or ""
        if cover_url:
            cover_url = urljoin(url, cover_url)
        author = "Unknown"
        status = "Unknown"
        for label in soup.select(".author-content, .manga-authors, [itemprop='author']"):
            if label.get_text(" ", strip=True):
                author = label.get_text(" ", strip=True)
                break
        for label in soup.select(".post-status, .manga-status, [itemprop='status']"):
            text = label.get_text(" ", strip=True)
            if text:
                status = text
                break
        chapters = await self.get_chapter_list(url)
        return {
            "title": title.strip(),
            "cover_url": cover_url,
            "author": author,
            "status": status,
            "site_name": urlparse(url).netloc,
            "total_chapters": len(chapters),
        }

    async def get_chapter_list(self, url: str) -> list[dict]:
        candidates = []
        if urlparse(url).hostname and urlparse(url).hostname.endswith("mangafire.to"):
            candidates = await self._mangafire_chapter_api(url)
        if not candidates:
            html = await self._html(url)
            candidates = self._extract_chapter_links(html, url)
        if not candidates:
            html = await self._rendered_html(url)
            candidates = self._extract_chapter_links(html, url)
        if not candidates or len(candidates) < 2 or urlparse(url).hostname.endswith("mangafire.to"):
            candidates = await self._rendered_chapter_pages(url, candidates)
        # Madara and generic themes use chapter list containers; prefer these links first.
        unique = {}
        for chapter_url, text in candidates:
            unique.setdefault(chapter_url, text)
        chapters_by_number = {}
        for index, (chapter_url, text) in enumerate(unique.items(), start=1):
            match = re.search(r"(?:chapter|chap|ch)[\s_.-]*(\d+(?:\.\d+)?)", text, re.I)
            if not match:
                match = re.search(r"/(?:chapter|chap|ch)[-/](\d+(?:\.\d+)?)", chapter_url, re.I)
            number = match.group(1) if match else str(index)
            title = re.sub(r"^\s*(?:chapter|chap|ch)[\s_.-]*\d+(?:\.\d+)?[\s_.:-]*", "", text, flags=re.I).strip()
            title = f"Chapter {number}: {title}" if title else f"Chapter {number}"
            chapters_by_number.setdefault(number, {"id": f"chapter-{number}", "number": number, "title": title, "url": chapter_url})
        chapters = list(chapters_by_number.values())
        def sort_key(item):
            try:
                return float(item["number"])
            except ValueError:
                return float("inf")
        chapters.sort(key=sort_key)
        return chapters

    async def _mangafire_chapter_api(self, url: str) -> list[tuple[str, str]]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
            payloads = {}
            initial_response = None

            async def capture(response):
                nonlocal initial_response
                if "/api/titles/" not in response.url or "/chapters?" not in response.url:
                    return
                try:
                    payload = await response.json()
                    meta = payload.get("meta", {})
                    page_number = int(meta.get("page", 1))
                    payloads[page_number] = payload
                    if initial_response is None:
                        initial_response = response
                except Exception:
                    return

            page.on("response", lambda response: asyncio.create_task(capture(response)))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(3_000)
                if not payloads:
                    await browser.close()
                    return []
                first_payload = payloads.get(1) or next(iter(payloads.values()), {})
                last_page = int(first_payload.get("meta", {}).get("lastPage", 1))
                candidates = []
                seen = set()
                for chapter_url, text in self._extract_chapter_links(await page.content(), url):
                    seen.add(chapter_url)
                    candidates.append((chapter_url, text))

                next_control = page.locator('button[aria-label="Next page"]')
                for page_number in range(2, last_page + 1):
                    if await next_control.count() and await next_control.is_enabled():
                        previous_keys = set(payloads)
                        await next_control.evaluate("(button) => button.click()")
                    else:
                        numeric_controls = page.locator("button.npager__num")
                        selected = None
                        for control_index in range(await numeric_controls.count()):
                            control = numeric_controls.nth(control_index)
                            if (await control.inner_text()).strip() == str(page_number):
                                selected = control
                                break
                        if selected is None:
                            break
                        previous_keys = set(payloads)
                        await selected.evaluate("(button) => button.click()")
                    for __ in range(100):
                        await page.wait_for_timeout(100)
                        if page_number in payloads:
                            break
                        if len(payloads) > len(previous_keys):
                            break
                    if page_number not in payloads and set(payloads) == previous_keys:
                        break

                if last_page not in payloads:
                    last_control = page.locator('button[aria-label="Last page"]')
                    if await last_control.count() and await last_control.is_enabled():
                        await last_control.evaluate("(button) => button.click()")
                        for __ in range(100):
                            await page.wait_for_timeout(100)
                            if last_page in payloads:
                                break
                for page_number in range(1, last_page + 1):
                    for item in payloads.get(page_number, {}).get("items", []):
                        chapter_id = item.get("id")
                        number = item.get("number")
                        if chapter_id is None or number is None:
                            continue
                        chapter_url = f"{url.rstrip('/')}/chapter/{chapter_id}"
                        if chapter_url in seen:
                            continue
                        seen.add(chapter_url)
                        name = " ".join(str(item.get("name", "")).split())
                        text = f"Ch. {number}" + (f" {name}" if name else "")
                        candidates.append((chapter_url, text))
                await browser.close()
                return candidates
            except Exception:
                await browser.close()
                return []

    @staticmethod
    def _page_url(api_url: str, page_number: int) -> str:
        parsed = urlparse(api_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["page"] = [str(page_number)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    async def _rendered_chapter_pages(self, url: str, initial: list[tuple[str, str]]) -> list[tuple[str, str]]:
        candidates = list(initial)
        seen = {chapter_url for chapter_url, _ in candidates}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=random.choice(USER_AGENTS))
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2_500)

            previous_signature = None
            no_new_pages = 0
            visited_urls = {url}
            url_queue = []
            button_pages = 2

            for _ in range(500):
                html = await page.content()
                page_links = self._extract_chapter_links(html, url)
                for chapter_url, text in page_links:
                    if chapter_url not in seen:
                        seen.add(chapter_url)
                        candidates.append((chapter_url, text))

                signature_links = [chapter_url for chapter_url, _ in page_links]
                signature = (len(signature_links), tuple(signature_links[:1]), tuple(signature_links[-1:]))
                if signature == previous_signature:
                    no_new_pages += 1
                else:
                    no_new_pages = 0
                previous_signature = signature

                discovered = await page.eval_on_selector_all("a[href]", """els => els.map(e => ({href:e.href, text:(e.innerText || '').trim()})).filter(x => x.href.includes('?page=') || x.href.includes('&page=') || x.href.includes('/page/') || x.href.includes('/paged/') || x.href.includes('/p/'))""")
                for item in discovered:
                    href = item["href"]
                    if href and urlparse(href).netloc == urlparse(url).netloc and href not in visited_urls and href not in url_queue:
                        url_queue.append(href)

                if url_queue:
                    next_url = url_queue.pop(0)
                    visited_urls.add(next_url)
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=45_000)
                    await page.wait_for_timeout(700)
                    continue

                if no_new_pages >= 2:
                    break

                numeric = page.locator("button, a[href='#'], a[href='javascript:void(0)'], a:not([href])")
                numeric_count = await numeric.count()
                selected = None
                for index in range(numeric_count):
                    control = numeric.nth(index)
                    try:
                        if await control.is_visible() and (await control.inner_text()).strip() == str(button_pages):
                            selected = control
                            break
                    except Exception:
                        continue
                if selected is None:
                    next_control = page.locator("button:has-text('Next'), a:has-text('Next'), a[rel='next']")
                    if await next_control.count() == 0:
                        break
                    selected = next_control.first

                try:
                    before = await page.locator("a[href*='/chapter/'], a[href*=chapter]").evaluate_all("els => els.map(e => e.href).join('|')")
                    await selected.click(timeout=3_000)
                    await page.wait_for_timeout(700)
                    try:
                        await page.wait_for_function("old => document.querySelectorAll('a[href*=\"chapter\"]').length > 0 && [...document.querySelectorAll('a[href*=\"chapter\"]')].map(e => e.href).join('|') !== old", before, timeout=2500)
                    except Exception:
                        pass
                except Exception:
                    break
                button_pages += 1
            await browser.close()
        return candidates

    def _extract_chapter_links(self, html: str, url: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        candidates: list[tuple[str, str]] = []
        seen_links = set()
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(" ", strip=True)
            if link.find_parent(["nav", "pagination"]):
                continue
            if link.get("rel") and {"next", "prev"}.intersection(link.get("rel", [])):
                continue
            if not href or href.startswith(("#", "javascript:")) or re.fullmatch(r"(?:next|prev|previous|\d+|[«»‹›])", text.strip(), re.I):
                continue
            full_url = urljoin(url, href)
            chapter_path = re.search(r"/(?:chapter|chap|ch)(?:[-_/]|$)", full_url, re.I)
            chapter_text = re.search(r"(?:chapter|chap|ch)[\s_.-]*\d+", text, re.I)
            if not chapter_path and not chapter_text:
                continue
            if full_url == url or full_url in seen_links:
                continue
            seen_links.add(full_url)
            candidates.append((full_url, text))
        return candidates

    async def get_chapter_images(self, chapter_url: str) -> list[str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=random.choice(USER_AGENTS))
            api_page_urls = []

            async def capture_chapter_api(response):
                if "/api/chapters/" not in response.url:
                    return
                try:
                    payload = await response.json()
                    api_page_urls.extend(
                        item["url"] for item in payload.get("data", {}).get("pages", [])
                        if isinstance(item, dict) and item.get("url")
                    )
                except Exception:
                    return

            page.on("response", lambda response: asyncio.create_task(capture_chapter_api(response)))
            await page.goto(chapter_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2_500)
            await page.evaluate("async () => { const height = document.body.scrollHeight; for (const ratio of [0.25, 0.5, 0.75, 1]) { window.scrollTo(0, height * ratio); await new Promise(r => setTimeout(r, 180)); } window.scrollTo(0, 0); }")
            await page.wait_for_timeout(1_000)
            images = api_page_urls or await page.eval_on_selector_all(
                ".reader-img, .reading-content img, #readerarea img, div.page-break img, [data-page] img, .chapter-content img, .readimg img, img[data-src], img[srcset]",
                """els => els.map(el => el.currentSrc || el.dataset.src || el.dataset.original || el.dataset.lazySrc || el.dataset.pagespeedLazySrc || el.src || el.getAttribute('srcset')?.split(' ')[0]).filter(Boolean)""",
            )
            await browser.close()
        clean = []
        for src in images:
            absolute = urljoin(chapter_url, src.split()[0])
            if absolute not in clean and absolute.startswith(("http://", "https://")):
                clean.append(absolute)
        return clean

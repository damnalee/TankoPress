import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import re

from scraper.generic import GenericPlaywrightProvider


JS_PAGE = """
<!doctype html><html><body>
<div id="chapters"></div>
<nav class="pagination"><button class="page" data-page="1">1</button><button class="page" data-page="2">2</button><button class="page" data-page="3">3</button><button>Next</button></nav>
<script>
const box=document.getElementById('chapters'); let current=1;
function render(){box.innerHTML=Array.from({length:20},(_,i)=>{const n=(current-1)*20+i+1; return `<a href="/chapter/${n}">Chapter ${n}</a>`}).join('');}
for(const button of document.querySelectorAll('button.page')) button.addEventListener('click',()=>{const page=Number(button.dataset.page); setTimeout(()=>{current=page;render();}, page===2 ? 500 : 0);});
document.querySelector('button:last-child').addEventListener('click',()=>{setTimeout(()=>{current=Math.min(3,current+1);render();},100);});
render();
</script></body></html>
"""


async def collect_js_page():
    provider = GenericPlaywrightProvider()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = JS_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/manga"
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(300)
            html = await page.content()
            initial = provider._extract_chapter_links(html, base_url)
            result = await provider._rendered_chapter_pages(base_url, initial)
            await browser.close()
        return result
    finally:
        server.shutdown()


def run(coro):
    return asyncio.run(coro)


def test_page1_and_page2_unique_async_delay():
    result = run(collect_js_page())
    numbers = [int(re.search(r"/(\d+)$", url).group(1)) for url, _ in result]
    assert len(result) == 60
    assert len(numbers) == len(set(numbers))
    assert numbers[:20] == list(range(1, 21))
    assert numbers[20:40] == list(range(21, 41))


def test_pagination_links_excluded_and_dedupe_overlap():
    html = '<nav class="pagination"><a href="/page/1">1</a><a href="/page/2">2</a></nav><a href="/chapter/1">Chapter 1</a><a href="/chapter/2">Chapter 2</a><a href="/chapter/2">Chapter 2</a>'
    provider = GenericPlaywrightProvider()
    links = provider._extract_chapter_links(html, "http://catalog.local/manga")
    assert links == [("http://catalog.local/chapter/1", "Chapter 1"), ("http://catalog.local/chapter/2", "Chapter 2")]


def test_url_pagination_links_are_discovered():
    html = '<a href="/chapter/21">Chapter 21</a><a href="?page=2">2</a><a href="?page=3">3</a>'
    provider = GenericPlaywrightProvider()
    links = provider._extract_chapter_links(html, "http://catalog.local/manga?page=1")
    assert links == [("http://catalog.local/chapter/21", "Chapter 21")]


def test_hidden_api_page_url_preserves_vrf():
    api_url = "https://reader.test/api/titles/hid/chapters?page=1&limit=20&vrf=token"
    page_2 = GenericPlaywrightProvider._page_url(api_url, 2)
    assert "page=2" in page_2
    assert "limit=20" in page_2
    assert "vrf=token" in page_2

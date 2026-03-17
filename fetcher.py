"""
Fetcher module: takes URLs and extracts clean article text optimized for
text-to-speech narration.

Adapted from the tab-triage pipeline's fetcher, but with different text
cleaning priorities:
  - Strip footnote markers, citation numbers, image captions
  - Remove code blocks, tables, and other non-prose content
  - Handle headings gracefully (insert pause markers)
  - Remove navigation cruft, cookie banners, etc.
  - Keep meaningful prose that sounds good when read aloud

Extraction strategy (in priority order):
  1. Site-specific extractors (YouTube, Substack, academic papers)
  2. Structured data in HTML (JSON-LD, OpenGraph, __NEXT_DATA__)
  3. Trafilatura (best general-purpose article extractor)
  4. BeautifulSoup fallback (raw visible text from content areas)
"""

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urlparse

import requests

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

import config

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Container for the result of fetching a URL."""
    url: str
    title: str = ""
    text: str = ""           # raw extracted text
    audio_text: str = ""     # cleaned text optimized for TTS
    author: str = ""
    success: bool = True
    error: str = ""
    extraction_method: str = ""
    word_count: int = 0
    estimated_minutes: float = 0.0  # estimated audio duration at ~150 wpm

    def __post_init__(self):
        if self.audio_text:
            self.word_count = len(self.audio_text.split())
            self.estimated_minutes = round(self.word_count / 150, 1)


# ── Main entry point ───────────────────────────────────────────────────

def fetch_url(url: str) -> FetchResult:
    """Fetch a URL and extract clean text optimized for TTS narration."""
    url = url.strip()
    if not url:
        return FetchResult(url=url, success=False, error="Empty URL")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    try:
        response = _download(url)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        return FetchResult(url=url, success=False,
                           error=f"HTTP {status} from {domain}")
    except Exception as e:
        return FetchResult(url=url, success=False,
                           error=f"Download failed: {e}")

    html = response.text
    title, text, author, method = "", "", "", ""

    # ── Layer 1: Site-specific extractors ──────────────────────────
    if _is_youtube(domain):
        title, text = _extract_youtube(html)
        method = "youtube"
    elif _is_substack(html, domain):
        title, text = _extract_substack(html)
        method = "substack"
    elif _is_lesswrong_or_eaforum(domain):
        title, text, author = _extract_forum_post(html, domain)
        method = "lw/eaf"

    # ── Layer 2: Structured data ──────────────────────────────────
    if not text:
        title_sd, text_sd = _extract_structured_data(html)
        if text_sd and len(text_sd) > 200:
            title = title_sd or title
            text = text_sd
            method = "json-ld/opengraph"

    # ── Layer 3: Trafilatura ──────────────────────────────────────
    if not text and HAS_TRAFILATURA:
        title_tr, text_tr = _extract_trafilatura(html, url)
        if text_tr:
            title = title_tr or title
            text = text_tr
            method = "trafilatura"

    # ── Layer 4: BeautifulSoup fallback ───────────────────────────
    if not text and HAS_BS4:
        title_bs, text_bs = _extract_beautifulsoup(html)
        if text_bs:
            title = title_bs or title
            text = text_bs
            method = "beautifulsoup"

    # ── Last resort: at least get a title ─────────────────────────
    if not title:
        title = _extract_title_from_html(html)

    if not text:
        return FetchResult(url=url, title=title, success=False,
                           error="Could not extract text content")

    # Extract author from meta tags if we don't have one yet
    if not author:
        author = _extract_author(html)

    # Truncate to configured max
    if len(text) > config.MAX_ARTICLE_CHARS:
        text = text[:config.MAX_ARTICLE_CHARS]

    # Clean the text for audio narration
    audio_text = clean_text_for_audio(text, title=title, author=author)

    result = FetchResult(
        url=url, title=title, text=text, audio_text=audio_text,
        author=author, success=True, extraction_method=method
    )
    logger.info(f"  Fetched [{method}]: {title!r} "
                f"({result.word_count} words, ~{result.estimated_minutes} min)")
    return result


# ── Audio text cleaning ────────────────────────────────────────────────

def clean_text_for_audio(text: str, title: str = "", author: str = "") -> str:
    """
    Transform extracted article text into prose optimized for TTS narration.

    This is where listenability is made or broken. Raw web text is full of
    things that sound terrible when read aloud: footnote markers like [1],
    URLs, image captions, code blocks, markdown artifacts, etc.
    """
    # Build an intro line
    parts = []
    if title:
        intro = f"{title}."
        if author:
            intro = f"{title}, by {author}."
        parts.append(intro)
        parts.append("")  # blank line = pause

    # Start cleaning the body text
    body = text

    # Remove code blocks (fenced and indented)
    body = re.sub(r'```[\s\S]*?```', ' [code block omitted] ', body)
    body = re.sub(r'~~~[\s\S]*?~~~', ' [code block omitted] ', body)

    # Remove inline code
    body = re.sub(r'`[^`]+`', lambda m: m.group(0).strip('`'), body)

    # Remove footnote/citation markers: [1], [2,3], [note 1], etc.
    body = re.sub(r'\[(\d+(?:,\s*\d+)*)\]', '', body)
    body = re.sub(r'\[(?:note|citation|ref)\s*\d*\]', '', body, flags=re.IGNORECASE)

    # Remove superscript-style footnote markers that survive extraction
    body = re.sub(r'(?<=[a-zA-Z.,;:!?])(\d{1,3})(?=\s|[.,;:!?\)])', '', body)

    # Remove bare URLs (they sound awful when narrated)
    body = re.sub(
        r'https?://[^\s\)<\]]+',
        ' [link] ',
        body
    )

    # Remove markdown link syntax, keeping just the link text
    body = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', body)

    # Remove image references / captions that look like: ![alt text](url)
    body = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', '', body)

    # Remove HTML tags that survived extraction
    body = re.sub(r'<[^>]+>', '', body)

    # Handle markdown headings — convert to spoken section markers
    def heading_to_speech(match):
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        if level <= 2:
            return f"\n\n{heading_text}.\n\n"
        else:
            return f"\n{heading_text}.\n"

    body = re.sub(r'^(#{1,6})\s+(.+)$', heading_to_speech, body, flags=re.MULTILINE)

    # Remove horizontal rules
    body = re.sub(r'^[\-\*_]{3,}\s*$', '\n', body, flags=re.MULTILINE)

    # Remove markdown bold/italic markers but keep the text
    body = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', body)
    body = re.sub(r'\*\*(.+?)\*\*', r'\1', body)
    body = re.sub(r'\*(.+?)\*', r'\1', body)
    body = re.sub(r'___(.+?)___', r'\1', body)
    body = re.sub(r'__(.+?)__', r'\1', body)
    body = re.sub(r'_(.+?)_', r'\1', body)

    # Remove markdown bullet points — convert to flowing prose
    body = re.sub(r'^\s*[\-\*\+]\s+', '', body, flags=re.MULTILINE)

    # Remove numbered list markers
    body = re.sub(r'^\s*\d+[\.\)]\s+', '', body, flags=re.MULTILINE)

    # Remove blockquote markers
    body = re.sub(r'^\s*>\s*', '', body, flags=re.MULTILINE)

    # Remove table-like content (lines with multiple | separators)
    body = re.sub(r'^.*\|.*\|.*$', '', body, flags=re.MULTILINE)
    # Remove table separator rows
    body = re.sub(r'^[\s\-\|:]+$', '', body, flags=re.MULTILINE)

    # Collapse excessive whitespace
    body = re.sub(r'\n{3,}', '\n\n', body)
    body = re.sub(r' {2,}', ' ', body)

    # Clean up lines
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")  # preserve paragraph breaks

    body = "\n".join(lines)

    parts.append(body)
    return "\n".join(parts).strip()


# ── Download ───────────────────────────────────────────────────────────

class _DownloadResponse:
    """Unified response wrapper."""
    def __init__(self, text: str, status_code: int, url: str):
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                response=type("R", (), {"status_code": self.status_code})()
            )


def _download(url: str) -> _DownloadResponse:
    """Download a URL, using curl_cffi if available for TLS impersonation."""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    if HAS_CURL_CFFI:
        resp = cffi_requests.get(
            url, headers=headers, timeout=config.FETCH_TIMEOUT,
            allow_redirects=True, impersonate="chrome",
        )
        result = _DownloadResponse(resp.text, resp.status_code, str(resp.url))
        result.raise_for_status()
        return result
    else:
        resp = requests.get(
            url, headers=headers, timeout=config.FETCH_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return _DownloadResponse(resp.text, resp.status_code, resp.url)


# ── Site detection ─────────────────────────────────────────────────────

def _is_youtube(domain: str) -> bool:
    return domain in ("youtube.com", "m.youtube.com", "youtu.be")


def _is_substack(html: str, domain: str) -> bool:
    if "substack.com" in domain:
        return True
    if "__NEXT_DATA__" in html and ("substack" in html.lower()[:5000]
                                     or '"pub"' in html[:5000]):
        return True
    return False


def _is_lesswrong_or_eaforum(domain: str) -> bool:
    return domain in (
        "lesswrong.com", "forum.effectivealtruism.org",
        "ea.greaterwrong.com", "greaterwrong.com",
    )


# ── Site-specific extractors ──────────────────────────────────────────

def _extract_youtube(html: str) -> tuple[str, str]:
    """Extract video title and description from YouTube."""
    title, description = "", ""
    try:
        marker = "var ytInitialPlayerResponse = "
        idx = html.find(marker)
        if idx >= 0:
            decoder = json.JSONDecoder()
            player_data, _ = decoder.raw_decode(html, idx + len(marker))
            vd = player_data.get("videoDetails", {})
            title = vd.get("title", "")
            description = vd.get("shortDescription", "")
    except Exception:
        pass

    if not title:
        og = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"',
                        html, re.IGNORECASE)
        if og:
            title = unescape(og.group(1))

    if not description:
        og = re.search(
            r'<meta\s+(?:property="og:description"|name="description")\s+content="([^"]*)"',
            html, re.IGNORECASE
        )
        if og:
            description = unescape(og.group(1))

    text = f"{title}\n\n{description}" if title and description else (title or description)
    return title, text


def _extract_substack(html: str) -> tuple[str, str]:
    """Extract post content from Substack's __NEXT_DATA__."""
    try:
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>',
            html, re.DOTALL
        )
        if not match:
            return "", ""

        data = json.loads(match.group(1))
        props = data.get("props", {}).get("pageProps", {})
        post = props.get("post")
        if not post and "initialState" in props:
            posts = props["initialState"].get("post", {}).get("posts", {})
            if posts:
                post = next(iter(posts.values()), None)
        if not post:
            return "", ""

        title = post.get("title", "")
        subtitle = post.get("subtitle", "")
        body_html = post.get("body_html", "") or post.get("body", "")

        if body_html and HAS_BS4:
            soup = BeautifulSoup(body_html, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
        elif body_html:
            text = re.sub(r"<[^>]+>", " ", body_html)
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = post.get("truncated_body_text", "")

        if subtitle and text:
            text = f"{subtitle}\n\n{text}"

        return title, text
    except Exception as e:
        logger.debug(f"Substack extraction failed: {e}")
        return "", ""


def _extract_forum_post(html: str, domain: str) -> tuple[str, str, str]:
    """
    Extract title, body text, and author from LessWrong / EA Forum posts.

    These sites use a React SPA, but the HTML includes enough meta tags
    and JSON-LD to get the content without JS rendering.
    """
    title, text, author = "", "", ""

    # JSON-LD often has the article body
    try:
        ld_blocks = re.findall(
            r'<script\s+type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL
        )
        for block in ld_blocks:
            try:
                ld = json.loads(block)
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") in ("Article", "BlogPosting", "DiscussionForumPosting"):
                        title = item.get("headline", "") or item.get("name", "")
                        text = item.get("articleBody", "") or item.get("text", "")
                        a = item.get("author", {})
                        if isinstance(a, dict):
                            author = a.get("name", "")
                        elif isinstance(a, list) and a:
                            author = a[0].get("name", "")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    # Fallback to meta tags
    if not title:
        og = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"',
                        html, re.IGNORECASE)
        if og:
            title = unescape(og.group(1))

    if not text:
        # Try trafilatura on the HTML
        if HAS_TRAFILATURA:
            _, text = _extract_trafilatura(html, "")

    if not author:
        author = _extract_author(html)

    return title, text, author


# ── Generic extractors ────────────────────────────────────────────────

def _extract_structured_data(html: str) -> tuple[str, str]:
    """Extract from JSON-LD and OpenGraph meta tags."""
    title, description = "", ""

    try:
        ld_blocks = re.findall(
            r'<script\s+type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL
        )
        for block in ld_blocks:
            try:
                ld = json.loads(block)
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if not title:
                        title = item.get("headline", "") or item.get("name", "")
                    body = (item.get("articleBody", "")
                            or item.get("text", "")
                            or item.get("description", ""))
                    if body and len(body) > len(description):
                        description = body
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    if not title:
        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"',
                       html, re.IGNORECASE)
        if m:
            title = unescape(m.group(1))

    if not description or len(description) < 200:
        for pattern in [
            r'<meta\s+property="og:description"\s+content="([^"]*)"',
            r'<meta\s+name="description"\s+content="([^"]*)"',
        ]:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                candidate = unescape(m.group(1))
                if len(candidate) > len(description):
                    description = candidate

    return title, description


def _extract_trafilatura(html: str, url: str) -> tuple[str, str]:
    """Extract using trafilatura."""
    try:
        metadata = trafilatura.extract_metadata(html)
        title = metadata.title if metadata and metadata.title else ""
        text = trafilatura.extract(
            html, url=url, include_comments=False,
            include_tables=False,  # tables are useless in audio
            favor_recall=True,
        )
        return title, text or ""
    except Exception as e:
        logger.debug(f"Trafilatura failed: {e}")
        return "", ""


def _extract_beautifulsoup(html: str) -> tuple[str, str]:
    """Fallback: extract visible text using BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = ""
        for selector in ["article", "main", '[role="main"]',
                         ".post-content", ".entry-content", ".article-body"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                break

        if not text:
            body = soup.find("body")
            if body:
                text = body.get_text(separator="\n", strip=True)

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        return title, text
    except Exception:
        return "", ""


# ── Utility ────────────────────────────────────────────────────────────

def _extract_title_from_html(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return unescape(m.group(1).strip()) if m else ""


def _extract_author(html: str) -> str:
    """Try to extract author name from meta tags."""
    for pattern in [
        r'<meta\s+name="author"\s+content="([^"]*)"',
        r'<meta\s+property="article:author"\s+content="([^"]*)"',
        r'<meta\s+name="citation_author"\s+content="([^"]*)"',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return unescape(m.group(1))
    return ""


# ── Batch fetch ────────────────────────────────────────────────────────

def fetch_urls(urls: list[str], progress_callback=None) -> list[FetchResult]:
    """Fetch multiple URLs with optional progress callback."""
    results = []
    total = len(urls)

    for i, url in enumerate(urls):
        logger.info(f"Fetching [{i+1}/{total}]: {url}")
        result = fetch_url(url)
        if not result.success:
            logger.warning(f"  Failed: {result.error}")
        results.append(result)
        if progress_callback:
            progress_callback(i + 1, total, result)

    return results

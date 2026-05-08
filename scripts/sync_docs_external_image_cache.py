#!/usr/bin/env python3
"""Sync external image URLs from markdown docs into a local cache.

Reads an existing ``docs-external-image-cache/manifest.json`` and only downloads images
whose URLs are not already tracked (either successfully cached or previously
recorded as errors). New entries are appended and the manifest is rewritten
with stable ordering so diffs stay small.

Usage:
    python scripts/sync_docs_external_image_cache.py \
        --docs-root /path/to/vibewatch.github.io/docs \
        --cache-root /path/to/static/docs-external-image-cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

USER_AGENT = (
    "Mozilla/5.0 (compatible; vibewatch-docs-external-image-cache/1.0; "
    "+https://github.com/vibewatch/static)"
)
REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number

# Markdown ![alt](url "title")
MD_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\"|\s+'[^']*')?\s*\)"
)
# HTML <img ... src="url">
HTML_IMG_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)

CONTENT_TYPE_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/x-icon": "ico",
    "image/avif": "avif",
}


def iter_english_markdown_files(docs_root: Path) -> Iterable[Path]:
    """Yield .md files under ``docs_root`` excluding *.zh.md."""
    for path in sorted(docs_root.rglob("*.md")):
        name = path.name
        if name.endswith(".zh.md"):
            continue
        yield path


def extract_image_urls(markdown_text: str) -> list[str]:
    """Extract http(s) image URLs from markdown image syntax and <img> tags."""
    urls: list[str] = []
    for match in MD_IMAGE_RE.finditer(markdown_text):
        url = match.group(1).strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    for match in HTML_IMG_RE.finditer(markdown_text):
        url = match.group(1).strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def url_to_filename(url: str, ext: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{digest}.{ext}" if ext else digest


def guess_extension(url: str, content_type: str | None) -> str:
    # Prefer the URL's own extension when it's a recognized image type, so
    # filenames stay stable and consistent with the historical cache (e.g.
    # *.jpg URLs stay *.jpg rather than being normalized to *.jpeg).
    known_image_exts = {"png", "jpg", "jpeg", "gif", "webp", "svg",
                        "bmp", "tiff", "tif", "ico", "avif"}
    path = urllib.parse.urlparse(url).path
    if "." in path:
        candidate = path.rsplit(".", 1)[-1].lower()
        if candidate in known_image_exts:
            return candidate
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in CONTENT_TYPE_TO_EXT:
            return CONTENT_TYPE_TO_EXT[ct]
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            return guessed.lstrip(".")
    if "." in path:
        candidate = path.rsplit(".", 1)[-1].lower()
        if 1 <= len(candidate) <= 5 and candidate.isalnum():
            return candidate
    return "bin"


def download(url: str) -> tuple[bytes, str | None]:
    """Download URL with retries. Returns (body, content_type)."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type")
                return body, content_type
        except urllib.error.HTTPError as exc:
            # Don't retry client errors (4xx) — they won't recover.
            if 400 <= exc.code < 500:
                raise
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
        time.sleep(RETRY_BACKOFF * attempt)
    assert last_exc is not None
    raise last_exc


def load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    return {
        "english_markdown_files": 0,
        "unique_image_urls": 0,
        "downloaded_or_cached": 0,
        "failed": 0,
        "entries": [],
        "errors": [],
    }


def save_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", required=True, type=Path,
                        help="Path to docs/ folder of source repo (english + zh).")
    parser.add_argument("--cache-root", required=True, type=Path,
                        help="Path to docs-external-image-cache/ folder in this repo.")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="Path to manifest.json (defaults to <cache-root>/manifest.json).")
    parser.add_argument("--files-subdir", default="files",
                        help="Subdirectory under cache-root for blob files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report but do not download or write.")
    args = parser.parse_args()

    docs_root: Path = args.docs_root.resolve()
    cache_root: Path = args.cache_root.resolve()
    manifest_path: Path = (args.manifest or (cache_root / "manifest.json")).resolve()
    files_dir: Path = (cache_root / args.files_subdir).resolve()

    if not docs_root.is_dir():
        print(f"error: docs-root not found: {docs_root}", file=sys.stderr)
        return 2

    manifest = load_manifest(manifest_path)
    existing_entries: list[dict] = list(manifest.get("entries", []))
    existing_errors: list[dict] = list(manifest.get("errors", []))

    # Build a quick index: url -> entry (so we can update source_docs).
    entries_by_url: dict[str, dict] = {e["url"]: e for e in existing_entries}
    errors_by_url: dict[str, dict] = {e["url"]: e for e in existing_errors}

    # Discover english markdown files and extract URLs.
    md_files = list(iter_english_markdown_files(docs_root))
    print(f"scanning {len(md_files)} english markdown files under {docs_root}")

    # url -> sorted list of relative source doc paths
    url_sources: dict[str, set[str]] = {}
    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        rel = md_path.relative_to(docs_root).as_posix()
        for url in extract_image_urls(text):
            url_sources.setdefault(url, set()).add(rel)

    print(f"found {len(url_sources)} unique image URLs")

    new_urls = [
        u for u in url_sources
        if u not in entries_by_url and u not in errors_by_url
    ]
    print(f"{len(new_urls)} URLs are new and need downloading")

    if args.dry_run:
        for url in new_urls[:20]:
            print(f"  would download: {url}")
        if len(new_urls) > 20:
            print(f"  ... and {len(new_urls) - 20} more")
        return 0

    files_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    failed = 0
    for index, url in enumerate(sorted(new_urls), start=1):
        sources = sorted(url_sources[url])
        try:
            body, content_type = download(url)
        except urllib.error.HTTPError as exc:
            print(f"[{index}/{len(new_urls)}] FAIL HTTP {exc.code} {url}")
            errors_by_url[url] = {
                "url": url,
                "status": "error",
                "error": f"HTTP {exc.code}",
            }
            failed += 1
            continue
        except Exception as exc:  # network/other
            print(f"[{index}/{len(new_urls)}] FAIL {type(exc).__name__} {url}: {exc}")
            errors_by_url[url] = {
                "url": url,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            failed += 1
            continue

        ext = guess_extension(url, content_type)
        filename = url_to_filename(url, ext)
        out_path = files_dir / filename
        out_path.write_bytes(body)
        sha256 = hashlib.sha256(body).hexdigest()
        entry = {
            "url": url,
            "cache_path": f"{args.files_subdir}/{filename}",
            "content_type": (content_type or "").split(";", 1)[0].strip() or None,
            "size": len(body),
            "sha256": sha256,
            "source_docs": sources,
        }
        # Drop None content_type to match existing schema style.
        if entry["content_type"] is None:
            entry.pop("content_type")
        entries_by_url[url] = entry
        added += 1
        print(f"[{index}/{len(new_urls)}] OK {len(body):>8}B {url}")

    # Refresh source_docs for previously-cached URLs that still appear.
    for url, sources in url_sources.items():
        if url in entries_by_url and url not in new_urls:
            entry = entries_by_url[url]
            existing_sources = set(entry.get("source_docs", []))
            merged = sorted(existing_sources | sources)
            if merged != entry.get("source_docs"):
                entry["source_docs"] = merged

    # Rebuild manifest with stable ordering.
    sorted_entries = sorted(entries_by_url.values(), key=lambda e: e["url"])
    sorted_errors = sorted(errors_by_url.values(), key=lambda e: e["url"])

    manifest = {
        "english_markdown_files": len(md_files),
        "unique_image_urls": len(url_sources),
        "downloaded_or_cached": len(sorted_entries),
        "failed": len(sorted_errors),
        "entries": sorted_entries,
        "errors": sorted_errors,
    }
    save_manifest(manifest_path, manifest)

    print(
        f"done: added={added} failed={failed} "
        f"total_entries={len(sorted_entries)} total_errors={len(sorted_errors)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

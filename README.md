# vibewatch / static

Static asset mirror for [vibewatch.github.io](https://github.com/vibewatch/vibewatch.github.io).

The source site links to many third‑party images (Reddit, Twitter/X, etc.) that
are unstable: posts get deleted, hotlink protection changes, CDNs return 403/404
without warning. This repo keeps a content‑addressed cache of every image
referenced by the English docs so the site can fall back to a stable copy.

## Layout

```
docs-external-image-cache/
  manifest.json   # index of every URL we've seen, success or failure
  files/          # downloaded blobs, named <sha256(url)>.<ext>
scripts/
  sync_docs_external_image_cache.py
.github/workflows/
  sync-docs-external-image-cache.yml
```

## How it works

[scripts/sync_docs_external_image_cache.py](scripts/sync_docs_external_image_cache.py):

1. Walks `<docs-root>/**/*.md` and **skips any `*.zh.md`** (English docs only).
2. Extracts image URLs from both Markdown (`![alt](url)`) and HTML (`<img src="…">`).
3. Looks each URL up in [docs-external-image-cache/manifest.json](docs-external-image-cache/manifest.json).
   URLs already recorded (success **or** prior error) are skipped — this is the
   core "only add what isn't cached" guarantee.
4. Downloads each new URL with retries, writes the blob to
  `docs-external-image-cache/files/<sha256(url)>.<ext>`, and appends an entry to the
   manifest. Failures are recorded in `errors[]` so they aren't retried every run.
5. Refreshes `source_docs[]` on existing entries when new docs reference an
   already‑cached image.

Filename scheme:

- **Path**: `files/<sha256-of-url>.<ext>` — content‑addressed by URL, so the
  same URL always maps to the same file.
- **Extension**: prefer the URL's own extension when it's a known image type
  (`png`, `jpg`, `jpeg`, `gif`, `webp`, `svg`, `bmp`, `tiff`, `ico`, `avif`),
  otherwise fall back to `Content-Type`. This keeps `.jpg` URLs as `.jpg`
  rather than normalizing them to `.jpeg`.

## Manifest schema

```jsonc
{
  "english_markdown_files": 193, // count of *.md (excluding *.zh.md)
  "unique_image_urls": 797,      // distinct URLs found
  "downloaded_or_cached": 789,   // entries.length
  "failed": 8,                   // errors.length
  "entries": [                   // sorted by url
    {
      "url": "https://i.redd.it/example.png",
      "cache_path": "files/<sha256>.png",
      "content_type": "image/png",
      "size": 85074,
      "sha256": "<sha256-of-bytes>",
      "source_docs": ["reddit/ai-coding/2026-04-24.md"]
    }
  ],
  "errors": [                    // sorted by url
    {
      "url": "https://i.redd.it/gone.jpeg",
      "status": "error",
      "error": "HTTP 404"
    }
  ]
}
```

## Running locally

Requires Python 3.10+ (standard library only — no `pip install` needed).

```bash
# Dry run: report what would be downloaded, change nothing.
python scripts/sync_docs_external_image_cache.py \
  --docs-root /path/to/vibewatch.github.io/docs \
  --cache-root ./docs-external-image-cache \
  --dry-run

# Real run.
python scripts/sync_docs_external_image_cache.py \
  --docs-root /path/to/vibewatch.github.io/docs \
  --cache-root ./docs-external-image-cache
```

Flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--docs-root` | _required_ | Root of the source repo's `docs/` folder. |
| `--cache-root` | _required_ | This repo's `docs-external-image-cache/`. |
| `--manifest` | `<cache-root>/manifest.json` | Override manifest path. |
| `--files-subdir` | `files` | Where blobs are written under `cache-root`. |
| `--dry-run` | off | Scan and report; do not download or write. |

## Automation

[.github/workflows/sync-docs-external-image-cache.yml](.github/workflows/sync-docs-external-image-cache.yml)
runs on:

- `workflow_dispatch` — manual trigger from the Actions tab.
- `schedule` — daily at `06:00 UTC`.
- `push` to `main` that touches the script or workflow itself.

The job checks out both repos, runs the sync script against
`source/docs → static/docs-external-image-cache`, and commits + pushes only if the diff
is non‑empty. Manual runs can override the source repo ref and enable dry-run
mode. A `concurrency` group prevents overlapping runs.

## Adding a new image source

The URL extractor handles standard Markdown image syntax and `<img src="…">`
tags out of the box. If the source repo starts using a different image syntax
(e.g. a custom shortcode), update `MD_IMAGE_RE` / `HTML_IMG_RE` in
[scripts/sync_docs_external_image_cache.py](scripts/sync_docs_external_image_cache.py).

## Troubleshooting

- **An image keeps failing.** It's recorded in `errors[]` and won't be retried.
  Delete its entry from `manifest.json` to force another attempt on the next
  run.
- **An image needs to be re‑downloaded.** Delete both the `entries[]` entry
  and the blob under `files/`; the next run will re‑fetch.
- **Want to verify the cache.** Each entry has `sha256` (of the file bytes);
  `sha256sum docs-external-image-cache/files/<name>` should match.

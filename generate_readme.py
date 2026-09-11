#!/usr/bin/env python3
"""
Hytale FOSS Mod List Generator
Retrieves all mods for Hytale from CurseForge and generates a README.md list
containing all Free and Open Source (FOSS) mods.
"""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from urllib.parse import urlparse
import urllib.request
import urllib.error

# Default CurseForge Core API configuration
BASE_URL = "https://api.curseforge.com/v1"
HYTALE_GAME_ID = 70216
DEFAULT_PAGE_SIZE = 50
DEFAULT_WORKERS = 8

# Public community API key used by tools like Prism Launcher and Ferium.
# Can be overridden via CURSEFORGE_API_KEY environment variable or --api-key CLI flag.
DEFAULT_API_KEY = "$2a$10$bL4bIL5pUWqfcO7KQtnMReakwtfHbNKh6v1uTpKlzhwoueEJQnPnm"

# Non-code repository domains often erroneously placed in the 'sourceUrl' field
DISALLOWED_DOMAINS = {
    "discord.gg",
    "discord.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "twitch.tv",
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "7-zip.org",
    "crowdin.com",
    "wiki.hytalemodding.dev",
    "curseforge.com",
    "reddit.com",
}

# Category name beautification
CATEGORY_NAME_MAP = {
    "Mobs\\Characters": "Mobs & Characters",
    "Food\\Farming": "Food & Farming",
    "Cosmetics\\Armor": "Cosmetics & Armor",
    "World Gen": "World Generation",
    "QoL": "Quality of Life",
}


def sanitize_markdown(text: str) -> str:
    """Sanitize text for inclusion inside a Markdown table cell."""
    if not text:
        return ""
    # Collapse newlines and tabs into single spaces
    clean = re.sub(r"\s+", " ", text).strip()
    # Escape pipe characters which break markdown tables
    clean = clean.replace("|", "\\|")
    return clean


def truncate_text(text: str, max_length: int = 140) -> str:
    """Truncate text with ellipsis if it exceeds max_length."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def identify_forge(url: str) -> str:
    """Identify the code forge name for a given source repository URL."""
    url_lower = url.lower()
    if "github.com" in url_lower:
        return "GitHub"
    elif "gitlab.com" in url_lower or "gitlab." in url_lower:
        return "GitLab"
    elif "codeberg.org" in url_lower:
        return "Codeberg"
    elif "bitbucket.org" in url_lower:
        return "Bitbucket"
    elif "sourceforge.net" in url_lower:
        return "SourceForge"
    elif "git." in url_lower or "gitea" in url_lower or "forgejo" in url_lower:
        return "Git"
    return "Source"


def is_valid_source_url(url: str) -> bool:
    """Check if the provided URL represents a valid open source repository."""
    if not url:
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False

    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        if netloc in DISALLOWED_DOMAINS:
            return False

        # Reject top-level domain only without repository path (e.g. https://github.com)
        path = parsed.path.strip("/")
        if not path and netloc in {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"}:
            return False

        return True
    except Exception:
        return False


class CurseForgeClient:
    def __init__(self, api_key: str, verbose: bool = False):
        self.api_key = api_key
        self.verbose = verbose

    def _make_request(self, endpoint: str, max_retries: int = 4) -> dict:
        url = f"{BASE_URL}{endpoint}"
        headers = {
            "x-api-key": self.api_key,
            "User-Agent": "HytaleFossModsList/1.0 (+https://github.com)",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, headers=headers)

        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read().decode("utf-8")
                    return json.loads(data)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** (attempt + 1)
                    if self.verbose:
                        print(f"[!] Rate limited (429). Retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                elif e.code >= 500:
                    wait = 2 ** attempt
                    if self.verbose:
                        print(f"[!] Server error ({e.code}). Retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                else:
                    if self.verbose:
                        print(f"[!] HTTP error {e.code} fetching {url}: {e.reason}", file=sys.stderr)
                    raise
            except Exception as e:
                wait = 2 ** attempt
                if self.verbose:
                    print(f"[!] Connection error: {e}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts.")

    def fetch_categories(self, game_id: int) -> dict[int, str]:
        """Fetch all categories for the specified game."""
        res = self._make_request(f"/categories?gameId={game_id}")
        categories = {}
        for cat in res.get("data", []):
            cat_id = cat["id"]
            name = cat["name"]
            beautified = CATEGORY_NAME_MAP.get(name, name)
            categories[cat_id] = beautified
        return categories

    def fetch_all_mods(self, game_id: int, workers: int = DEFAULT_WORKERS) -> tuple[list[dict], int]:
        """Retrieve all mods for the game using parallel pagination."""
        first_page = self._make_request(f"/mods/search?gameId={game_id}&index=0&pageSize={DEFAULT_PAGE_SIZE}")
        pagination = first_page.get("pagination", {})
        total_count = pagination.get("totalCount", 0)
        all_mods = list(first_page.get("data", []))

        if self.verbose:
            print(f"[*] Found {total_count} total projects on CurseForge for Hytale.", file=sys.stderr)

        indices = list(range(DEFAULT_PAGE_SIZE, total_count, DEFAULT_PAGE_SIZE))
        if not indices:
            return all_mods, total_count

        def fetch_index(idx: int) -> list[dict]:
            endpoint = f"/mods/search?gameId={game_id}&index={idx}&pageSize={DEFAULT_PAGE_SIZE}"
            res = self._make_request(endpoint)
            return res.get("data", [])

        completed = 0
        total_pages = len(indices)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {executor.submit(fetch_index, idx): idx for idx in indices}
            for future in concurrent.futures.as_completed(future_to_idx):
                completed += 1
                if self.verbose:
                    print(f"\r[*] Progress: {completed}/{total_pages} pages fetched...", end="", file=sys.stderr)
                page_data = future.result()
                all_mods.extend(page_data)

        if self.verbose:
            print(f"\n[*] Finished fetching {len(all_mods)} mods.", file=sys.stderr)

        unique_mods = {m["id"]: m for m in all_mods}
        return list(unique_mods.values()), total_count


def process_mods(raw_mods: list[dict], categories: dict[int, str], min_downloads: int = 0) -> list[dict]:
    """Filter and enrich open source mods."""
    foss_mods = []

    for mod in raw_mods:
        links = mod.get("links") or {}
        source_url = (links.get("sourceUrl") or "").strip()

        if not is_valid_source_url(source_url):
            continue

        download_count = mod.get("downloadCount", 0)
        if download_count < min_downloads:
            continue

        primary_cat_id = mod.get("primaryCategoryId")
        primary_cat_name = categories.get(primary_cat_id, "Miscellaneous")

        authors = mod.get("authors") or []
        author_names = [a.get("name", "Unknown") for a in authors]
        author_str = ", ".join(author_names) if author_names else "Unknown"

        # Author profile link if available
        first_author_url = authors[0].get("url") if authors else None
        if first_author_url and len(author_names) == 1:
            author_md = f"[{author_names[0]}]({first_author_url})"
        else:
            author_md = author_str

        date_modified = mod.get("dateModified", "")
        updated_str = date_modified[:10] if date_modified else "N/A"

        cf_url = links.get("websiteUrl") or f"https://www.curseforge.com/hytale/mods/{mod.get('slug')}"

        forge = identify_forge(source_url)
        summary = mod.get("summary") or ""

        foss_mods.append({
            "id": mod["id"],
            "name": mod.get("name", "Unnamed Mod"),
            "slug": mod.get("slug", ""),
            "summary": summary,
            "curseforge_url": cf_url,
            "source_url": source_url,
            "forge": forge,
            "authors": author_names,
            "author_md": author_md,
            "category": primary_cat_name,
            "download_count": download_count,
            "updated": updated_str,
            "date_modified": date_modified,
        })

    return foss_mods


def generate_markdown(
    foss_mods: list[dict],
    total_scanned: int,
    sort_by: str = "downloads",
    top_count: int = 15,
) -> str:
    """Build the README.md content with summary tables and category breakdown."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if sort_by == "name":
        key_fn = lambda m: m["name"].lower()
        reverse = False
    elif sort_by == "updated":
        key_fn = lambda m: m.get("date_modified", "")
        reverse = True
    else:  # 'downloads'
        key_fn = lambda m: m.get("download_count", 0)
        reverse = True

    foss_mods.sort(key=key_fn, reverse=reverse)

    categories_dict: dict[str, list[dict]] = {}
    for mod in foss_mods:
        cat = mod["category"]
        categories_dict.setdefault(cat, []).append(mod)

    sorted_categories = sorted(
        categories_dict.items(),
        key=lambda item: len(item[1]),
        reverse=True,
    )

    total_downloads = sum(m["download_count"] for m in foss_mods)

    lines = []
    lines.append("# Free and Open Source (FOSS) Hytale Mods\n")
    lines.append(
        "A curated and automatically updated directory of **Free and Open Source (FOSS)** "
        "mods, plugins, and tools for [Hytale](https://hytale.com) hosted on "
        "[CurseForge](https://www.curseforge.com/hytale).\n"
    )

    # Badges
    lines.append(
        f"[![FOSS Mods](https://img.shields.io/badge/FOSS%20Mods-{len(foss_mods)}-brightgreen)](#categories) "
        f"[![Total Scanned](https://img.shields.io/badge/CurseForge%20Projects-{total_scanned}-blue)](https://www.curseforge.com/hytale) "
        f"[![Total Downloads](https://img.shields.io/badge/Total%20Downloads-{total_downloads:,}-orange)](#) "
        f"[![Last Updated](https://img.shields.io/badge/Last%20Updated-{now_utc.replace(' ', '%20')}-informational)](#)\n"
    )

    lines.append(
        "> **Note**: A mod is included here if the author has provided a verified public source repository "
        "(GitHub, GitLab, Codeberg, etc.) in their CurseForge project settings. "
        "Support the open source modding community by starring and contributing to their repositories!\n"
    )

    # Top Most Popular Mods
    if top_count > 0 and len(foss_mods) > 0:
        lines.append(f"## 🌟 Top {min(top_count, len(foss_mods))} Most Popular FOSS Mods\n")
        lines.append("| # | Mod | Description | Author | Source Code | Category | Downloads |")
        lines.append("| :-: | :--- | :--- | :--- | :---: | :---: | ---: |")

        top_mods = sorted(foss_mods, key=lambda m: m["download_count"], reverse=True)[:top_count]
        for idx, mod in enumerate(top_mods, 1):
            name_link = f"[{sanitize_markdown(mod['name'])}]({mod['curseforge_url']})"
            desc = sanitize_markdown(truncate_text(mod["summary"], 90))
            source_link = f"[{mod['forge']}]({mod['source_url']})"
            downloads_fmt = f"{mod['download_count']:,}"
            lines.append(
                f"| {idx} | {name_link} | {desc} | {mod['author_md']} | {source_link} | {mod['category']} | {downloads_fmt} |"
            )
        lines.append("\n---\n")

    # Table of Contents
    lines.append("## 📑 Categories\n")
    lines.append("Quickly jump to a category:\n")
    for cat_name, mod_list in sorted_categories:
        anchor = re.sub(r"[^a-z0-9_-]", "", cat_name.lower().replace(" ", "-").replace("&", ""))
        count_str = f"{len(mod_list)} mod" if len(mod_list) == 1 else f"{len(mod_list)} mods"
        lines.append(f"- [{cat_name}](#{anchor}) ({count_str})")
    lines.append("\n---\n")

    # Category Tables
    for cat_name, mod_list in sorted_categories:
        anchor = re.sub(r"[^a-z0-9_-]", "", cat_name.lower().replace(" ", "-").replace("&", ""))
        lines.append(f"## {cat_name}\n")
        count_str = "1 open source mod" if len(mod_list) == 1 else f"{len(mod_list)} open source mods"
        lines.append(f"*{count_str}*\n")
        lines.append("| Mod | Description | Author | Source | Downloads | Last Updated |")
        lines.append("| :--- | :--- | :--- | :---: | ---: | :---: |")

        mod_list.sort(key=key_fn, reverse=reverse)
        for mod in mod_list:
            name_link = f"[{sanitize_markdown(mod['name'])}]({mod['curseforge_url']})"
            desc = sanitize_markdown(truncate_text(mod["summary"], 120))
            source_link = f"[{mod['forge']}]({mod['source_url']})"
            downloads_fmt = f"{mod['download_count']:,}"
            lines.append(
                f"| {name_link} | {desc} | {mod['author_md']} | {source_link} | {downloads_fmt} | {mod['updated']} |"
            )
        lines.append("\n")

    lines.append("---\n")
    lines.append("## 🔄 Updating this List\n")
    lines.append(
        "This list is generated automatically using `generate_readme.py`. To refresh the list:\n\n"
        "```bash\n"
        "# Run with default settings (uses Python 3 standard library only, no external dependencies)\n"
        "python3 generate_readme.py\n\n"
        "# Optional: specify custom output file or export structured JSON\n"
        "python3 generate_readme.py --output README.md --json-output mods.json\n"
        "```\n"
    )

    lines.append("## 💡 How to Add Your Mod\n")
    lines.append(
        "If you are a Hytale mod author on CurseForge and want your mod to appear in this list:\n"
        "1. Go to your project on CurseForge and click **Edit Project**.\n"
        "2. Add your GitHub/GitLab/Codeberg repository link into the **Source Code** URL field.\n"
        "3. Once saved, run the updater script or wait for the next automated update!\n"
    )

    lines.append("## ⚖️ License & Disclaimer\n")
    lines.append(
        "- Each mod is governed by its author's own license (see their respective source repositories for details).\n"
        "- Hytale is a trademark or registered trademark of Hypixel Studios Inc.\n"
        "- CurseForge is a trademark or registered trademark of Overwolf Ltd.\n"
        "- This project is an independent community effort and is not affiliated with Hypixel Studios or Overwolf.\n"
    )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve Hytale mods from CurseForge and generate a FOSS README.md list."
    )
    parser.add_argument(
        "-o", "--output",
        default="README.md",
        help="Path to output Markdown file (default: README.md)",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Optional path to export structured JSON data (e.g. mods.json)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CURSEFORGE_API_KEY", DEFAULT_API_KEY),
        help="CurseForge Core API Key (defaults to built-in key or CURSEFORGE_API_KEY env var)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel worker threads for API queries (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--min-downloads",
        type=int,
        default=0,
        help="Minimum number of downloads required to include a mod (default: 0)",
    )
    parser.add_argument(
        "--sort",
        choices=["downloads", "name", "updated"],
        default="downloads",
        help="Sorting order within categories (default: downloads)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of top mods to feature in the highlight section (default: 15)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress progress and status messages",
    )

    args = parser.parse_args()
    verbose = not args.quiet

    if verbose:
        print(f"[*] Starting Hytale FOSS mod retrieval...", file=sys.stderr)

    client = CurseForgeClient(api_key=args.api_key, verbose=verbose)

    # 1. Fetch categories
    if verbose:
        print("[*] Fetching Hytale categories...", file=sys.stderr)
    categories = client.fetch_categories(HYTALE_GAME_ID)

    # 2. Fetch all mods
    if verbose:
        print(f"[*] Fetching all mods for Hytale (Game ID: {HYTALE_GAME_ID})...", file=sys.stderr)
    raw_mods, total_scanned = client.fetch_all_mods(HYTALE_GAME_ID, workers=args.workers)

    # 3. Filter open source mods
    foss_mods = process_mods(raw_mods, categories, min_downloads=args.min_downloads)
    if verbose:
        print(
            f"[*] Filtered {len(foss_mods)} open-source mods out of {total_scanned} total projects.",
            file=sys.stderr,
        )

    # 4. Generate README.md
    markdown_content = generate_markdown(
        foss_mods=foss_mods,
        total_scanned=total_scanned,
        sort_by=args.sort,
        top_count=args.top,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    if verbose:
        print(f"[✓] Successfully generated {args.output} ({len(foss_mods)} FOSS mods).", file=sys.stderr)

    # 5. Optional JSON export
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump({
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_scanned": total_scanned,
                "total_foss_mods": len(foss_mods),
                "mods": foss_mods,
            }, f, indent=2)
        if verbose:
            print(f"[✓] Exported structured data to {args.json_output}", file=sys.stderr)


if __name__ == "__main__":
    main()

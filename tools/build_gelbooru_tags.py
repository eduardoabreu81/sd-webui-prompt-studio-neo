#!/usr/bin/env python3
"""Build tags/gelbooru.csv from the public Gelbooru tag snapshots.

The Gelbooru tag API does not return aliases, so the list is assembled from two
independent sources:

  * base tags  - DraconicDragon/dbr-e621-lists-archive, the same upstream that
                 already produces our danbooru_e621_merged.csv. The "pt20" file
                 is the full Gelbooru dump cut at post_count >= 20 with the
                 deprecated category (6) removed.
  * aliases    - deepghs/site_tags, gelbooru.com/tag_aliases.csv (CC-BY-4.0,
                 attribution required - see the Credits section of README.md).

Note on ambiguous tags: we deliberately use the "incl_ambiguous" variant.
Gelbooru's `ambiguous` flag does not mean "low quality" - it is set on much of
the core vocabulary, including long_hair (the second most used tag on the
site), black_hair, gloves, red_eyes and 2girls. Excluding it produces a list
that is unusable for prompting.

Output format matches the other files in tags/: no header, one row per tag,
`name,category,post_count,aliases` where aliases is a comma-separated list
quoted by the csv writer when needed.

Usage:
    python tools/build_gelbooru_tags.py
    python tools/build_gelbooru_tags.py --offline   # reuse the cached downloads
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "tags" / "gelbooru.csv"
DEFAULT_CACHE = REPO_ROOT / "tools" / ".cache"

BASE_TAGS_URL = (
    "https://raw.githubusercontent.com/DraconicDragon/dbr-e621-lists-archive"
    "/main/tag-lists/gelbooru/gelbooru_tags_2026-06-11_pt20_incl_ambiguous.csv"
)
ALIASES_URL = (
    "https://huggingface.co/datasets/deepghs/site_tags"
    "/resolve/main/gelbooru.com/tag_aliases.csv"
)

# A tag containing either character cannot round-trip through a prompt: a comma
# splits it into two tags, and a double quote breaks CSV parsing downstream.
UNUSABLE_IN_ALIAS = (",", '"')

# Gelbooru category 6. The base snapshot already excludes it; this is a guard in
# case a future snapshot stops doing so.
CATEGORY_DEPRECATED = "6"

DOWNLOAD_TIMEOUT_SECONDS = 120
USER_AGENT = "sd-webui-prompt-studio-neo/build_gelbooru_tags"


def fetch(url: str, destination: Path, offline: bool) -> Path:
    """Download `url` to `destination`, reusing the file if already cached."""
    if destination.exists():
        print(f"  cache: {destination.name} ({destination.stat().st_size / 1048576:.1f} MB)")
        return destination
    if offline:
        raise SystemExit(f"--offline was given but {destination} is not cached yet")

    print(f"  baixando: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            payload = response.read()
    except OSError as error:
        raise SystemExit(f"falha ao baixar {url}: {error}") from error

    # Write via a temporary file so an interrupted run never leaves a truncated
    # cache entry behind.
    staging = destination.with_suffix(destination.suffix + ".part")
    staging.write_bytes(payload)
    staging.replace(destination)
    print(f"  salvo: {destination.name} ({len(payload) / 1048576:.1f} MB)")
    return destination


def load_base_tags(path: Path) -> list[tuple[str, str, str]]:
    """Read the base snapshot as (name, category, post_count) rows.

    Gelbooru hands out separate tag ids for a handful of names carrying special
    characters (apostrophes, ampersands, faces like `>_<`), so the snapshot
    contains a few duplicated names with conflicting categories and counts. The
    row with the higher post count is consistently the real tag - the other is a
    near-empty stub, usually miscategorised as general. Keeping both would show
    the same tag twice in the autocomplete, so we keep only the higher one.
    """
    best: dict[str, tuple[str, str, str]] = {}
    skipped_deprecated = 0
    deduplicated = 0

    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.reader(handle):
            if len(record) < 3:
                continue
            name, category, post_count = record[0].strip(), record[1].strip(), record[2].strip()
            if not name or not post_count.isdigit():
                continue
            if category == CATEGORY_DEPRECATED:
                skipped_deprecated += 1
                continue

            previous = best.get(name)
            if previous is None:
                best[name] = (name, category, post_count)
                continue

            deduplicated += 1
            if int(post_count) > int(previous[2]):
                best[name] = (name, category, post_count)

    if skipped_deprecated:
        print(f"  ignoradas {skipped_deprecated} tags depreciadas (categoria 6)")
    if deduplicated:
        print(f"  {deduplicated} linhas duplicadas resolvidas pela maior contagem")

    return sorted(best.values(), key=lambda row: -int(row[2]))


def load_aliases(path: Path) -> tuple[dict[str, list[str]], int]:
    """Read alias->tag pairs into a tag->[aliases] map, dropping unusable ones."""
    by_tag: dict[str, list[str]] = {}
    dropped = 0

    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            alias = (record.get("alias") or "").strip()
            tag = (record.get("tag") or "").strip()
            if not alias or not tag:
                continue
            if any(character in alias for character in UNUSABLE_IN_ALIAS):
                dropped += 1
                continue
            by_tag.setdefault(tag, []).append(alias)

    for aliases in by_tag.values():
        aliases.sort()
    return by_tag, dropped


def write_list(rows: list[tuple[str, str, str]], aliases: dict[str, list[str]], output: Path) -> int:
    """Write the final CSV and return how many tags received at least one alias."""
    matched = 0
    output.parent.mkdir(parents=True, exist_ok=True)

    # newline="" keeps the csv module in charge of line endings, so the file does
    # not pick up CRLF on Windows and differ from the other lists in tags/.
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        for name, category, post_count in rows:
            found = aliases.get(name, [])
            if found:
                matched += 1
            writer.writerow([name, category, post_count, ",".join(found)])

    return matched


def display_path(path: Path) -> str:
    """Path relative to the repo when it lives inside it, absolute otherwise."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="caminho do CSV gerado")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="onde guardar os downloads")
    parser.add_argument("--offline", action="store_true", help="usar apenas o que já está em cache")
    args = parser.parse_args()

    print("Fontes:")
    base_path = fetch(BASE_TAGS_URL, args.cache_dir / "gelbooru_base.csv", args.offline)
    alias_path = fetch(ALIASES_URL, args.cache_dir / "gelbooru_aliases.csv", args.offline)

    print("\nProcessando:")
    rows = load_base_tags(base_path)
    print(f"  {len(rows)} tags na base")

    aliases, dropped = load_aliases(alias_path)
    alias_count = sum(len(values) for values in aliases.values())
    print(f"  {alias_count} aliases utilizáveis ({dropped} descartados por vírgula ou aspas)")

    matched = write_list(rows, aliases, args.output)

    size_mb = args.output.stat().st_size / 1048576
    shown = display_path(args.output)
    print(f"\nGerado: {shown}")
    print(f"  {len(rows)} tags · {matched} com alias · {size_mb:.1f} MB")
    print(f"\nO diretório tags/ tem .gitignore com '*' — use: git add -f {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

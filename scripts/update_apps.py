#!/usr/bin/env python3
"""Update ArtCraft WebAssembly apps only when their releases change."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
APPS_DIR = PUBLIC / "apps"
MANIFEST_PATH = PUBLIC / "apps.json"
STATE_PATH = PUBLIC / "apps-state.json"
CHANGE_FLAG_PATH = ROOT / ".apps-changed"

OWNER = os.environ.get("ARTCRAFT_REPO_OWNER", "storytold")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
API_BASE = "https://api.github.com"

# id, display name, repository, release asset prefix
APP_DEFINITIONS = [
    ("photocraft", "PhotoCraft", "photocraft", "photocraft-web-"),
    ("vectorcraft", "VectorCraft", "vectorcraft", "vectorcraft-web-"),
    ("filmcraft", "FilmCraft", "filmcraft", "filmcraft-web-"),
    ("lightcraft", "LightCraft", "lightcraft", "lightcraft-web-"),
    ("pdfcraft", "PDFCraft", "pdfcraft", "pdfcraft-web-"),
    ("effectcraft", "EffectCraft", "effectcraft", "effectcraft-web-"),
    ("designcraft", "DesignCraft", "designcraft", "designcraft-web-"),
    ("figcraft", "FigCraft", "figcraft", "figcraft-web-"),
    ("wordcraft", "WordCraft", "wordcraft", "wordcraft-web-"),
    ("deckcraft", "DeckCraft", "deckcraft", "deckcraft-web-"),
    ("cadcraft", "CADCraft", "cadcraft", "cadcraft-web-"),
    ("gridcraft", "GridCraft", "gridcraft", "gridcraft-web-"),
    ("soundcraft", "SoundCraft", "soundcraft", "soundcraft-web-"),
]


def request_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
            "User-Agent": "artcraft-pages-updater",
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
            "User-Agent": "artcraft-pages-updater",
        },
    )

    with urllib.request.urlopen(request, timeout=300) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract without allowing paths to escape the destination."""
    destination_resolved = destination.resolve()

    for item in archive.infolist():
        target = (destination / item.filename).resolve()

        if target != destination_resolved and destination_resolved not in target.parents:
            raise RuntimeError(f"Unsafe path in archive: {item.filename}")

    archive.extractall(destination)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def repository_url(repo: str) -> str:
    return f"https://github.com/{OWNER}/{repo}"


def get_latest_release(repo: str) -> dict:
    url = f"{API_BASE}/repos/{quote(OWNER)}/{quote(repo)}/releases/latest"
    return request_json(url)


def select_release_asset(release: dict, prefix: str) -> dict:
    assets = release.get("assets", [])

    candidates = [
        asset for asset in assets
        if asset.get("name", "").lower().startswith(prefix.lower())
        and asset.get("name", "").lower().endswith(".zip")
    ]

    if not candidates:
        available = ", ".join(asset.get("name", "") for asset in assets)
        raise RuntimeError(
            f"No ZIP asset starting with {prefix!r}. "
            f"Available assets: {available or '(none)'}"
        )

    # Prefer the first matching asset, as ordered by the release API.
    return candidates[0]


def verify_archive(archive_path: Path, asset: dict) -> None:
    """Verify against GitHub's asset digest when it is provided."""
    expected = asset.get("digest")

    if expected:
        algorithm, separator, expected_hash = expected.partition(":")
        if separator and algorithm.lower() == "sha256":
            actual = sha256_file(archive_path)
            if actual.lower() != expected_hash.lower():
                raise RuntimeError(
                    f"SHA-256 mismatch for {asset['name']}: "
                    f"expected {expected_hash}, got {actual}"
                )
            return

    # GitHub does not provide a digest for every release asset.
    # Do not pretend an unverified archive has been cryptographically verified.
    print(
        f"Warning: GitHub supplied no SHA-256 digest for {asset['name']}; "
        "skipping checksum verification.",
        file=sys.stderr,
    )


def make_app_url(app_id: str) -> str:
    if REPOSITORY:
        repo_name = REPOSITORY.split("/", 1)[-1]
        return f"/{repo_name}/apps/{app_id}/"

    return f"./apps/{app_id}/"


def update_app(definition: tuple, previous_state: dict) -> tuple[dict, dict, bool]:
    app_id, display_name, repo, asset_prefix = definition
    print(f"\nChecking {display_name} ({OWNER}/{repo})...")

    release = get_latest_release(repo)
    tag = release.get("tag_name") or release.get("name") or ""
    if not tag:
        raise RuntimeError("Latest release has no tag or name.")

    asset = select_release_asset(release, asset_prefix)
    asset_name = asset["name"]
    asset_id = asset["id"]

    previous = previous_state.get(app_id, {})
    destination = APPS_DIR / app_id
    old_tag = previous.get("tag")
    old_asset_id = previous.get("asset_id")

    # A version is unchanged only if both the release tag and selected asset ID
    # match the last successfully deployed state and the app still exists.
    if (
        old_tag == tag
        and old_asset_id == asset_id
        and (destination / "index.html").is_file()
    ):
        print(f"Unchanged: {tag}; skipping download.")
        app_entry = {
            "id": app_id,
            "name": display_name,
            "version": tag,
            "description": release.get("body", "").strip()[:500],
            "url": make_app_url(app_id),
            "repository": repository_url(repo),
        }
        new_state = {
            "tag": tag,
            "asset_id": asset_id,
            "asset_name": asset_name,
        }
        return app_entry, new_state, False

    print(f"Updating from {old_tag or 'not installed'} to {tag}.")

    with tempfile.TemporaryDirectory(prefix=f"{app_id}-") as temp:
        temp_dir = Path(temp)
        archive_path = temp_dir / asset_name
        extracted_dir = temp_dir / "extracted"
        extracted_dir.mkdir()

        download(asset["browser_download_url"], archive_path)
        verify_archive(archive_path, asset)

        with zipfile.ZipFile(archive_path) as archive:
            safe_extract(archive, extracted_dir)

        # Support either a ZIP with index.html at its root or one containing
        # a single enclosing directory.
        index_files = list(extracted_dir.rglob("index.html"))
        valid_indexes = [
            index for index in index_files
            if any(file.suffix.lower() == ".wasm" for file in index.parent.rglob("*") if file.is_file())
        ]

        if len(valid_indexes) != 1:
            raise RuntimeError(
                f"Expected one index.html alongside a WebAssembly build, "
                f"found {len(valid_indexes)} candidate(s)."
            )

        app_root = valid_indexes[0].parent

        # Prepare replacement separately so a failed download or extraction
        # does not destroy the existing working app.
        staged = temp_dir / "staged"
        shutil.copytree(app_root, staged)

        # Swap only after the new build has passed basic validation.
        backup = temp_dir / "backup"
        if destination.exists():
            destination.rename(backup)

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise

    app_entry = {
        "id": app_id,
        "name": display_name,
        "version": tag,
        "description": release.get("body", "").strip()[:500],
        "url": make_app_url(app_id),
        "repository": repository_url(repo),
    }

    new_state = {
        "tag": tag,
        "asset_id": asset_id,
        "asset_name": asset_name,
    }

    print(f"Installed {display_name} {tag}.")
    return app_entry, new_state, True


def main() -> int:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    CHANGE_FLAG_PATH.write_text("false\n", encoding="utf-8")

    previous_state = load_json(STATE_PATH, {})
    new_state = {}
    manifest = []
    changed = False
    failures = []

    for definition in APP_DEFINITIONS:
        app_id, display_name, repo, _ = definition

        try:
            app_entry, app_state, app_changed = update_app(
                definition, previous_state
            )
            manifest.append(app_entry)
            new_state[app_id] = app_state
            changed = changed or app_changed

        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError, zipfile.BadZipFile, RuntimeError, KeyError) as error:
            failures.append(f"{display_name}: {error}")
            print(f"ERROR: {display_name}: {error}", file=sys.stderr)

            # Preserve the prior manifest entry and state if the update failed.
            old_entry = next(
                (
                    entry for entry in load_json(MANIFEST_PATH, [])
                    if entry.get("id") == app_id
                ),
                None,
            )
            if old_entry:
                manifest.append(old_entry)

            if app_id in previous_state:
                new_state[app_id] = previous_state[app_id]

    # Save manifest and state only after all apps have been checked.
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    STATE_PATH.write_text(
        json.dumps(new_state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    CHANGE_FLAG_PATH.write_text(
        "true\n" if changed else "false\n",
        encoding="utf-8",
    )

    size = sum(
        path.stat().st_size
        for path in PUBLIC.rglob("*")
        if path.is_file()
    )
    print(f"\nPublic directory size: {size / (1024 ** 2):.1f} MiB")
    print(f"App files changed: {changed}")

    if failures:
        print("\nSome apps could not be checked or updated:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
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
STATE_DIR = ROOT / ".state"
STATE_PATH = STATE_DIR / "apps-state.json"
CHANGE_FLAG_PATH = ROOT / ".apps-changed"

OWNER = os.environ.get("ARTCRAFT_REPO_OWNER", "storytold")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
API_BASE = "https://api.github.com"

APP_DEFINITIONS = [
    ("photocraft", "PhotoCraft", "photocraft", "photocraft-web-", "Photo editor"),
    ("vectorcraft", "VectorCraft", "vectorcraft", "vectorcraft-web-", "Vector graphics editor"),
    ("filmcraft", "FilmCraft", "filmcraft", "filmcraft-web-", "Video editor"),
    ("lightcraft", "LightCraft", "lightcraft", "lightcraft-web-", "Photo and image enhancement"),
    ("pdfcraft", "PDFCraft", "pdfcraft", "pdfcraft-web-", "PDF editor"),
    ("effectcraft", "EffectCraft", "effectcraft", "effectcraft-web-", "Visual effects editor"),
    ("designcraft", "DesignCraft", "designcraft", "designcraft-web-", "Graphic design tool"),
    ("figcraft", "FigCraft", "figcraft", "figcraft-web-", "Design and prototyping tool"),
    ("wordcraft", "WordCraft", "wordcraft", "wordcraft-web-", "Word processor"),
    ("deckcraft", "DeckCraft", "deckcraft", "deckcraft-web-", "Presentation editor"),
    ("cadcraft", "CADCraft", "cadcraft", "cadcraft-web-", "CAD design tool"),
    ("gridcraft", "GridCraft", "gridcraft", "gridcraft-web-", "Spreadsheet editor"),
    ("soundcraft", "SoundCraft", "soundcraft", "soundcraft-web-", "Audio editor"),
]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def api_json(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "artcraft-pages-updater",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(url: str, destination: Path) -> None:
    headers = {"User-Agent": "artcraft-pages-updater"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, asset: dict) -> None:
    expected = asset.get("digest")
    if not expected:
        print(f"Warning: no GitHub checksum for {asset['name']}.")
        return

    algorithm, separator, expected_hash = expected.partition(":")
    if not separator or algorithm.lower() != "sha256":
        print(f"Warning: unsupported checksum for {asset['name']}.")
        return

    actual = sha256_file(path)
    if actual.lower() != expected_hash.lower():
        raise RuntimeError(f"SHA-256 mismatch for {asset['name']}.")


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()

    for item in archive.infolist():
        target = (destination / item.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"Unsafe archive path: {item.filename}")

    archive.extractall(destination)


def get_release(repo: str) -> dict:
    url = f"{API_BASE}/repos/{quote(OWNER)}/{quote(repo)}/releases/latest"
    return api_json(url)


def get_asset(release: dict, prefix: str) -> dict:
    assets = release.get("assets", [])
    candidates = [
        asset for asset in assets
        if asset.get("name", "").lower().startswith(prefix.lower())
        and asset.get("name", "").lower().endswith(".zip")
    ]

    if not candidates:
        names = ", ".join(a.get("name", "") for a in assets)
        raise RuntimeError(
            f"No ZIP asset starting with {prefix!r}. "
            f"Available assets: {names or '(none)'}"
        )

    return candidates[0]


def app_url(app_id: str) -> str:
    if REPOSITORY:
        repo_name = REPOSITORY.split("/", 1)[-1]
        return f"/{repo_name}/apps/{app_id}/"
    return f"./apps/{app_id}/"


def update_app(definition: tuple, old_state: dict) -> tuple[dict, dict, bool]:
    app_id, display_name, repo, prefix, description = definition
    print(f"\nChecking {display_name} ({OWNER}/{repo})...")

    release = get_release(repo)
    tag = release.get("tag_name")
    if not tag:
        raise RuntimeError("Latest release has no tag.")

    asset = get_asset(release, prefix)
    asset_id = asset["id"]
    asset_name = asset["name"]

    destination = APPS_DIR / app_id
    previous = old_state.get(app_id, {})

    if (
        previous.get("tag") == tag
        and previous.get("asset_id") == asset_id
        and (destination / "index.html").is_file()
    ):
        print(f"Unchanged: {tag}. Skipping download.")
        changed = False
    else:
        print(f"Updating {display_name} to {tag}.")

        with tempfile.TemporaryDirectory(prefix=f"{app_id}-") as temp:
            temp_dir = Path(temp)
            archive_path = temp_dir / asset_name
            extracted = temp_dir / "extracted"
            extracted.mkdir()

            download(asset["browser_download_url"], archive_path)
            verify_archive(archive_path, asset)

            with zipfile.ZipFile(archive_path) as archive:
                safe_extract(archive, extracted)

            candidates = []
            for index in extracted.rglob("index.html"):
                if any(
                    file.is_file() and file.suffix.lower() == ".wasm"
                    for file in index.parent.rglob("*")
                ):
                    candidates.append(index)

            if len(candidates) != 1:
                raise RuntimeError(
                    "Expected exactly one index.html alongside a WebAssembly "
                    f"build; found {len(candidates)}."
                )

            staged = temp_dir / "staged"
            shutil.copytree(candidates[0].parent, staged)

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

        changed = True
        print(f"Installed {display_name} {tag}.")

    entry = {
        "id": app_id,
        "name": display_name,
        "version": tag,
        "description": description,
        "url": app_url(app_id),
        "repository": f"https://github.com/{OWNER}/{repo}",
    }

    state = {
        "tag": tag,
        "asset_id": asset_id,
        "asset_name": asset_name,
    }

    return entry, state, changed


def main() -> int:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    CHANGE_FLAG_PATH.write_text("false\n", encoding="utf-8")

    old_state = load_json(STATE_PATH, {})
    old_manifest = load_json(MANIFEST_PATH, [])

    old_entries = {
        entry.get("id"): entry
        for entry in old_manifest
        if isinstance(entry, dict) and entry.get("id")
    }

    new_state = {}
    manifest = []
    changed = False
    failures = []

    for definition in APP_DEFINITIONS:
        app_id, display_name, _, _ = definition

        try:
            entry, state, app_changed = update_app(definition, old_state)
            manifest.append(entry)
            new_state[app_id] = state
            changed = changed or app_changed

        except Exception as error:
            print(f"ERROR: {display_name}: {error}", file=sys.stderr)
            failures.append(f"{display_name}: {error}")

            # Keep the last known manifest and state if an update fails.
            if app_id in old_entries:
                manifest.append(old_entries[app_id])
            if app_id in old_state:
                new_state[app_id] = old_state[app_id]

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
        print("\nSome apps failed to update:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

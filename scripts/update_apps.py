#!/usr/bin/env python3
"""Download, verify and prepare ArtCraft browser builds for GitHub Pages."""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
APPS_DIR = PUBLIC / "apps"
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_OWNER = os.environ.get("ARTCRAFT_REPO_OWNER", "storytold")
HEADERS = {
    "User-Agent": "artcraft-wasm-github-pages/1.0",
    "Accept": "application/vnd.github+json",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

# id, display name, description, upstream repository, release asset name prefix
APPS = [
    ("photocraft", "PhotoCraft", "Photo editing and image workflows", "photocraft", "photocraft-web-"),
    ("vectorcraft", "VectorCraft", "Vector graphics and illustration", "vectorcraft", "vectorcraft-web-"),
    ("filmcraft", "FilmCraft", "Video and timeline editing", "filmcraft", "filmcraft-web-"),
    ("lightcraft", "LightCraft", "Lighting and image adjustments", "lightcraft", "lightcraft-web-"),
    ("pdfcraft", "PdfCraft", "PDF editing and document workflows", "pdfcraft", "pdfcraft-web-"),
    ("effectcraft", "EffectCraft", "Visual effects and compositing", "effectcraft", "effectcraft-web-"),
    ("designcraft", "DesignCraft", "Design and layout", "designcraft", "designcraft-web-"),
    ("figcraft", "FigCraft", "Interface and vector design", "figcraft", "figcraft-web-"),
    ("wordcraft", "WordCraft", "Document editing", "wordcraft", "wordcraft-web-"),
    ("deckcraft", "DeckCraft", "Presentations", "deckcraft", "deckcraft-web-"),
    ("cadcraft", "CADCraft", "Computer-aided design", "cadcraft", "cadcraft-web-"),
    ("gridcraft", "GridCraft", "Spreadsheet and grid workflows", "gridcraft", "gridcraft-web-"),
    ("soundcraft", "SoundCraft", "Audio editing", "soundcraft", "soundcraft-web-"),
]

def request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()

def get_json(url):
    return json.loads(request(url))

def get_sha256(release_assets, archive_asset):
    digest = archive_asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    manifest = next((a for a in release_assets if a["name"].lower() in ("sha256sums.txt", "sha256sum.txt", "checksums.txt")), None)
    if not manifest:
        raise RuntimeError(f"No published SHA-256 digest or checksum manifest for {archive_asset['name']}")
    text = request(manifest["browser_download_url"]).decode("utf-8", "replace")
    for line in text.splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match and PurePosixPath(match.group(2).strip().lstrip("*")).name == archive_asset["name"]:
            return match.group(1).lower()
    raise RuntimeError(f"Checksum for {archive_asset['name']} not found in {manifest['name']}")

def safe_extract(archive_path, destination):
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe path in ZIP: {name}")
            if not path.parts:
                continue
            # Avoid shipping Rust build intermediates if included in the archive.
            if any(part.lower() == "target" for part in path.parts):
                continue
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise RuntimeError(f"Symlinks are not allowed in ZIP: {name}")
            output = (root / Path(*path.parts)).resolve()
            if output != root and root not in output.parents:
                raise RuntimeError(f"Unsafe output path in ZIP: {name}")
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, output.open("wb") as target:
                    shutil.copyfileobj(source, target)

def flatten_single_directory(folder):
    if (folder / "index.html").is_file():
        return
    dirs = [p for p in folder.iterdir() if p.is_dir()]
    if len(dirs) == 1 and (dirs[0] / "index.html").is_file():
        nested = dirs[0]
        for item in list(nested.iterdir()):
            target = folder / item.name
            if item.is_dir():
                shutil.move(str(item), str(target))
            else:
                shutil.move(str(item), str(target))
        nested.rmdir()
    if not (folder / "index.html").is_file():
        raise RuntimeError(f"Extracted archive has no index.html at its root: {folder}")

def check_pages_size():
    total = sum(p.stat().st_size for p in PUBLIC.rglob("*") if p.is_file())
    print(f"Published site size: {total / (1024**2):.1f} MiB")
    # Keep a little margin under GitHub Pages' 1 GB published-site limit.
    if total > 950_000_000:
        raise RuntimeError(f"Published site is too large for GitHub Pages: {total:,} bytes")

def main():
    PUBLIC.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": [],
    }
    errors = []

    for app_id, name, description, repo_name, asset_prefix in APPS:
        repository = f"{REPO_OWNER}/{repo_name}"
        archive_path = ROOT / f".{app_id}-download.zip"
        staging = APPS_DIR / f".{app_id}-staging"
        target = APPS_DIR / app_id
        try:
            release = get_json(f"{API}/repos/{repository}/releases/latest")
            assets = release.get("assets", [])
            matches = [
                asset for asset in assets
                if asset["name"].lower().endswith(".zip")
                and asset["name"].lower().startswith(asset_prefix.lower())
            ]
            if not matches:
                raise RuntimeError(f"No ZIP asset starting with '{asset_prefix}' in latest release")
            asset = sorted(matches, key=lambda item: item["name"])[-1]
            expected_hash = get_sha256(assets, asset)
            archive_path.write_bytes(request(asset["browser_download_url"]))
            actual_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise RuntimeError(f"SHA-256 mismatch for {asset['name']}")

            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            safe_extract(archive_path, staging)
            flatten_single_directory(staging)
            if not any(staging.rglob("*.wasm")):
                raise RuntimeError("Extracted archive contains no .wasm file")

            # Replace the old app only after the new archive passes validation.
            backup = APPS_DIR / f".{app_id}-previous"
            shutil.rmtree(backup, ignore_errors=True)
            if target.exists():
                target.rename(backup)
            try:
                staging.rename(target)
            except Exception:
                if backup.exists() and not target.exists():
                    backup.rename(target)
                raise
            shutil.rmtree(backup, ignore_errors=True)

            base_path = os.environ.get("GITHUB_REPOSITORY", "")
            if base_path and "/" in base_path:
                owner, repository_name = base_path.split("/", 1)
                app_url = f"/{repository_name}/apps/{app_id}/"
            else:
                app_url = f"/apps/{app_id}/"

            manifest["apps"].append({
                "id": app_id,
                "name": name,
                "description": description,
                "version": release.get("tag_name", "unknown").lstrip("v"),
                "url": app_url,
                "source": f"https://github.com/{repository}/releases",
                "release": release.get("html_url"),
                "archive": asset["name"],
                "sha256": actual_hash,
            })
            print(f"OK {name} {release.get('tag_name', 'unknown')}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"ERROR {name}: {exc}", file=sys.stderr)
        finally:
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    # Include every app entry, even if a download failed, so the launcher can show its release link.
    completed = {app["id"]: app for app in manifest["apps"]}
    for app_id, name, description, repo_name, _ in APPS:
        if app_id not in completed:
            repository = f"{REPO_OWNER}/{repo_name}"
            completed[app_id] = {
                "id": app_id,
                "name": name,
                "description": description,
                "version": "Unavailable",
                "url": None,
                "source": f"https://github.com/{repository}/releases",
            }
    manifest["apps"] = sorted(completed.values(), key=lambda app: app["name"].lower())
    (PUBLIC / "apps.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    check_pages_size()
    if errors:
        print("\nUpdate errors:\n- " + "\n- ".join(errors), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()

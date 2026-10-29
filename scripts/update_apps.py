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


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
APPS_DIR = PUBLIC_DIR / "apps"
MANIFEST_PATH = PUBLIC_DIR / "apps.json"
CHANGED_PATH = ROOT / ".apps-changed"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

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


def github_request(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ArtCraft-Updater",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), response.headers


def get_latest_release(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    data, _ = github_request(url)
    return json.loads(data)


def download_asset(asset, destination):
    url = asset["url"]
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "ArtCraft-Updater",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=180) as response:
        with open(destination, "wb") as output:
            shutil.copyfileobj(response, output)


def select_asset(release, prefix):
    assets = release.get("assets", [])

    candidates = [
        asset
        for asset in assets
        if asset["name"].startswith(prefix)
        and asset["name"].lower().endswith(".zip")
    ]

    if not candidates:
        available = ", ".join(asset["name"] for asset in assets)
        raise RuntimeError(
            f"No ZIP asset beginning with {prefix!r} was found. "
            f"Available assets: {available or '(none)'}"
        )

    return candidates[0]


def verify_asset(asset, path):
    expected_digest = asset.get("digest")

    if not expected_digest:
        return

    algorithm, separator, expected_hash = expected_digest.partition(":")

    if not separator or algorithm != "sha256":
        print(f"Skipping unsupported asset digest: {expected_digest}")
        return

    hasher = hashlib.sha256()

    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)

    actual_hash = hasher.hexdigest()

    if actual_hash.lower() != expected_hash.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {asset['name']}: "
            f"expected {expected_hash}, got {actual_hash}"
        )


def safe_extract(zip_path, destination):
    destination = destination.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()

            if target != destination and destination not in target.parents:
                raise RuntimeError(
                    f"Unsafe path in ZIP archive: {member.filename}"
                )

            unix_mode = member.external_attr >> 16

            # Reject symbolic links in downloaded archives.
            if (unix_mode & 0o170000) == 0o120000:
                raise RuntimeError(
                    f"Symbolic links are not allowed in ZIP archives: "
                    f"{member.filename}"
                )

        archive.extractall(destination)


def find_app_root(extracted_dir):
    indexes = list(extracted_dir.rglob("index.html"))

    if len(indexes) != 1:
        raise RuntimeError(
            f"Expected exactly one index.html, found {len(indexes)}"
        )

    index_path = indexes[0]
    app_root = index_path.parent

    wasm_files = list(app_root.rglob("*.wasm"))

    if not wasm_files:
        raise RuntimeError(
            f"No .wasm file found below {index_path}"
        )

    return app_root


def install_app(app_id, display_name, repo, prefix, description):
    owner = "storytold"

    print(f"\nChecking {display_name} ({owner}/{repo})...")

    release = get_latest_release(owner, repo)
    asset = select_asset(release, prefix)

    tag = release.get("tag_name", "unknown")
    print(f"Downloading {display_name} release {tag}: {asset['name']}")

    APPS_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{app_id}-") as temp_name:
        temp_dir = Path(temp_name)
        zip_path = temp_dir / "release.zip"
        extracted_dir = temp_dir / "extracted"
        extracted_dir.mkdir()

        download_asset(asset, zip_path)
        verify_asset(asset, zip_path)
        safe_extract(zip_path, extracted_dir)

        app_root = find_app_root(extracted_dir)
        destination = APPS_DIR / app_id
        staged_dir = APPS_DIR / f".{app_id}-staged"
        backup_dir = APPS_DIR / f".{app_id}-backup"

        shutil.rmtree(staged_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)

        shutil.copytree(app_root, staged_dir)

        try:
            if destination.exists():
                destination.rename(backup_dir)

            staged_dir.rename(destination)
            shutil.rmtree(backup_dir, ignore_errors=True)

        except Exception:
            if not destination.exists() and backup_dir.exists():
                backup_dir.rename(destination)
            raise

    print(f"Installed {display_name} {tag}." )

    return {
        "id": app_id,
        "name": display_name,
        "description": description,
        "url": f"apps/{app_id}/",
        "version": tag,
        "repository": f"https://github.com/{owner}/{repo}",
    }


def main():
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)

    manifest = []
    errors = []

    for definition in APP_DEFINITIONS:
        app_id, display_name, repo, prefix, description = definition

        try:
            app_entry = install_app(
                app_id,
                display_name,
                repo,
                prefix,
                description,
            )
            manifest.append(app_entry)

        except Exception as error:
            message = f"{display_name}: {error}"
            errors.append(message)
            print(f"ERROR: {message}", file=sys.stderr)

    # Do not publish an incomplete manifest if any app failed.
    if errors:
        print(
            "\nOne or more apps failed to download. "
            "The existing apps.json has not been replaced.",
            file=sys.stderr,
        )
        CHANGED_PATH.write_text("true\n", encoding="utf-8")
        return 1

    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Every run downloads and replaces every app, so files have changed.
    CHANGED_PATH.write_text("true\n", encoding="utf-8")

    total_size = sum(
        path.stat().st_size
        for path in PUBLIC_DIR.rglob("*")
        if path.is_file()
    )

    print(f"\nUpdated {len(manifest)} apps.")
    print(f"Public directory size: {total_size / (1024 * 1024):.1f} MiB")
    print(f"Manifest written to {MANIFEST_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

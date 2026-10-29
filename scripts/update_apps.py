#!/usr/bin/env python3
"""Download and verify the latest official ArtCraft web builds."""
import hashlib, json, os, re, shutil, sys, time, urllib.error, urllib.request, zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
APPS_DIR = BUILD / "apps"
PUBLIC = ROOT / "public"
BASE_URL = os.environ.get("ASSET_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
API = "https://api.github.com"
APPS = [
    ("photocraft","PhotoCraft","Photo editing and image workflows"),
    ("vectorcraft","VectorCraft","Vector graphics and illustration"),
    ("filmcraft","FilmCraft","Video and timeline editing"),
    ("lightcraft","LightCraft","Lighting and image adjustments"),
    ("pdfcraft","PdfCraft","PDF editing and document workflows"),
    ("effectcraft","EffectCraft","Visual effects and compositing"),
    ("designcraft","DesignCraft","Design and layout"),
    ("figcraft","FigCraft","Interface and vector design"),
    ("wordcraft","WordCraft","Document editing"),
    ("deckcraft","DeckCraft","Presentations"),
    ("cadcraft","CADCraft","Computer-aided design"),
    ("gridcraft","GridCraft","Spreadsheet and grid workflows"),
    ("soundcraft","SoundCraft","Audio editing"),
]
HEADERS = {"User-Agent":"artcraft-wasm-static-launcher/1.0","Accept":"application/vnd.github+json"}
if TOKEN: HEADERS["Authorization"] = f"Bearer {TOKEN}"

def request(url):
    # Only send the optional GitHub API token to api.github.com, never to redirected asset hosts.
    headers = dict(HEADERS) if urlparse(url).hostname == "api.github.com" else {"User-Agent":"artcraft-wasm-static-launcher/1.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r: return r.read()

def get_json(url): return json.loads(request(url))

def checksum_from_release(repo, assets, web_asset):
    # Prefer GitHub's per-asset digest. Otherwise require the release's published checksum manifest.
    digest = web_asset.get("digest") or ""
    if digest.startswith("sha256:"): return digest.split(":",1)[1].lower()
    manifest = next((a for a in assets if a["name"].lower() == "sha256sums.txt"), None)
    if not manifest: raise RuntimeError(f"{repo}: no SHA-256 digest or SHA256SUMS.txt; refusing unverified download")
    content = request(manifest["browser_download_url"]).decode("utf-8", "replace")
    for line in content.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m and PurePosixPath(m.group(2).strip().lstrip("*")).name == web_asset["name"]:
            return m.group(1).lower()
    raise RuntimeError(f"{repo}: checksum for {web_asset['name']} not found in SHA256SUMS.txt")

def safe_extract(archive, dest):
    root = Path(dest).resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            raw = info.filename.replace("\\","/")
            p = PurePosixPath(raw)
            if p.is_absolute() or ".." in p.parts: raise RuntimeError(f"Unsafe path in archive: {raw}")
            # LightCraft's published web archive has a large, unused Cargo target directory.
            if any(part.lower() == "target" for part in p.parts): continue
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000: raise RuntimeError(f"Symlink not allowed in web archive: {raw}")
            out = (root / Path(*p.parts)).resolve()
            if root not in out.parents and out != root: raise RuntimeError(f"Unsafe output path: {raw}")
            if info.is_dir(): out.mkdir(parents=True, exist_ok=True); continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out,"wb") as dst: shutil.copyfileobj(src,dst)

def normalize_root(folder):
    if (folder / "index.html").is_file(): return folder
    candidates = [p for p in folder.iterdir() if p.is_dir() and (p / "index.html").is_file()]
    if len(candidates) == 1:
        temp = folder.with_name(folder.name + "-normalized")
        temp.mkdir()
        for p in candidates[0].iterdir():
            target = temp / p.name
            if p.is_dir(): shutil.copytree(p,target)
            else: shutil.copy2(p,target)
        shutil.rmtree(folder); temp.rename(folder)
    if not (folder / "index.html").is_file(): raise RuntimeError(f"Expected index.html at archive root for {folder.name}")
    return folder

def main():
    if not BASE_URL: raise RuntimeError("ASSET_BASE_URL must be set to the public base URL for the R2 bucket, e.g. https://assets.example.com")
    BUILD.mkdir(exist_ok=True); APPS_DIR.mkdir(parents=True, exist_ok=True); PUBLIC.mkdir(exist_ok=True)
    previous_path = PUBLIC / "apps.json"
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_apps = {a.get("id"):a for a in previous.get("apps",[]) if a.get("id")}
    except (OSError, ValueError):
        previous = {}; previous_apps = {}
    manifest = {"checked_at":previous.get("checked_at"),"apps":[]}
    updated_ids=[]; failures=[]
    for app_id,name,description in APPS:
        repo = "storytold/" + app_id
        if app_id == "pdfcraft": repo = "storytold/pdfcraft"
        try:
            release = get_json(f"{API}/repos/{repo}/releases/latest")
            assets = release.get("assets",[])
            candidates = [a for a in assets if a["name"].lower().endswith(".zip") and (a["name"].lower().startswith(app_id+"-web-") or (app_id=="pdfcraft" and a["name"].lower().startswith("printcraft-web-")))]
            if not candidates: raise RuntimeError(f"{repo}: no web build ZIP found in latest release {release.get('tag_name')}")
            # If multiple web zips exist, prefer the newest-looking matching asset name.
            web = sorted(candidates,key=lambda a:a["name"])[-1]
            version = release.get("tag_name","unknown").lstrip("v")
            old = previous_apps.get(app_id,{})
            url = f"{BASE_URL}/apps/{app_id}/"
            if old.get("version") == version and old.get("url") == url:
                manifest["apps"].append({"id":app_id,"name":name,"description":description,"version":version,"url":url,"source":f"https://github.com/{repo}/releases","release":release.get("html_url"),"archive":old.get("archive"),"sha256":old.get("sha256")})
                print(f"UNCHANGED {name} {version}")
                continue
            expected = checksum_from_release(repo,assets,web)
            archive_path = BUILD / f"{app_id}.zip"
            archive_path.write_bytes(request(web["browser_download_url"]))
            actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual != expected: raise RuntimeError(f"{repo}: SHA-256 mismatch for {web['name']}")
            staging = BUILD / f".{app_id}-staging"
            shutil.rmtree(staging,ignore_errors=True); staging.mkdir()
            safe_extract(archive_path,staging); normalize_root(staging)
            # Validate a web build before replacing the currently staged copy.
            if not any(staging.rglob("*.wasm")): raise RuntimeError(f"{repo}: archive contains no .wasm file")
            target = APPS_DIR / app_id
            shutil.rmtree(target,ignore_errors=True); staging.rename(target)
            manifest["apps"].append({"id":app_id,"name":name,"description":description,"version":version,"url":url,"source":f"https://github.com/{repo}/releases","release":release.get("html_url"),"archive":web["name"],"sha256":actual})
            updated_ids.append(app_id)
            archive_path.unlink(missing_ok=True)
            print(f"UPDATED {name} {version}: {web['name']}")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            print(f"ERROR {name}: {exc}",file=sys.stderr)
    # A failed app prevents publication; the checked-in catalogue remains unchanged.
    manifest["apps"].sort(key=lambda a:a["name"].lower())
    if updated_ids or not manifest["checked_at"]:
        manifest["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    (PUBLIC / "apps.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    (BUILD / "updated-apps.txt").write_text("\n".join(updated_ids) + ("\n" if updated_ids else ""),encoding="utf-8")
    if failures:
        print("\nSome apps failed:\n- " + "\n- ".join(failures),file=sys.stderr)
        # Fail the workflow so a partial deployment is not published accidentally.
        raise SystemExit(1)
if __name__ == "__main__": main()

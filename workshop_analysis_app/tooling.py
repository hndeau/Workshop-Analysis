"""Download and install helper routines for external inspection tools."""

import json
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .common import USER_AGENT, ensure_directory


def download_file(uri, out_file):
    out_file = Path(out_file)
    request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        with out_file.open("wb") as target:
            shutil.copyfileobj(response, target)


def get_steam_workshop_item_title(content_id):
    data = urllib.parse.urlencode(
        {
            "itemcount": 1,
            "publishedfileids[0]": str(content_id),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    details = payload.get("response", {}).get("publishedfiledetails", [])
    if not details:
        return None

    title = details[0].get("title")
    if not title or not str(title).strip():
        return None

    return str(title).strip()


def get_steam_app_title(app_id):
    query = urllib.parse.urlencode({"appids": str(app_id)})
    request = urllib.request.Request(
        "https://store.steampowered.com/api/appdetails?{0}".format(query),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    details = payload.get(str(app_id), {})
    if not details.get("success"):
        return None

    title = details.get("data", {}).get("name")
    if not title or not str(title).strip():
        return None

    return str(title).strip()

def get_github_latest_release_asset(repository, asset_name_regex):
    import re

    uri = "https://api.github.com/repos/{0}/releases/latest".format(repository)
    request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        release = json.loads(response.read().decode("utf-8"))

    pattern = re.compile(asset_name_regex)
    for asset in release.get("assets", []):
        if pattern.search(asset.get("name", "")):
            return asset

    raise RuntimeError(
        "Could not find a release asset matching '{0}' in {1} latest release.".format(
            asset_name_regex, repository
        )
    )

def install_zip_tool_from_github(repository, asset_name_regex, install_dir, expected_exe_name):
    install_dir = ensure_directory(install_dir)
    asset = get_github_latest_release_asset(repository, asset_name_regex)
    asset_path = install_dir / asset["name"]
    expected_name = expected_exe_name.lower()

    print("Downloading {0} from {1}...".format(asset["name"], repository))
    download_file(asset["browser_download_url"], asset_path)

    if asset_path.name.lower() == expected_name:
        return str(asset_path)

    if asset_path.suffix.lower() != ".zip":
        raise RuntimeError(
            "Downloaded {0}, but it is not a zip archive and does not match expected executable {1}.".format(
                asset["name"],
                expected_exe_name,
            )
        )

    try:
        with zipfile.ZipFile(asset_path) as archive:
            archive.extractall(install_dir)
    finally:
        if asset_path.exists():
            asset_path.unlink()

    for path in install_dir.rglob("*"):
        if path.is_file() and path.name.lower() == expected_name:
            return str(path)

    raise RuntimeError(
        "Installed {0}, but could not find {1} under {2}.".format(
            repository, expected_exe_name, install_dir
        )
    )

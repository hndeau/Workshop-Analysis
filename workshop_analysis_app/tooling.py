"""Download and install helper routines for external inspection tools."""

import json
import shutil
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
    zip_path = install_dir / asset["name"]

    print("Downloading {0} from {1}...".format(asset["name"], repository))
    download_file(asset["browser_download_url"], zip_path)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(install_dir)
    finally:
        if zip_path.exists():
            zip_path.unlink()

    expected_name = expected_exe_name.lower()
    for path in install_dir.rglob("*"):
        if path.is_file() and path.name.lower() == expected_name:
            return str(path)

    raise RuntimeError(
        "Installed {0}, but could not find {1} under {2}.".format(
            repository, expected_exe_name, install_dir
        )
    )

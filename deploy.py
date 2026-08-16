#!/usr/bin/env python3
import os
import zipfile
import shutil
from pathlib import Path
import re


def increment_version():
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        print("pyproject.toml not found")
        return

    content = pyproject_path.read_text()
    version_match = re.search(r'version = "(\d+)\.(\d+)\.(\d+)"', content)
    if not version_match:
        print("Version not found in pyproject.toml")
        return

    major, minor, patch = map(int, version_match.groups())
    patch += 1
    new_version = f"{major}.{minor}.{patch}"

    new_content = re.sub(
        r'version = "\d+\.\d+\.\d+"', f'version = "{new_version}"', content
    )
    pyproject_path.write_text(new_content)
    print(f"Version incremented to {new_version}")


def delete_logs():
    logs_dir = Path("logs")
    if logs_dir.exists():
        shutil.rmtree(logs_dir)
        print("Deleted logs directory")


def create_zip():
    current_dir = Path.cwd()
    parent_dir = current_dir.name
    zip_name = f"{parent_dir}.sdkmod"

    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("__init__.py", f"{parent_dir}/__init__.py")
        zf.write("pyproject.toml", f"{parent_dir}/pyproject.toml")

    print(f"Created {zip_name}")
    return zip_name


# Every mods_base.Game value this machine has an install for. Not every game a
# mod could declare in supported_games necessarily has an entry here -
# install_to_supported_games warns rather than fails when a declared game has
# no known or no existing folder, since that is a fact about this machine, not
# about the mod.
STEAM_COMMON = Path("C:/Program Files (x86)/Steam/steamapps/common")
GAME_INSTALL_FOLDERS = {
    "BL1": STEAM_COMMON / "Borderlands",
    "BL1E": STEAM_COMMON / "BorderlandsGOTYEnhanced",
    "BL2": STEAM_COMMON / "Borderlands 2",
    "TPS": STEAM_COMMON / "BorderlandsPreSequel",
}


def read_supported_games() -> list[str]:
    content = Path("pyproject.toml").read_text()
    match = re.search(r"supported_games\s*=\s*\[([^\]]*)\]", content)
    if not match:
        print("supported_games not found in pyproject.toml")
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def install_to_supported_games(zip_name: str):
    """Copy the built .sdkmod into every game folder this mod declares support
    for, deriving the list fresh from pyproject.toml each run rather than from
    a remembered set of folders - the set of supported games changes over a
    mod's life and each deploy must reflect whatever it says right now.
    """
    games = read_supported_games()
    if not games:
        print("No supported_games to install to")
        return

    extra_files = sorted(Path.cwd().glob("verify_*.py"))

    for game in games:
        folder = GAME_INSTALL_FOLDERS.get(game)
        if folder is None:
            print(f"  {game}: skipped, no known install folder for this machine")
            continue
        sdk_mods = folder / "sdk_mods"
        if not sdk_mods.is_dir():
            print(f"  {game}: skipped, {sdk_mods} does not exist")
            continue

        shutil.copy(zip_name, sdk_mods / zip_name)
        for extra in extra_files:
            shutil.copy(extra, sdk_mods / extra.name)
        print(f"  {game}: installed to {sdk_mods}")


if __name__ == "__main__":
    delete_logs()
    increment_version()
    zip_name = create_zip()
    install_to_supported_games(zip_name)

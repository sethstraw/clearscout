"""Verify that Python distributions carry a complete production SPA."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path

ASSET_REFERENCE = re.compile(r"(?:src|href)=[\"'](/assets/[^\"']+)[\"']")


class DistributionError(ValueError):
    """A built distribution is missing its frontend contract."""


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            files: dict[str, bytes] = {}
            for member in archive.getmembers():
                data = archive.extractfile(member) if member.isfile() else None
                if data is not None:
                    files[member.name] = data.read()
            return files
    raise DistributionError(f"unsupported distribution: {path.name}")


def verify_archive(path: Path) -> None:
    """Require an index and every local asset it references."""
    files = _archive_files(path)
    indexes = [
        name
        for name in files
        if name == "clearscout/web/index.html" or name.endswith("/web/dist/index.html")
    ]
    if len(indexes) != 1:
        raise DistributionError(f"{path.name}: expected one production index, found {len(indexes)}")
    index = indexes[0]
    references = set(ASSET_REFERENCE.findall(files[index].decode("utf-8")))
    if not references:
        raise DistributionError(f"{path.name}: production index references no local assets")
    root = index.removesuffix("index.html")
    missing = sorted(
        reference for reference in references if f"{root}{reference.lstrip('/')}" not in files
    )
    if missing:
        raise DistributionError(f"{path.name}: missing referenced assets: {', '.join(missing)}")


def main() -> int:
    """Verify archive paths supplied on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+")
    args = parser.parse_args()
    for archive in args.archives:
        verify_archive(archive)
        print(f"Verified packaged frontend in {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

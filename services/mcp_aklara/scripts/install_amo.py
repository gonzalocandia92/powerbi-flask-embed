"""Download the AMO/TOM assemblies used by the MCP container."""

import os
from pathlib import Path
import shutil
import urllib.request
import zipfile


AMO_PACKAGE = "Microsoft.AnalysisServices"
AMO_VERSION = "19.114.8"
TARGET_FRAMEWORK = "lib/net8.0/"


def download_and_extract(package_name: str, version: str, output_dir: Path) -> None:
    url = f"https://www.nuget.org/api/v2/package/{package_name}/{version}"
    archive = output_dir.parent / f"{package_name}.zip"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        print(f"Downloading {package_name} v{version}...")
        urllib.request.urlretrieve(url, archive)
        extracted = 0
        with zipfile.ZipFile(archive, "r") as package:
            for member in package.namelist():
                if (
                    member.startswith(TARGET_FRAMEWORK)
                    and member.endswith(".dll")
                    and len(member.split("/")) == 3
                ):
                    target = output_dir / os.path.basename(member)
                    with package.open(member) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    extracted += 1
        if extracted == 0:
            raise RuntimeError(
                f"No DLLs found under {TARGET_FRAMEWORK} in {package_name} {version}."
            )
        print(f"Installed {extracted} AMO assemblies in {output_dir}")
    finally:
        archive.unlink(missing_ok=True)


if __name__ == "__main__":
    service_root = Path(__file__).resolve().parents[1]
    download_and_extract(AMO_PACKAGE, AMO_VERSION, service_root / "lib")

"""Add a release to manifest.json (newest first).

Usage: update_manifest.py <manifest> <version x.y.z.w> <zip> <download url> <changelog>
The targetAbi is read from the Jellyfin.Controller version in the plugin's csproj.
"""

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

manifest_path, version, zip_path, url, changelog = sys.argv[1:6]

csproj = Path("plugin/Jellyfin.Plugin.ForcedSubs/Jellyfin.Plugin.ForcedSubs.csproj").read_text(encoding="utf-8")
m = re.search(r'Include="Jellyfin\.Controller"\s+Version="([\d.]+)"', csproj)
if not m:
    sys.exit("Could not find the Jellyfin.Controller version in the csproj")
abi = m.group(1)
abi = abi + ".0" * (4 - abi.count(".") - 1)

checksum = hashlib.md5(Path(zip_path).read_bytes()).hexdigest()
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
plugin = manifest[0]
plugin["versions"] = [v for v in plugin["versions"] if v["version"] != version]
plugin["versions"].insert(0, {
    "version": version,
    "changelog": changelog.strip() or f"Release {version}",
    "targetAbi": abi,
    "sourceUrl": url,
    "checksum": checksum,
    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
})
Path(manifest_path).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"Added {version} (targetAbi {abi}, md5 {checksum})")

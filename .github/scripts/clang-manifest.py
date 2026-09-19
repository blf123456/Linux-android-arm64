"""Read the compiler project from repo's flattened Android manifest."""

import shlex
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

manifest = ET.parse(sys.argv[1]).getroot()
default = manifest.find("default")
defaults = default.attrib if default is not None else {}
remotes = {remote.attrib["name"]: remote.attrib for remote in manifest.findall("remote")}
projects = [
    project
    for project in manifest.findall("project")
    if project.attrib["name"] == "platform/prebuilts/clang/host/linux-x86"
]
if len(projects) != 1:
    raise SystemExit("Expected exactly one official Linux Clang project in the manifest")

project = projects[0].attrib
remote = remotes[project.get("remote", defaults.get("remote"))]
revision = project.get("revision") or remote.get("revision") or defaults.get("revision")
if not revision:
    raise SystemExit("Compiler revision is missing from the manifest")
fetch = urljoin("https://android.googlesource.com/kernel/manifest", remote["fetch"])
url = fetch.rstrip("/") + "/" + project["name"]
print("CLANG_REMOTE=" + shlex.quote(url))
print("CLANG_REVISION=" + shlex.quote(revision))

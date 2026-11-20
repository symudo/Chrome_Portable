#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双更新检测脚本 (Chrome + Chrome++)
检查 Google 官方 Chrome 最新版与 Bush2021/chrome_plus 最新版插件。
任一更新（或对应的 Release Tag 尚未在仓库中发布）即判定为 update_needed=true。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

GOOGLE_OMAHA_URL = "https://tools.google.com/service/update2"
BUSH_INSTALLER_DATA_URL = "https://raw.githubusercontent.com/Bush2021/chrome_installer/main/data.json"
CHROME_PLUS_API_URL = "https://api.github.com/repos/Bush2021/chrome_plus/releases/latest"

DEFAULT_REPO = "symudo/Chrome_Portable"


def get_github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return None


def github_request(url, token=None):
    if not token:
        token = get_github_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ChromePortableChecker",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_chrome_from_omaha():
    body = """<?xml version="1.0" encoding="UTF-8"?>
<request protocol="3.0" updater="Omaha" updaterversion="1.3.36.372" shell_version="1.3.36.352" ismachine="0" sessionid="{11111111-1111-1111-1111-111111111111}" installsource="taggedmi" requestid="{11111111-1111-1111-1111-111111111111}" dedup="cr" domainjoined="0">
  <hw physmemory="16" sse="1" sse2="1" sse3="1" ssse3="1" sse41="1" sse42="1" avx="1"/>
  <os platform="win" version="10.0" sp="" arch="x64"/>
  <app appid="{8A69D345-D564-463C-AFF1-A69D9E530F96}" version="" nextversion="" lang="en" brand="" installage="-1" installdate="-1" iid="{11111111-1111-1111-1111-111111111111}">
    <updatecheck/>
    <data name="install" index="empty"/>
  </app>
</request>"""
    req = urllib.request.Request(
        GOOGLE_OMAHA_URL,
        data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml", "User-Agent": "Google Update/1.3.36.372"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = resp.read().decode("utf-8")
        root = ET.fromstring(res)
        app = root.find("app")
        if app is None:
            raise RuntimeError("Omaha response missing <app>")
        updatecheck = app.find("updatecheck")
        if updatecheck is None:
            raise RuntimeError("Omaha response missing <updatecheck>")
        manifest = updatecheck.find("manifest")
        if manifest is None:
            raise RuntimeError("Omaha response missing <manifest>")
        version = manifest.attrib.get("version")
        package = manifest.find("packages").find("package")
        pkg_name = package.attrib.get("name")
        pkg_size = int(package.attrib.get("size", "0"))
        pkg_sha256 = package.attrib.get("hash_sha256")
        urls = [
            u.attrib.get("codebase") + pkg_name
            for u in updatecheck.find("urls").findall("url")
        ]
        # Prefer HTTPS google.com URL if available
        chosen_url = None
        for u in urls:
            if u.startswith("https://") and "google.com" in u:
                chosen_url = u
                break
        if not chosen_url:
            chosen_url = urls[0] if urls else ""

        return {
            "version": version,
            "url": chosen_url,
            "size": pkg_size,
            "sha256": pkg_sha256,
        }


def get_latest_chrome_fallback():
    req = urllib.request.Request(
        BUSH_INSTALLER_DATA_URL,
        headers={"User-Agent": "ChromePortableChecker"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        entry = data.get("win_stable_x64")
        if not entry:
            raise RuntimeError("No win_stable_x64 in Bush2021 data.json")
        urls = entry.get("urls", [])
        chosen_url = urls[0] if urls else ""
        for u in urls:
            if u.startswith("https://") and "google.com" in u:
                chosen_url = u
                break
        return {
            "version": entry.get("version"),
            "url": chosen_url,
            "size": entry.get("size", 0),
            "sha256": entry.get("sha256"),
        }


def get_latest_chrome():
    try:
        return get_latest_chrome_from_omaha()
    except Exception as e:
        print(f"[WARN] Omaha check failed ({e}), falling back to Bush2021/chrome_installer...", file=sys.stderr)
        return get_latest_chrome_fallback()


def get_latest_chrome_plus():
    data = github_request(CHROME_PLUS_API_URL)
    tag = data.get("tag_name") or data.get("name") or ""
    assets = data.get("assets", [])
    setdll_asset = next((a for a in assets if a.get("name") == "setdll.7z"), None)
    if not setdll_asset:
        raise RuntimeError(f"setdll.7z not found in chrome_plus {tag}")
    return {
        "version": tag,
        "url": setdll_asset.get("browser_download_url"),
        "size": setdll_asset.get("size", 0),
    }


def check_tag_exists(repo, tag, token=None):
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        data = github_request(url, token=token)
        # Verify that it actually has assets attached
        assets = data.get("assets", [])
        has_7z = any(a.get("name", "").endswith(".7z") for a in assets)
        return has_7z
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def set_github_output(outputs):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            for k, v in outputs.items():
                f.write(f"{k}={v}\n")


def main():
    parser = argparse.ArgumentParser(description="Check for Chrome and Chrome++ updates.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO), help="Target GitHub repo")
    parser.add_argument("--force", action="store_true", help="Force update_needed=true")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    print("=== [1/3] 检测 Google Chrome 最新稳定版 ===")
    chrome_info = get_latest_chrome()
    print(f"Chrome 最新版: {chrome_info['version']}")
    print(f"下载直链: {chrome_info['url']}")
    print(f"安装包大小: {chrome_info['size']} 字节, SHA256: {chrome_info['sha256']}")

    print("\n=== [2/3] 检测 Chrome++ 插件最新版 ===")
    plus_info = get_latest_chrome_plus()
    print(f"Chrome++ 最新版: {plus_info['version']}")
    print(f"插件包下载直链: {plus_info['url']}")

    release_tag = f"v{chrome_info['version']}-{plus_info['version']}"
    release_title = f"Chrome++ v{chrome_info['version']} (插件 {plus_info['version']})"

    print(f"\n=== [3/3] 检查目标仓库 Release 状态 ({args.repo}) ===")
    print(f"目标 Tag: {release_tag}")

    already_released = check_tag_exists(args.repo, release_tag)
    if args.force:
        print("[INFO] 强制触发构建模式已开启 (--force)")
        update_needed = True
    else:
        update_needed = not already_released

    if update_needed:
        print(f"[RESULT] 检测到更新！需要打包发布 (Tag {release_tag} 尚未发布或强制触发)")
    else:
        print(f"[RESULT] 当前已是最新版本 (Tag {release_tag} 已存在)，无需重新打包。")

    result = {
        "update_needed": str(update_needed).lower(),
        "chrome_version": chrome_info["version"],
        "chrome_url": chrome_info["url"],
        "chrome_sha256": chrome_info["sha256"],
        "chrome_size": str(chrome_info["size"]),
        "chrome_plus_version": plus_info["version"],
        "chrome_plus_url": plus_info["url"],
        "release_tag": release_tag,
        "release_title": release_title,
    }

    set_github_output(result)

    if args.json:
        print("\n" + json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

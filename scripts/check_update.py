#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双更新检测脚本 (Chrome + Chrome++)
检查 Google 官方 Chrome 最新版与 Bush2021/chrome_plus 最新版插件。
任一更新（且未发生版本倒退、对应 Release Tag 尚未在仓库中发布）即判定为 update_needed=true。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from common import (
    DEFAULT_REPO,
    archive_filename,
    configure_utf8_stdio,
    get_github_token,
    urlopen_with_retry,
)

configure_utf8_stdio()

GOOGLE_OMAHA_URL = "https://tools.google.com/service/update2"
BUSH_INSTALLER_DATA_URL = "https://raw.githubusercontent.com/Bush2021/chrome_installer/main/data.json"
CHROME_PLUS_API_URL = "https://api.github.com/repos/Bush2021/chrome_plus/releases/latest"

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)+)-v?(\d+(?:\.\d+)+)$")


def parse_version_tuple(version_str):
    """将点分版本号（支持前导 v）解析为可比较的整数元组。"""
    cleaned = (version_str or "").strip().lstrip("vV")
    if not _VERSION_RE.match(cleaned):
        raise ValueError(f"无效版本号格式: {version_str!r}")
    return tuple(int(part) for part in cleaned.split("."))


def validate_chrome_info(info):
    version = (info.get("version") or "").strip()
    url = (info.get("url") or "").strip()
    sha256 = (info.get("sha256") or "").strip().lower()
    size = int(info.get("size") or 0)

    if not _VERSION_RE.match(version):
        raise RuntimeError(f"Chrome 版本号无效或为空: {version!r}")
    if not url.startswith(("https://", "http://")):
        raise RuntimeError(f"Chrome 安装包下载链接无效: {url!r}")
    if not _SHA256_RE.match(sha256):
        raise RuntimeError(f"Chrome 安装包 SHA256 无效: {sha256!r}")

    return {"version": version, "url": url, "size": size, "sha256": sha256}


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
    with urlopen_with_retry(req, timeout=30) as resp:
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
    with urlopen_with_retry(req, timeout=30) as resp:
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
        packages_el = manifest.find("packages")
        package = packages_el.find("package") if packages_el is not None else None
        if package is None:
            raise RuntimeError("Omaha response missing <package>")

        version = manifest.attrib.get("version") or ""
        pkg_name = package.attrib.get("name") or ""
        if not pkg_name:
            raise RuntimeError("Omaha response missing package name")
        pkg_size = int(package.attrib.get("size") or "0")
        pkg_sha256 = package.attrib.get("hash_sha256") or ""

        urls_el = updatecheck.find("urls")
        url_nodes = urls_el.findall("url") if urls_el is not None else []
        urls = [
            (u.attrib.get("codebase") or "") + pkg_name
            for u in url_nodes
            if u.attrib.get("codebase")
        ]
        chosen_url = next(
            (u for u in urls if u.startswith("https://") and "google.com" in u),
            urls[0] if urls else "",
        )

        return validate_chrome_info({
            "version": version,
            "url": chosen_url,
            "size": pkg_size,
            "sha256": pkg_sha256,
        })


def get_latest_chrome_fallback():
    req = urllib.request.Request(
        BUSH_INSTALLER_DATA_URL,
        headers={"User-Agent": "ChromePortableChecker"},
    )
    with urlopen_with_retry(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        entry = data.get("win_stable_x64")
        if not entry:
            raise RuntimeError("No win_stable_x64 in Bush2021 data.json")
        urls = entry.get("urls") or []
        chosen_url = next(
            (u for u in urls if u.startswith("https://") and "google.com" in u),
            urls[0] if urls else "",
        )
        return validate_chrome_info({
            "version": entry.get("version"),
            "url": chosen_url,
            "size": entry.get("size", 0),
            "sha256": entry.get("sha256"),
        })


def get_latest_chrome():
    try:
        return get_latest_chrome_from_omaha()
    except Exception as e:
        print(f"[WARN] Omaha check failed ({e}), falling back to Bush2021/chrome_installer...", file=sys.stderr)
        return get_latest_chrome_fallback()


def get_latest_chrome_plus():
    data = github_request(CHROME_PLUS_API_URL)
    tag = (data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        raise RuntimeError("chrome_plus latest release missing tag_name")
    assets = data.get("assets") or []
    setdll_asset = next((a for a in assets if a.get("name") == "setdll.7z"), None)
    if not setdll_asset:
        raise RuntimeError(f"setdll.7z not found in chrome_plus {tag}")

    url = (setdll_asset.get("browser_download_url") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise RuntimeError(f"Invalid setdll.7z download url in chrome_plus {tag}: {url!r}")

    digest = setdll_asset.get("digest") or ""
    sha256 = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    if sha256 and not _SHA256_RE.match(sha256):
        sha256 = ""

    return {
        "version": tag,
        "url": url,
        "size": int(setdll_asset.get("size") or 0),
        "sha256": sha256,
    }


def check_tag_exists(repo, tag, expected_asset=None, token=None):
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        data = github_request(url, token=token)
        if data.get("draft"):
            return False
        assets = data.get("assets") or []
        if expected_asset:
            return any(
                a.get("name") == expected_asset and a.get("state", "uploaded") == "uploaded"
                for a in assets
            )
        return any(
            a.get("name", "").endswith(".7z") and a.get("state", "uploaded") == "uploaded"
            for a in assets
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def get_latest_published_versions(repo, token=None):
    """获取仓库最新正式 Release 的 (chrome_version, plus_version)，用于防止 Omaha 灰度回退。"""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        data = github_request(url, token=token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    tag = (data.get("tag_name") or "").strip()
    match = _TAG_RE.match(tag)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_version_downgrade(chrome_ver, plus_ver, published_pair):
    """任一组件版本号低于仓库最新正式 Release，则视为版本回退。"""
    if not published_pair:
        return False
    pub_chrome, pub_plus = published_pair
    try:
        return (
            parse_version_tuple(chrome_ver) < parse_version_tuple(pub_chrome)
            or parse_version_tuple(plus_ver) < parse_version_tuple(pub_plus)
        )
    except ValueError:
        return False


def set_github_output(outputs):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            for k, v in outputs.items():
                clean_v = str(v).replace("\r", "").replace("\n", "")
                f.write(f"{k}={clean_v}\n")


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
    print(f"插件包大小: {plus_info['size']} 字节, SHA256: {plus_info['sha256'] or '(未提供)'}")

    release_tag = f"v{chrome_info['version']}-{plus_info['version']}"
    release_title = f"Chrome Portable v{chrome_info['version']} (Chrome++ {plus_info['version']})"
    expected_archive = archive_filename(chrome_info["version"], plus_info["version"])

    print(f"\n=== [3/3] 检查目标仓库 Release 状态 ({args.repo}) ===")
    print(f"目标 Tag: {release_tag}, 目标产物: {expected_archive}")

    already_released = check_tag_exists(args.repo, release_tag, expected_asset=expected_archive)
    latest_published = get_latest_published_versions(args.repo)
    if latest_published:
        print(f"当前已发布最新版本: Chrome {latest_published[0]} / Chrome++ {latest_published[1]}")

    if args.force:
        print("[INFO] 强制触发构建模式已开启 (--force)")
        update_needed = True
    elif already_released:
        update_needed = False
    elif is_version_downgrade(chrome_info["version"], plus_info["version"], latest_published):
        print(
            f"[WARN] 检测到上游版本回退 (候选: {chrome_info['version']} / {plus_info['version']}，"
            f"线上最新: {latest_published[0]} / {latest_published[1]})，跳过构建以防旧版覆盖 Latest。",
            file=sys.stderr,
        )
        update_needed = False
    else:
        update_needed = True

    if update_needed:
        print(f"[RESULT] 检测到更新！需要打包发布 (Tag {release_tag} 或目标资产尚未发布，或强制触发)")
    else:
        print(f"[RESULT] 无需重新打包 (Tag {release_tag} 已发布或检测到上游版本回退)。")

    result = {
        "update_needed": str(update_needed).lower(),
        "chrome_version": chrome_info["version"],
        "chrome_url": chrome_info["url"],
        "chrome_sha256": chrome_info["sha256"],
        "chrome_size": str(chrome_info["size"]),
        "chrome_plus_version": plus_info["version"],
        "chrome_plus_url": plus_info["url"],
        "chrome_plus_size": str(plus_info["size"]),
        "chrome_plus_sha256": plus_info["sha256"],
        "release_tag": release_tag,
        "release_title": release_title,
    }

    set_github_output(result)

    if args.json:
        print("\n" + json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

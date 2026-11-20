#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Release 发布辅助脚本
自动创建或更新 Release，渲染发布说明，并上传 .7z 与 .sha256 产物文件。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def format_size(bytes_num):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} TB"


def render_release_body(chrome_version, plus_version, archive_name, archive_size, archive_sha256):
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    body = f"""自动构建的 Google Chrome 64位 Windows 便携版，集成 [Chrome++] 增强插件。

> **安装与使用说明**：
> - 解压至任意目录，双击 `开始.bat` 即可在解压根目录生成桌面/本地快捷方式 `Chrome.lnk`。
> - 用户数据默认隔离保存在 `Chrome` 同级的 `Data` 与 `Cache` 文件夹，随目录整体移动，绿色便携。
> - 插件已默认开启 **禁止从注册表读取企业版策略**（`ignore_policies=1`）。
> - 已移除 `GoogleUpdate` 等常驻后台及自动更新组件。

---

### 版本信息
- **Google Chrome 版本**：`{chrome_version}`
- **Chrome++ 增强插件**：`{plus_version}`
- **打包时间**：`{date_str}`

---

### 文件校验
| 文件名 | 文件大小 | SHA256 校验码 |
| :--- | :--- | :--- |
| `{archive_name}` | {format_size(archive_size)} | `{archive_sha256}` |

> 同目录下附带 `{archive_name}.sha256` 校验文件，可在终端中执行 `sha256sum -c {archive_name}.sha256` 进行完整性核验。

---

### 相关项目
- Google Chrome 离线包追踪：[Bush2021/chrome_installer](https://github.com/Bush2021/chrome_installer)
- Chrome++ 插件源：[Bush2021/chrome_plus](https://github.com/Bush2021/chrome_plus)
- 架构参考：[Piracola/Chrome-Portable](https://github.com/Piracola/Chrome-Portable)
"""
    return body


def create_or_get_release(repo, tag, title, body, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ChromePortableReleaseHelper",
    }
    # 检查是否已存在
    get_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(get_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[RELEASE] 发现已存在的 Release: {data.get('html_url')}")
            # 更新 body 和 title
            update_url = f"https://api.github.com/repos/{repo}/releases/{data['id']}"
            update_data = json.dumps({"name": title, "body": body}).encode("utf-8")
            update_req = urllib.request.Request(update_url, data=update_data, headers=headers, method="PATCH")
            with urllib.request.urlopen(update_req, timeout=30) as u_resp:
                return json.loads(u_resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    # 不存在则创建
    print(f"[RELEASE] 正在创建新 Release: {tag} - {title} ...")
    create_url = f"https://api.github.com/repos/{repo}/releases"
    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": False,
        "prerelease": False,
    }
    create_req = urllib.request.Request(
        create_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(create_req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_asset(upload_url_template, file_path, token):
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # upload_url_template 通常形如 "https://uploads.github.com/repos/.../assets{?name,label}"
    base_upload_url = upload_url_template.split("{")[0]
    encoded_name = urllib.parse.quote(file_path.name)
    upload_url = f"{base_upload_url}?name={encoded_name}"

    content_type = "application/x-7z-compressed" if file_path.suffix == ".7z" else "text/plain"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "User-Agent": "ChromePortableReleaseHelper",
    }

    file_bytes = file_path.read_bytes()
    req = urllib.request.Request(upload_url, data=file_bytes, headers=headers, method="POST")
    print(f"[UPLOAD] 正在上传资产: {file_path.name} ({len(file_bytes) // (1024*1024)} MB) ...")
    with urllib.request.urlopen(req, timeout=600) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print(f"[UPLOAD] 资产上传成功: {res.get('browser_download_url')}")
        return res


def main():
    parser = argparse.ArgumentParser(description="Publish Chrome Portable release to GitHub.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO), help="Target GitHub repo")
    parser.add_argument("--chrome-version", required=True, help="Chrome version")
    parser.add_argument("--plus-version", required=True, help="Chrome++ version")
    parser.add_argument("--archive-path", required=True, help="Path to .7z archive")
    args = parser.parse_args()

    token = get_github_token()
    if not token:
        raise RuntimeError("未找到 GitHub Token，无法发布 Release。")

    archive_path = Path(args.archive_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"未找到压缩包: {archive_path}")

    sha256_path = archive_path.parent / f"{archive_path.name}.sha256"
    if not sha256_path.exists():
        raise FileNotFoundError(f"未找到 sha256 校验文件: {sha256_path}")

    import hashlib
    archive_size = archive_path.stat().st_size
    sha256_text = hashlib.sha256(archive_path.read_bytes()).hexdigest().lower()

    tag = f"v{args.chrome_version}-{args.plus_version}"
    title = f"Chrome++ v{args.chrome_version} (插件 {args.plus_version})"
    body = render_release_body(args.chrome_version, args.plus_version, archive_path.name, archive_size, sha256_text)

    # 1. 创建 Release
    release_data = create_or_get_release(args.repo, tag, title, body, token)
    release_id = release_data["id"]
    upload_url = release_data["upload_url"]

    # 2. 删除已有同名资产（如果存在）
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ChromePortableReleaseHelper",
    }
    existing_assets = release_data.get("assets", [])
    target_names = {
        archive_path.name,
        sha256_path.name,
        archive_path.name.replace("++", " "),
        archive_path.name.replace("++", "."),
        sha256_path.name.replace("++", " "),
        sha256_path.name.replace("++", "."),
    }
    for asset in existing_assets:
        if asset.get("name") in target_names or asset.get("name", "").startswith("Chrome"):
            print(f"[ASSET] 发现旧资产 {asset.get('name')}，正在删除...")
            del_req = urllib.request.Request(asset["url"], headers=headers, method="DELETE")
            try:
                urllib.request.urlopen(del_req, timeout=30)
            except Exception as e:
                print(f"[WARN] 删除旧资产失败: {e}")

    # 3. 上传新资产
    upload_asset(upload_url, archive_path, token)
    upload_asset(upload_url, sha256_path, token)

    print(f"\n[SUCCESS] 发布成功！访问地址: {release_data.get('html_url')}")


if __name__ == "__main__":
    main()

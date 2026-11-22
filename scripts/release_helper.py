#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Release 原子发布辅助脚本
采用“先草稿 → 交叉校验哈希 → 流式上传资产（重试前清理残缺项）→ 正式公开”的原子发布流程。
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import (
    DEFAULT_REPO,
    calc_sha256,
    configure_utf8_stdio,
    get_github_token,
    urlopen_with_retry,
)

configure_utf8_stdio()

BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def format_size(bytes_num):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} TB"


def render_release_body(chrome_version, plus_version, archive_name, archive_size, archive_sha256):
    date_str = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    return f"""自动构建的 Google Chrome 64位 Windows 便携版，集成 [Chrome++](https://github.com/Bush2021/chrome_plus) 增强插件。

> **安装与使用说明**：
> - 解压至任意目录，双击 `开始.bat` 即可在解压根目录生成本地快捷方式 `Chrome.lnk`。
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


def _github_headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ChromePortableReleaseHelper",
    }


def find_existing_release(repo, tag, token):
    """查找已发布的同 tag Release，或同 tag_name 的草稿 Release。"""
    headers = _github_headers(token)
    get_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        req = urllib.request.Request(get_url, headers=headers)
        with urlopen_with_retry(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    list_url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    req = urllib.request.Request(list_url, headers=headers)
    with urlopen_with_retry(req, timeout=30) as resp:
        for rel in json.loads(resp.read().decode("utf-8")):
            if rel.get("tag_name") == tag:
                return rel
    return None


def prepare_release(repo, tag, title, body, token):
    """若存在则更新元数据；若不存在则先创建 draft=True 草稿，等待资产上传完成后再公开。"""
    headers = _github_headers(token)
    existing = find_existing_release(repo, tag, token)
    if existing:
        print(f"[RELEASE] 发现已存在的 Release (draft={existing.get('draft')}): {existing.get('html_url')}")
        update_url = f"https://api.github.com/repos/{repo}/releases/{existing['id']}"
        update_data = json.dumps({"name": title, "body": body}).encode("utf-8")
        update_req = urllib.request.Request(update_url, data=update_data, headers=headers, method="PATCH")
        with urlopen_with_retry(update_req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    print(f"[RELEASE] 正在创建草稿 Release: {tag} - {title} ...")
    create_url = f"https://api.github.com/repos/{repo}/releases"
    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": True,
        "prerelease": False,
    }
    create_req = urllib.request.Request(
        create_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen_with_retry(create_req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_assets_by_names(repo, release_id, target_names, token):
    """实时拉取 Release 资产列表并精确删除同名旧资产或上次中断遗留的 starter 残缺资产。"""
    headers = _github_headers(token)
    assets_url = f"https://api.github.com/repos/{repo}/releases/{release_id}/assets?per_page=100"
    req = urllib.request.Request(assets_url, headers=headers)
    with urlopen_with_retry(req, timeout=30) as resp:
        assets = json.loads(resp.read().decode("utf-8"))

    for asset in assets:
        name = asset.get("name")
        if name in target_names:
            print(f"[ASSET] 删除同名/残缺资产: {name} (state={asset.get('state')}) ...")
            del_req = urllib.request.Request(asset["url"], headers=headers, method="DELETE")
            with urlopen_with_retry(del_req, timeout=30):
                pass


def publish_draft_release(repo, release_id, token):
    """将草稿 Release 翻转为正式公开状态。"""
    headers = _github_headers(token)
    url = f"https://api.github.com/repos/{repo}/releases/{release_id}"
    payload = json.dumps({"draft": False}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
    with urlopen_with_retry(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_asset_streaming(repo, release_id, upload_url_template, file_path, token, retries=3):
    """流式上传大文件资产（避免一次性读入内存）；失败重试前自动清理服务端残缺资产防止 422。"""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    base_upload_url = upload_url_template.split("{")[0]
    encoded_name = urllib.parse.quote(file_path.name)
    upload_url = f"{base_upload_url}?name={encoded_name}"

    file_size = file_path.stat().st_size
    content_type = "application/x-7z-compressed" if file_path.suffix == ".7z" else "text/plain"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "Content-Length": str(file_size),
        "User-Agent": "ChromePortableReleaseHelper",
    }

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[UPLOAD] 正在流式上传资产: {file_path.name} ({format_size(file_size)}) [第 {attempt}/{retries} 次] ...")
            with open(file_path, "rb") as f:
                req = urllib.request.Request(upload_url, data=f, headers=headers, method="POST")
                with urlopen_with_retry(req, timeout=600, retries=1) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    print(f"[UPLOAD] 资产上传成功: {res.get('browser_download_url')}")
                    return res
        except urllib.error.HTTPError as e:
            # 4xx 中仅 429 限流与 422（可能由上次断流残留 starter 资产引起）值得清理后重试
            if 400 <= e.code < 500 and e.code not in (422, 429):
                raise
            last_err = e
        except Exception as e:
            last_err = e

        if attempt < retries:
            wait = 3 * attempt
            print(f"[RETRY] 上传失败 ({last_err})，清理可能残留的同名资产并在 {wait}s 后重试...", file=sys.stderr)
            try:
                delete_assets_by_names(repo, release_id, {file_path.name}, token)
            except Exception as clean_err:
                print(f"[WARN] 清理残缺资产失败: {clean_err}", file=sys.stderr)
            time.sleep(wait)
    raise last_err


def verify_sha256_sidecar(archive_path, sha256_path):
    """分块流式计算归档 SHA256，并与 .sha256 校验文件做严格交叉核验。"""
    actual_sha = calc_sha256(archive_path)
    sidecar_content = sha256_path.read_text(encoding="utf-8").strip()
    parts = sidecar_content.split()
    if len(parts) < 2:
        raise RuntimeError(f".sha256 文件格式无效: {sidecar_content!r}")
    sidecar_sha, sidecar_name = parts[0].lower(), parts[-1]
    if sidecar_sha != actual_sha:
        raise RuntimeError(
            f"SHA256 交叉校验失败！归档实际={actual_sha}，.sha256 记录={sidecar_sha}"
        )
    if sidecar_name != archive_path.name:
        raise RuntimeError(
            f".sha256 文件名不匹配！归档={archive_path.name}，.sha256 记录={sidecar_name}"
        )
    return actual_sha


def main():
    parser = argparse.ArgumentParser(description="Publish Chrome Portable release to GitHub atomically.")
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

    archive_size = archive_path.stat().st_size
    sha256_text = verify_sha256_sidecar(archive_path, sha256_path)
    print(f"[CHECKSUM] 归档与 .sha256 交叉校验通过: {sha256_text}")

    tag = f"v{args.chrome_version}-{args.plus_version}"
    title = f"Chrome Portable v{args.chrome_version} (Chrome++ {args.plus_version})"
    body = render_release_body(args.chrome_version, args.plus_version, archive_path.name, archive_size, sha256_text)

    # 1. 创建草稿或获取已有 Release
    release_data = prepare_release(args.repo, tag, title, body, token)
    release_id = release_data["id"]
    upload_url = release_data["upload_url"]

    # 2. 精确删除与本次上传同名的旧资产/残缺资产
    delete_assets_by_names(args.repo, release_id, {archive_path.name, sha256_path.name}, token)

    # 3. 流式上传资产（重试前自动清理残缺项）
    upload_asset_streaming(args.repo, release_id, upload_url, archive_path, token)
    upload_asset_streaming(args.repo, release_id, upload_url, sha256_path, token)

    # 4. 若为草稿，全部上传成功后再正式公开
    if release_data.get("draft"):
        print("[RELEASE] 所有资产上传完毕，正在将草稿转为正式发布...")
        release_data = publish_draft_release(args.repo, release_id, token)

    print(f"\n[SUCCESS] 原子发布成功！访问地址: {release_data.get('html_url')}")


if __name__ == "__main__":
    main()

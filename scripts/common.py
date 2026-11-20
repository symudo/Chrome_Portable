#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Portable 公共工具模块
提供 UTF-8 标准流配置、GitHub 凭据获取、带重试与跨站重定向脱敏的 HTTP 请求以及流式 SHA256 计算。
"""

import hashlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REPO = "symudo/Chrome_Portable"
ARCHIVE_PREFIX = "ChromePortable"


def configure_utf8_stdio():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def archive_filename(chrome_version, plus_version, arch="x64"):
    return f"{ARCHIVE_PREFIX}_{chrome_version}_{plus_version}_{arch}.7z"


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


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """跨主机重定向时剥离 Authorization 头，避免向第三方对象存储 CDN 泄露令牌并触发 401。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc.lower()
            new_host = urllib.parse.urlparse(newurl).netloc.lower()
            if old_host != new_host:
                new_req.remove_header("Authorization")
        return new_req


_OPENER = urllib.request.build_opener(_StripAuthOnCrossHostRedirect)


def urlopen_with_retry(req_or_url, timeout=30, retries=3, backoff=2.0):
    """带指数退避重试的 urlopen，仅对网络抖动、超时、429 及 5xx 错误重试。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return _OPENER.open(req_or_url, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 4xx（除 429 限流外）属于确定性错误，不重试
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e

        if attempt < retries:
            wait = backoff * (2 ** (attempt - 1))
            print(f"[RETRY] 请求失败 ({last_err})，{wait:.0f}s 后进行第 {attempt + 1}/{retries} 次重试...", file=sys.stderr)
            time.sleep(wait)

    raise last_err


def calc_sha256(file_path):
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest().lower()

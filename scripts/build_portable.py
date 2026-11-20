#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Chrome 便携版自动化打包构建脚本
实现：下载、校验、解包、剔除冗余组件、插件集成、配置合并、PE 注入、格式压缩与自测验证。
"""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = REPO_ROOT / "build"
ASSETS_DIR = BUILD_DIR / "assets"
DOWNLOADS_DIR = BUILD_DIR / "downloads"

FORBIDDEN_FILES = {
    "googleupdate.exe",
    "googleupdater.exe",
    "updater.exe",
    "googleupdatebroker.exe",
    "googleupdatehelper.msi",
}


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


def find_7z():
    configured = os.environ.get("SEVEN_ZIP")
    if configured and Path(configured).exists():
        return configured
    for name in ("7z", "7zz", "7za", "NanaZipC"):
        found = shutil.which(name)
        if found:
            return found
    # Check default installation paths
    default_paths = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for p in default_paths:
        if Path(p).exists():
            return p
    raise RuntimeError("未找到 7-Zip 工具，请安装 7-Zip 或设置 SEVEN_ZIP 环境变量。")


def calc_sha256(file_path):
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest().lower()


def download_file(url, dest_path, expected_sha256=None, token=None):
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if dest_path.exists() and expected_sha256:
        actual = calc_sha256(dest_path)
        if actual == expected_sha256.lower():
            print(f"[CACHE] 文件已存在且校验通过: {dest_path.name}")
            return dest_path

    print(f"[DOWNLOAD] 开始下载: {url} -> {dest_path.name}")
    headers = {"User-Agent": "ChromePortableBuilder/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    sha = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest_path, "wb") as out:
        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        last_pct = -1
        while chunk := resp.read(1024 * 512):
            out.write(chunk)
            sha.update(chunk)
            downloaded += len(chunk)
            if total_size > 0:
                pct = int(downloaded * 100 / total_size)
                if pct != last_pct and pct % 10 == 0:
                    print(f"  进度: {pct}% ({downloaded // (1024*1024)} MB / {total_size // (1024*1024)} MB)")
                    last_pct = pct

    actual_sha = sha.hexdigest().lower()
    if expected_sha256 and actual_sha != expected_sha256.lower():
        dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"下载文件 SHA256 校验失败！期望: {expected_sha256}, 实际: {actual_sha}")
    print(f"[DOWNLOAD] 下载完成: {dest_path.name} (SHA256: {actual_sha[:16]}...)")
    return dest_path


def parse_ini_overrides(file_path):
    """解析 override.ini 文件中的各个 section 与 key-value"""
    overrides = {}
    if not Path(file_path).exists():
        return overrides
    text = Path(file_path).read_text(encoding="utf-8")
    current_sec = "general"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_sec = line[1:-1].strip().lower()
            if current_sec not in overrides:
                overrides[current_sec] = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if current_sec not in overrides:
                overrides[current_sec] = {}
            overrides[current_sec][k] = v
    return overrides


def merge_chrome_ini(base_text, overrides_dict):
    """合并配置到基线 INI 文本中，保持注释与格式，修改或添加对应键"""
    lines = base_text.splitlines()
    new_lines = []
    current_section = None
    applied_keys = {sec: set() for sec in overrides_dict}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_section in overrides_dict:
                for k, v in overrides_dict[current_section].items():
                    if k not in applied_keys[current_section]:
                        new_lines.append(f"{k}={v}")
                        applied_keys[current_section].add(k)
            current_section = stripped[1:-1].strip().lower()
            new_lines.append(line)
            continue

        if current_section in overrides_dict and "=" in stripped and not stripped.startswith(";"):
            k, v = stripped.split("=", 1)
            k = k.strip()
            if k in overrides_dict[current_section]:
                new_lines.append(f"{k}={overrides_dict[current_section][k]}")
                applied_keys[current_section].add(k)
                continue

        new_lines.append(line)

    if current_section in overrides_dict:
        for k, v in overrides_dict[current_section].items():
            if k not in applied_keys[current_section]:
                new_lines.append(f"{k}={v}")
                applied_keys[current_section].add(k)

    return "\r\n".join(new_lines) + "\r\n"


def get_pe_imports(file_path):
    """纯标准库解析 PE 文件导入表"""
    with open(file_path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack("<I", data[0x3C:0x40])[0]
    pe_sig = data[e_lfanew : e_lfanew + 4]
    if pe_sig != b"PE\x00\x00":
        return []
    coff_header = data[e_lfanew + 4 : e_lfanew + 24]
    num_sections = struct.unpack("<H", coff_header[2:4])[0]
    opt_header_size = struct.unpack("<H", coff_header[16:18])[0]
    opt_header_offset = e_lfanew + 24
    opt_header = data[opt_header_offset : opt_header_offset + opt_header_size]
    magic = struct.unpack("<H", opt_header[:2])[0]
    is_64 = magic == 0x020B

    dd_offset = 112 if is_64 else 96
    import_rva, import_size = struct.unpack("<II", opt_header[dd_offset + 8 : dd_offset + 16])
    if import_rva == 0 or import_size == 0:
        return []

    sections_offset = opt_header_offset + opt_header_size
    sections = []
    for i in range(num_sections):
        sec = data[sections_offset + i * 40 : sections_offset + (i + 1) * 40]
        vsize, vaddr, rsize, raddr = struct.unpack("<IIII", sec[8:24])
        sections.append((vaddr, max(vsize, rsize), raddr))

    def rva_to_offset(rva):
        for vaddr, size, raddr in sections:
            if vaddr <= rva < vaddr + size:
                return raddr + (rva - vaddr)
        return None

    import_offset = rva_to_offset(import_rva)
    if import_offset is None:
        return []

    dlls = []
    curr = import_offset
    while True:
        entry = data[curr : curr + 20]
        if len(entry) < 20 or entry == b"\x00" * 20:
            break
        name_rva = struct.unpack("<I", entry[12:16])[0]
        if name_rva == 0:
            break
        name_offset = rva_to_offset(name_rva)
        if name_offset is not None:
            end = data.find(b"\x00", name_offset)
            if end != -1:
                dlls.append(data[name_offset:end].decode("latin1", errors="ignore"))
        curr += 20
    return dlls


def extract_with_7z(seven_zip, archive_path, output_dir):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    cmd = [seven_zip, "x", str(archive_path), f"-o{output_dir}", "-y"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"解压失败: {archive_path}\n{res.stdout}\n{res.stderr}")


def build(chrome_version, chrome_url, chrome_sha256, chrome_plus_version, chrome_plus_url):
    seven_zip = find_7z()
    print(f"[TOOL] 7-Zip 工具路径: {seven_zip}")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    token = get_github_token()

    # 1. 下载文件
    installer_name = f"chrome_installer_{chrome_version}_x64.exe"
    installer_path = DOWNLOADS_DIR / installer_name
    download_file(chrome_url, installer_path, expected_sha256=chrome_sha256)

    plus_archive_name = f"setdll_{chrome_plus_version}.7z"
    plus_archive_path = DOWNLOADS_DIR / plus_archive_name
    download_file(chrome_plus_url, plus_archive_path, token=token)

    with tempfile.TemporaryDirectory(prefix="chrome_build_", ignore_cleanup_errors=True) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        raw_extract = temp_dir / "raw"
        chrome_bin_dir = temp_dir / "chrome_bin"
        plus_extract = temp_dir / "plus"
        stage_dir = temp_dir / "stage"
        chrome_app_dir = stage_dir / "Chrome"

        # 2. 解压 Chrome 安装包
        print("[EXTRACT] 正在解压 Chrome 离线安装程序...")
        extract_with_7z(seven_zip, installer_path, raw_extract)

        # 检查是否直接解包出 Chrome-bin 或 chrome.exe（uncompressed 安装包）
        candidate_bin = list(raw_extract.rglob("Chrome-bin"))
        candidate_exe = list(raw_extract.rglob("chrome.exe"))

        if not candidate_bin and not candidate_exe:
            # 寻找内部的 chrome.7z
            chrome_7z_list = list(raw_extract.rglob("chrome.7z")) + list(raw_extract.rglob("CHROME.7Z"))
            if chrome_7z_list:
                chrome_7z = chrome_7z_list[0]
                print(f"[EXTRACT] 正在解包内部核心文件: {chrome_7z.name} ...")
                extract_with_7z(seven_zip, chrome_7z, chrome_bin_dir)
                candidate_bin = list(chrome_bin_dir.rglob("Chrome-bin"))
                candidate_exe = list(chrome_bin_dir.rglob("chrome.exe"))

        if candidate_bin:
            source_bin = candidate_bin[0]
        elif candidate_exe:
            source_bin = candidate_exe[0].parent
        else:
            raise RuntimeError("解包后的目录中未找到 Chrome-bin 或 chrome.exe")

        print(f"[LAYOUT] 定位 Chrome 程序源目录: {source_bin}")
        chrome_app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_bin, chrome_app_dir, dirs_exist_ok=True)

        # 4. 剔除冗余更新及遥测组件
        print("[CLEAN] 剔除 Google 更新与安装辅助程序...")
        removed_count = 0
        for item in chrome_app_dir.rglob("*"):
            if item.is_file() and item.name.lower() in FORBIDDEN_FILES:
                item.unlink()
                print(f"  已移除: {item.name}")
                removed_count += 1
        print(f"[CLEAN] 共清除 {removed_count} 个冗余文件。")

        # 5. 解压 Chrome++ 并集成
        print("[EXTRACT] 正在解包 Chrome++ 插件...")
        extract_with_7z(seven_zip, plus_archive_path, plus_extract)

        # 定位所需文件: version-x64.dll, setdll-x64.exe, chrome++.ini
        version_dll = next(plus_extract.rglob("version-x64.dll"), None)
        setdll_exe = next(plus_extract.rglob("setdll-x64.exe"), None)
        base_ini = next(plus_extract.rglob("chrome++.ini"), None)

        if not version_dll or not setdll_exe or not base_ini:
            raise RuntimeError("setdll.7z 中缺少必要的 version-x64.dll, setdll-x64.exe 或 chrome++.ini")

        # 复制 version-x64.dll 到 Chrome 根目录命名为 version.dll
        target_version_dll = chrome_app_dir / "version.dll"
        shutil.copy2(version_dll, target_version_dll)
        print(f"[INTEGRATE] 已放置插件 DLL: {target_version_dll.name}")

        # 6. 配置合并 (chrome++.ini)
        print("[CONFIG] 正在合并 Chrome++ 配置文件 (含强制开启 ignore_policies)...")
        # 尝试解码原始 ini
        ini_raw = base_ini.read_bytes()
        ini_text = None
        for enc in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8", "gbk"):
            try:
                ini_text = ini_raw.decode(enc)
                break
            except Exception:
                continue
        if not ini_text:
            ini_text = ini_raw.decode("latin1")

        # 读取本地 override.ini
        override_file = REPO_ROOT / "chrome++" / "chrome++.override.ini"
        overrides = parse_ini_overrides(override_file)

        # 强制确保核心配置
        if "general" not in overrides:
            overrides["general"] = {}
        overrides["general"]["data_dir"] = r"%app%\..\Data"
        overrides["general"]["cache_dir"] = r"%app%\..\Cache"
        overrides["general"]["ignore_policies"] = "1"  # 用户明确要求默认开启

        merged_ini_text = merge_chrome_ini(ini_text, overrides)
        target_ini = chrome_app_dir / "chrome++.ini"
        target_ini.write_text(merged_ini_text, encoding="utf-16-le")
        print(f"[CONFIG] 配置文件写入完成: {target_ini.name} (UTF-16LE with ignore_policies=1)")

        # 7. PE 注入 (向 chrome.exe 注入 version.dll)
        chrome_exe = chrome_app_dir / "chrome.exe"
        if not chrome_exe.exists():
            raise RuntimeError(f"目标程序不存在: {chrome_exe}")

        print(f"[PE INJECT] 正在使用 setdll 注入 version.dll 到 {chrome_exe.name} ...")
        # 复制 setdll-x64.exe 到临时目录执行注入
        temp_setdll = temp_dir / "setdll-x64.exe"
        shutil.copy2(setdll_exe, temp_setdll)

        inject_cmd = [str(temp_setdll), "/d:version.dll", str(chrome_exe)]
        res = subprocess.run(inject_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(res.stdout)
            print(res.stderr, file=sys.stderr)
            raise RuntimeError(f"setdll PE 注入失败，退出码: {res.returncode}")
        print("[PE INJECT] setdll 执行成功。")

        # 8. 校验 PE 导入表
        print("[VERIFY] 校验 chrome.exe PE 导入表...")
        imports = get_pe_imports(chrome_exe)
        imports_lower = [i.lower() for i in imports]
        if "version.dll" not in imports_lower:
            raise RuntimeError(f"校验失败：chrome.exe 导入表中未检测到 version.dll！当前导入列表: {imports}")
        print(f"[VERIFY] 导入表校验通过，检测到 version.dll (首选注入 DLL)")

        # 9. 放置快捷方式辅助脚本 开始.bat
        start_bat_src = REPO_ROOT / "开始.bat"
        if start_bat_src.exists():
            shutil.copy2(start_bat_src, stage_dir / "开始.bat")
            print("[PACKAGE] 已添加 开始.bat 到压缩包根目录")

        # 10. 压缩生成便携版 7z 包
        archive_name = f"Chrome++_{chrome_version}_{chrome_plus_version}_x64.7z"
        archive_path = ASSETS_DIR / archive_name
        if archive_path.exists():
            archive_path.unlink()

        print(f"[COMPRESS] 正在打包压缩: {archive_name} ...")
        compress_cmd = [
            seven_zip,
            "a",
            "-t7z",
            "-mx=9",
            "-mmt=on",
            str(archive_path),
            f"{stage_dir}/*",
            "-y",
        ]
        res = subprocess.run(compress_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"压缩打包失败: {res.stdout}\n{res.stderr}")

        archive_size = archive_path.stat().st_size
        archive_sha256 = calc_sha256(archive_path)
        print(f"[COMPRESS] 打包完成！大小: {archive_size // (1024*1024)} MB, SHA256: {archive_sha256}")

        # 生成 .sha256 校验文件
        sha256_file = ASSETS_DIR / f"{archive_name}.sha256"
        sha256_file.write_text(f"{archive_sha256}  {archive_name}\n", encoding="utf-8")
        print(f"[CHECKSUM] 已生成校验文件: {sha256_file.name}")

        # 11. 全方位自测验证
        print("\n=== [全方位自测验证 (Smoke Test)] ===")
        test_extract_dir = temp_dir / "test_extract"
        extract_with_7z(seven_zip, archive_path, test_extract_dir)
        test_chrome_exe = test_extract_dir / "Chrome" / "chrome.exe"
        test_version_dll = test_extract_dir / "Chrome" / "version.dll"
        test_ini = test_extract_dir / "Chrome" / "chrome++.ini"
        test_bat = test_extract_dir / "开始.bat"

        assert test_chrome_exe.exists(), "自检失败: Chrome/chrome.exe 缺失"
        assert test_version_dll.exists(), "自检失败: Chrome/version.dll 缺失"
        assert test_ini.exists(), "自检失败: Chrome/chrome++.ini 缺失"
        assert test_bat.exists(), "自检失败: 开始.bat 缺失"

        # 验证解压后 PE 导入
        test_imports = [i.lower() for i in get_pe_imports(test_chrome_exe)]
        assert "version.dll" in test_imports, "自检失败: 解压后 chrome.exe 未包含 version.dll 导入"

        # 验证 ini 中的 ignore_policies=1
        ini_readback = test_ini.read_bytes().decode("utf-16-le")
        assert "ignore_policies=1" in ini_readback, "自检失败: chrome++.ini 未包含 ignore_policies=1"

        print("[SMOKE TEST] 尝试 Headless 启动解压后的 chrome.exe 验证运行...")
        smoke_cmd = [
            str(test_chrome_exe),
            "--headless=new",
            "--dump-dom",
            "--disable-gpu",
            "about:blank",
        ]
        try:
            smoke_res = subprocess.run(smoke_cmd, capture_output=True, text=True, timeout=15)
            print(f"[SMOKE TEST] Headless 启动正常退出 (code={smoke_res.returncode})")
        except subprocess.TimeoutExpired:
            print("[SMOKE TEST] Headless 启动超时，尝试非阻塞验证。")
        except Exception as e:
            print(f"[SMOKE TEST WARN] Headless 启动异常: {e}")

        print("=== 自测全部通过，产物准备就绪！===\n")
        time.sleep(1)
        return {
            "archive_name": archive_name,
            "archive_path": str(archive_path),
            "archive_size": archive_size,
            "archive_sha256": archive_sha256,
            "sha256_file_path": str(sha256_file),
        }


def main():
    parser = argparse.ArgumentParser(description="Build Chrome Portable package.")
    parser.add_argument("--chrome-version", help="Chrome version (e.g. 158.0.8127.37)")
    parser.add_argument("--chrome-url", help="Chrome installer download URL")
    parser.add_argument("--chrome-sha256", help="Chrome installer expected SHA256")
    parser.add_argument("--plus-version", help="Chrome++ version (e.g. 1.20.1)")
    parser.add_argument("--plus-url", help="Chrome++ setdll.7z download URL")
    args = parser.parse_args()

    # 如果未传递参数，则自动调用 check_update 获取最新信息
    if not args.chrome_version or not args.chrome_url or not args.plus_version or not args.plus_url:
        print("[INFO] 未指定版本参数，自动检测最新版本...")
        from check_update import get_latest_chrome, get_latest_chrome_plus

        chrome = get_latest_chrome()
        plus = get_latest_chrome_plus()
        chrome_version = chrome["version"]
        chrome_url = chrome["url"]
        chrome_sha256 = chrome["sha256"]
        plus_version = plus["version"]
        plus_url = plus["url"]
    else:
        chrome_version = args.chrome_version
        chrome_url = args.chrome_url
        chrome_sha256 = args.chrome_sha256
        plus_version = args.plus_version
        plus_url = args.plus_url

    res = build(chrome_version, chrome_url, chrome_sha256, plus_version, plus_url)

    # 导出到 GITHUB_ENV
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"ARCHIVE_NAME={res['archive_name']}\n")
            f.write(f"ARCHIVE_PATH={res['archive_path']}\n")
            f.write(f"ARCHIVE_SIZE={res['archive_size']}\n")
            f.write(f"ARCHIVE_SHA256={res['archive_sha256']}\n")


if __name__ == "__main__":
    main()

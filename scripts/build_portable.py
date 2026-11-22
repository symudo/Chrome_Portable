#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Chrome 便携版自动化打包构建脚本
实现：流式重试下载、哈希/大小校验、解包、剔除冗余组件、插件集成、安全编码合并配置(含UTF-16LE BOM)、
PE 注入前后严格校验、格式压缩与全方位运行时闭环自测。
"""

import argparse
import ctypes
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from common import (
    archive_filename,
    calc_sha256,
    configure_utf8_stdio,
    urlopen_with_retry,
)

configure_utf8_stdio()

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


def find_7z():
    configured = os.environ.get("SEVEN_ZIP")
    if configured and Path(configured).exists():
        return configured
    for name in ("7z", "7zz", "7za", "NanaZipC"):
        found = shutil.which(name)
        if found:
            return found
    default_paths = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for p in default_paths:
        if Path(p).exists():
            return p
    raise RuntimeError("未找到 7-Zip 工具，请安装 7-Zip 或设置 SEVEN_ZIP 环境变量。")


def find_shallowest(root, pattern, want_dir=False, want_file=False):
    """按路径层级最浅、字典序最小确定性查找目标，避免依赖文件系统 rglob 顺序。"""
    matches = []
    for p in Path(root).rglob(pattern):
        if want_dir and not p.is_dir():
            continue
        if want_file and not p.is_file():
            continue
        matches.append(p)
    if not matches:
        return None
    return min(matches, key=lambda p: (len(p.parts), str(p).lower()))


def download_file(url, dest_path, expected_sha256=None, expected_size=None, retries=3):
    """带全流级重试（涵盖读取中途断流/超时/校验失败）与原子落盘的文件下载。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = (expected_sha256 or "").strip().lower() or None
    expected_size = int(expected_size) if expected_size else None

    if dest_path.exists():
        size_ok = (expected_size is None) or (dest_path.stat().st_size == expected_size)
        sha_ok = (expected_sha256 is None) or (calc_sha256(dest_path) == expected_sha256)
        if (expected_sha256 or expected_size) and size_ok and sha_ok:
            print(f"[CACHE] 文件已存在且校验通过: {dest_path.name}")
            return dest_path

    headers = {"User-Agent": "ChromePortableBuilder/1.0"}
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    last_err = None

    for attempt in range(1, retries + 1):
        print(f"[DOWNLOAD] 开始下载 (第 {attempt}/{retries} 次): {url} -> {dest_path.name}")
        req = urllib.request.Request(url, headers=headers)
        sha = hashlib.sha256()
        downloaded = 0
        try:
            with urlopen_with_retry(req, timeout=300, retries=1) as resp, open(part_path, "wb") as out:
                total_size = int(resp.headers.get("content-length") or 0)
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
            if expected_size is not None and downloaded != expected_size:
                raise RuntimeError(f"下载文件大小校验失败！期望: {expected_size}, 实际: {downloaded}")
            if expected_sha256 and actual_sha != expected_sha256:
                raise RuntimeError(f"下载文件 SHA256 校验失败！期望: {expected_sha256}, 实际: {actual_sha}")

            part_path.replace(dest_path)
            print(f"[DOWNLOAD] 下载完成: {dest_path.name} ({downloaded} 字节, SHA256: {actual_sha[:16]}...)")
            return dest_path
        except urllib.error.HTTPError as e:
            part_path.unlink(missing_ok=True)
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last_err = e
        except Exception as e:
            part_path.unlink(missing_ok=True)
            last_err = e

        if attempt < retries:
            wait = 2 * attempt
            print(f"[RETRY] 下载中断或校验失败 ({last_err})，{wait}s 后重试...", file=sys.stderr)
            time.sleep(wait)

    raise last_err


def decode_ini_bytes(raw_bytes):
    """按 BOM 与空字节特征安全解码 INI，避免无 BOM 的 UTF-8 文本被静默误解为 UTF-16 乱码。"""
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw_bytes.decode("utf-16")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return raw_bytes.decode("utf-8-sig")
    # 仅在明显存在 UTF-16LE 空字节特征时才按 utf-16-le 解码
    if b"\x00" in raw_bytes and len(raw_bytes) % 2 == 0:
        try:
            return raw_bytes.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gbk", "latin1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("latin1")


def parse_ini_overrides(file_path):
    """解析 override.ini 文件中的各个 section 与 key-value"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件覆盖层不存在: {path}")

    overrides = {}
    text = path.read_text(encoding="utf-8-sig")
    current_sec = "general"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_sec = line[1:-1].strip().lower()
            overrides.setdefault(current_sec, {})
        elif "=" in line:
            k, v = line.split("=", 1)
            overrides.setdefault(current_sec, {})[k.strip()] = v.strip()

    if not overrides:
        raise RuntimeError(f"配置文件覆盖层为空: {path}")
    return overrides


def merge_chrome_ini(base_text, overrides_dict):
    """合并配置到基线 INI 文本中（键名大小写不敏感），保持注释与格式，修改或添加对应键。"""
    # 建立小写键名到 (原始覆盖键名, 值) 的映射，兼容 Windows INI 大小写不敏感规范
    norm_overrides = {
        sec.lower(): {k.lower(): (k, v) for k, v in kv.items()}
        for sec, kv in overrides_dict.items()
    }
    lines = base_text.splitlines()
    new_lines = []
    current_section = None
    applied_keys = {sec: set() for sec in norm_overrides}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_section in norm_overrides:
                for k_low, (orig_k, v) in norm_overrides[current_section].items():
                    if k_low not in applied_keys[current_section]:
                        new_lines.append(f"{orig_k}={v}")
                        applied_keys[current_section].add(k_low)
            current_section = stripped[1:-1].strip().lower()
            new_lines.append(line)
            continue

        if current_section in norm_overrides and "=" in stripped and not stripped.startswith(";"):
            k_raw, _ = stripped.split("=", 1)
            k_low = k_raw.strip().lower()
            if k_low in norm_overrides[current_section]:
                orig_k, v = norm_overrides[current_section][k_low]
                if k_low not in applied_keys[current_section]:
                    new_lines.append(f"{orig_k}={v}")
                    applied_keys[current_section].add(k_low)
                # 若基线中已存在重复的同键，后续重复行也直接去重跳过
                continue

        new_lines.append(line)

    if current_section in norm_overrides:
        for k_low, (orig_k, v) in norm_overrides[current_section].items():
            if k_low not in applied_keys[current_section]:
                new_lines.append(f"{orig_k}={v}")
                applied_keys[current_section].add(k_low)

    # 追加基线中不存在的新 section
    for sec, kv in norm_overrides.items():
        remaining = [(orig_k, v) for k_low, (orig_k, v) in kv.items() if k_low not in applied_keys[sec]]
        if remaining:
            new_lines.append("")
            new_lines.append(f"[{sec}]")
            for orig_k, v in remaining:
                new_lines.append(f"{orig_k}={v}")

    return "\r\n".join(new_lines) + "\r\n"


def read_win_ini_value(ini_path, section, key, default="__MISSING__"):
    """调用 Windows 原生 GetPrivateProfileStringW 实测读取 INI，与 Chrome++ 实际行为完全一致。"""
    buf = ctypes.create_unicode_buffer(4096)
    ctypes.windll.kernel32.GetPrivateProfileStringW(
        section, key, default, buf, len(buf), str(ini_path)
    )
    return buf.value


def get_pe_imports(file_path):
    """纯标准库解析 PE 文件导入表"""
    with open(file_path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack("<I", data[0x3C:0x40])[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
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
        sections.append((vaddr, vsize, rsize, raddr))

    def rva_to_offset(rva):
        for vaddr, vsize, rsize, raddr in sections:
            span = max(vsize, rsize)
            if rsize > 0 and vaddr <= rva < vaddr + span and (rva - vaddr) < rsize:
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


def assert_pe_injected(exe_path, expected_min_count=None):
    """严格断言 PE 导入表首位为 setdll 注入的 version.dll，避免被原生系统 VERSION.dll 误判通过。"""
    imports = get_pe_imports(exe_path)
    if not imports or imports[0].lower() != "version.dll":
        raise RuntimeError(
            f"PE 导入表校验失败：首位必须为注入的 version.dll，实际导入列表: {imports}"
        )
    if expected_min_count is not None and len(imports) < expected_min_count:
        raise RuntimeError(
            f"PE 导入表条目数量异常：期望至少 {expected_min_count} 项，实际: {imports}"
        )
    return imports


def terminate_processes_for_exe(exe_path):
    """精准清理可执行路径等于测试 chrome.exe 的残留子进程，避免锁住临时目录，绝不误杀用户正常 Chrome。"""
    env = os.environ.copy()
    env["TARGET_TEST_EXE"] = str(Path(exe_path).resolve())
    ps_script = (
        "$t = $env:TARGET_TEST_EXE; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
        "Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $t) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            env=env,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def extract_with_7z(seven_zip, archive_path, output_dir):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    cmd = [seven_zip, "x", str(archive_path), f"-o{output_dir}", "-y"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"解压失败: {archive_path}\n{res.stdout}\n{res.stderr}")


def build(
    chrome_version,
    chrome_url,
    chrome_sha256,
    chrome_plus_version,
    chrome_plus_url,
    chrome_plus_sha256=None,
    chrome_plus_size=None,
):
    seven_zip = find_7z()
    print(f"[TOOL] 7-Zip 工具路径: {seven_zip}")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 下载文件并校验完整性（带流级重试）
    installer_name = f"chrome_installer_{chrome_version}_x64.exe"
    installer_path = DOWNLOADS_DIR / installer_name
    download_file(chrome_url, installer_path, expected_sha256=chrome_sha256)

    plus_archive_name = f"setdll_{chrome_plus_version}.7z"
    plus_archive_path = DOWNLOADS_DIR / plus_archive_name
    download_file(
        chrome_plus_url,
        plus_archive_path,
        expected_sha256=chrome_plus_sha256,
        expected_size=chrome_plus_size,
    )

    with tempfile.TemporaryDirectory(prefix="chrome_build_", ignore_cleanup_errors=True) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        raw_extract = temp_dir / "raw"
        chrome_bin_dir = temp_dir / "chrome_bin"
        plus_extract = temp_dir / "plus"
        stage_dir = temp_dir / "stage"
        chrome_app_dir = stage_dir / "Chrome"

        # 2. 解压 Chrome 安装包（按路径深度最浅确定性定位）
        print("[EXTRACT] 正在解压 Chrome 离线安装程序...")
        extract_with_7z(seven_zip, installer_path, raw_extract)

        source_bin = find_shallowest(raw_extract, "Chrome-bin", want_dir=True)
        if source_bin is None:
            exe_match = find_shallowest(raw_extract, "chrome.exe", want_file=True)
            if exe_match is not None:
                source_bin = exe_match.parent

        if source_bin is None:
            chrome_7z = find_shallowest(raw_extract, "chrome.7z", want_file=True) or find_shallowest(
                raw_extract, "CHROME.7Z", want_file=True
            )
            if chrome_7z is not None:
                print(f"[EXTRACT] 正在解包内部核心文件: {chrome_7z.name} ...")
                extract_with_7z(seven_zip, chrome_7z, chrome_bin_dir)
                source_bin = find_shallowest(chrome_bin_dir, "Chrome-bin", want_dir=True)
                if source_bin is None:
                    exe_match = find_shallowest(chrome_bin_dir, "chrome.exe", want_file=True)
                    if exe_match is not None:
                        source_bin = exe_match.parent

        if source_bin is None:
            raise RuntimeError("解包后的目录中未找到 Chrome-bin 或 chrome.exe")

        print(f"[LAYOUT] 定位 Chrome 程序源目录: {source_bin}")
        chrome_app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_bin, chrome_app_dir, dirs_exist_ok=True)

        # 3. 剔除冗余更新及遥测组件
        print("[CLEAN] 剔除 Google 更新与安装辅助程序...")
        removed_count = 0
        for item in chrome_app_dir.rglob("*"):
            if item.is_file() and item.name.lower() in FORBIDDEN_FILES:
                item.unlink()
                print(f"  已移除: {item.name}")
                removed_count += 1
        print(f"[CLEAN] 共清除 {removed_count} 个冗余文件。")

        # 4. 解压 Chrome++ 并集成（最浅路径确定性定位）
        print("[EXTRACT] 正在解包 Chrome++ 插件...")
        extract_with_7z(seven_zip, plus_archive_path, plus_extract)

        version_dll = find_shallowest(plus_extract, "version-x64.dll", want_file=True)
        setdll_exe = find_shallowest(plus_extract, "setdll-x64.exe", want_file=True)
        base_ini = find_shallowest(plus_extract, "chrome++.ini", want_file=True)

        if not version_dll or not setdll_exe or not base_ini:
            raise RuntimeError("setdll.7z 中缺少必要的 version-x64.dll, setdll-x64.exe 或 chrome++.ini")

        target_version_dll = chrome_app_dir / "version.dll"
        shutil.copy2(version_dll, target_version_dll)
        print(f"[INTEGRATE] 已放置插件 DLL: {target_version_dll.name}")

        # 5. 配置合并 (单一事实来源：以 chrome++.override.ini 为准，安全解码并写入带 BOM 的 UTF-16LE)
        print("[CONFIG] 正在合并 Chrome++ 配置文件...")
        ini_text = decode_ini_bytes(base_ini.read_bytes())

        override_file = REPO_ROOT / "chrome++" / "chrome++.override.ini"
        overrides = parse_ini_overrides(override_file)

        merged_ini_text = merge_chrome_ini(ini_text, overrides)
        target_ini = chrome_app_dir / "chrome++.ini"
        target_ini.write_bytes(b"\xff\xfe" + merged_ini_text.encode("utf-16-le"))
        print(f"[CONFIG] 配置文件写入完成: {target_ini.name} (UTF-16LE with BOM)")

        # 6. PE 注入 (注入前后严格对比导入表)
        chrome_exe = chrome_app_dir / "chrome.exe"
        if not chrome_exe.exists():
            raise RuntimeError(f"目标程序不存在: {chrome_exe}")

        imports_before = get_pe_imports(chrome_exe)
        print(f"[PE INJECT] 注入前导入表 ({len(imports_before)} 项): {imports_before}")

        temp_setdll = temp_dir / "setdll-x64.exe"
        shutil.copy2(setdll_exe, temp_setdll)

        inject_cmd = [str(temp_setdll), "/d:version.dll", str(chrome_exe)]
        res = subprocess.run(inject_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(res.stdout)
            print(res.stderr, file=sys.stderr)
            raise RuntimeError(f"setdll PE 注入失败，退出码: {res.returncode}")

        imports_after = assert_pe_injected(chrome_exe, expected_min_count=len(imports_before) + 1)
        print(f"[VERIFY] 注入后导入表 ({len(imports_after)} 项): {imports_after}")

        # 7. 放置快捷方式辅助脚本 开始.bat（强制确保 CRLF，避免 cmd.exe + chcp 65001 在 LF 下错位）
        start_bat_src = REPO_ROOT / "开始.bat"
        if not start_bat_src.exists():
            raise FileNotFoundError(f"未找到启动脚本: {start_bat_src}")
        bat_text = start_bat_src.read_text(encoding="utf-8-sig")
        bat_crlf = bat_text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
        (stage_dir / "开始.bat").write_bytes(bat_crlf)
        print("[PACKAGE] 已添加 开始.bat 到压缩包根目录 (CRLF)")

        # 8. 压缩生成便携版 7z 包
        archive_name = archive_filename(chrome_version, chrome_plus_version)
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

        sha256_file = ASSETS_DIR / f"{archive_name}.sha256"
        sha256_file.write_text(f"{archive_sha256}  {archive_name}\n", encoding="utf-8")
        print(f"[CHECKSUM] 已生成校验文件: {sha256_file.name}")

        # 9. 全方位闭环自测验证（含单引号/&/!/空格路径、BOM/Windows API 读配置、PE 首位注入、开始.bat 与运行时便携目录生成）
        print("\n=== [全方位闭环自测验证 (Smoke Test)] ===")
        test_extract_dir = temp_dir / "Bob's R&D! Chrome Test"
        extract_with_7z(seven_zip, archive_path, test_extract_dir)
        test_chrome_exe = test_extract_dir / "Chrome" / "chrome.exe"
        test_version_dll = test_extract_dir / "Chrome" / "version.dll"
        test_ini = test_extract_dir / "Chrome" / "chrome++.ini"
        test_bat = test_extract_dir / "开始.bat"

        for required in (test_chrome_exe, test_version_dll, test_ini, test_bat):
            if not required.exists():
                raise RuntimeError(f"自检失败: 关键文件缺失 -> {required}")

        # 9.1 验证解压后 PE 导入表首位注入
        assert_pe_injected(test_chrome_exe, expected_min_count=len(imports_before) + 1)
        print("[TEST 1/4] PE 首位注入校验通过。")

        # 9.2 验证 BOM 头与 Windows 原生 API 读取所有 override 配置项
        if not test_ini.read_bytes().startswith(b"\xff\xfe"):
            raise RuntimeError("自检失败: chrome++.ini 缺少 UTF-16LE BOM 头 (FF FE)")
        for sec, kv in overrides.items():
            for k, expected_v in kv.items():
                actual_v = read_win_ini_value(test_ini, sec, k)
                if actual_v != expected_v:
                    raise RuntimeError(
                        f"自检失败: Windows API 读取 [{sec}] {k} 不匹配！期望={expected_v!r}, 实际={actual_v!r}"
                    )
        print("[TEST 2/4] UTF-16LE BOM 及 Windows 原生 INI API 实测校验通过。")

        # 9.3 实测含单引号/&/!/空格路径下的 开始.bat 快捷方式生成（以相对名在目标目录执行，与双击行为一致）
        bat_res = subprocess.run(
            ["cmd", "/c", "开始.bat"],
            cwd=str(test_extract_dir),
            input="\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        test_lnk = test_extract_dir / "Chrome.lnk"
        if bat_res.returncode != 0 or not test_lnk.exists() or bat_res.stderr.strip():
            raise RuntimeError(
                f"自检失败: 开始.bat 在含特殊字符路径下执行异常 (code={bat_res.returncode})\n"
                f"stdout={bat_res.stdout}\nstderr={bat_res.stderr}"
            )
        print(f"[TEST 3/4] 开始.bat 快捷方式生成验证通过（含特殊字符路径: {test_extract_dir.name}）。")

        # 9.4 实测 Headless 启动并验证 Chrome++ 便携数据/缓存目录生效
        test_data_dir = test_extract_dir / "Data"
        test_cache_dir = test_extract_dir / "Cache"
        for p in (test_data_dir, test_cache_dir):
            if p.exists():
                shutil.rmtree(p)

        print("[TEST 4/4] 启动解压后的 chrome.exe 验证运行时注入与便携数据隔离...")
        smoke_cmd = [
            str(test_chrome_exe),
            "--headless=new",
            "--dump-dom",
            "--disable-gpu",
            "about:blank",
        ]
        try:
            smoke_res = subprocess.run(smoke_cmd, capture_output=True, text=True, timeout=30)
            if smoke_res.returncode != 0:
                raise RuntimeError(
                    f"自检失败: Headless 启动返回非 0 退出码 ({smoke_res.returncode})\n{smoke_res.stderr}"
                )

            for _ in range(20):
                if test_data_dir.is_dir() and test_cache_dir.is_dir():
                    break
                time.sleep(0.5)

            if not test_data_dir.is_dir() or not test_cache_dir.is_dir():
                raise RuntimeError(
                    f"自检失败: 启动后未在便携路径生成 Data/Cache 目录 ({test_extract_dir})，Chrome++ 可能未生效！"
                )
            print("[TEST 4/4] Headless 启动正常 (code=0)，已确认便携目录生成: Data/ 与 Cache/")
        finally:
            time.sleep(1)
            terminate_processes_for_exe(test_chrome_exe)

        print("=== 全方位闭环自测全部通过，产物准备就绪！===\n")
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
    parser.add_argument("--plus-sha256", help="Chrome++ setdll.7z expected SHA256")
    parser.add_argument("--plus-size", type=int, help="Chrome++ setdll.7z expected size")
    args = parser.parse_args()

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
        plus_sha256 = plus.get("sha256")
        plus_size = plus.get("size")
    else:
        chrome_version = args.chrome_version
        chrome_url = args.chrome_url
        chrome_sha256 = args.chrome_sha256
        plus_version = args.plus_version
        plus_url = args.plus_url
        plus_sha256 = args.plus_sha256
        plus_size = args.plus_size

    res = build(
        chrome_version,
        chrome_url,
        chrome_sha256,
        plus_version,
        plus_url,
        chrome_plus_sha256=plus_sha256,
        chrome_plus_size=plus_size,
    )

    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"ARCHIVE_NAME={res['archive_name']}\n")
            f.write(f"ARCHIVE_PATH={res['archive_path']}\n")
            f.write(f"ARCHIVE_SIZE={res['archive_size']}\n")
            f.write(f"ARCHIVE_SHA256={res['archive_sha256']}\n")


if __name__ == "__main__":
    main()

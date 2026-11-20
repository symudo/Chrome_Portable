# Chrome Portable (Chrome++ 便携版)

[![Build & Update Check](https://github.com/symudo/Chrome_Portable/actions/workflows/build.yml/badge.svg)](https://github.com/symudo/Chrome_Portable/actions/workflows/build.yml)
[![Releases](https://img.shields.io/github/v/release/symudo/Chrome_Portable?logo=github&color=blue)](https://github.com/symudo/Chrome_Portable/releases)
[![License](https://img.shields.io/badge/License-GPL--3.0-green.svg)](LICENSE)

自动追踪并打包发布的 **Google Chrome Windows 64位便携增强版**。集成 [Chrome++](https://github.com/Bush2021/chrome_plus) 便携化与功能增强插件，每日定时检测双端更新，实现免安装、纯净隔离、随身携带。

---

## 特性亮点

- **双端更新自动追踪**：
  - 每天北京时间 **09:00** 自动检查 Google Chrome 官方最新稳定版与 Chrome++ 最新插件。
  - 只要 **Chrome 浏览器** 或 **Chrome++ 插件** 任一更新，即刻触发云端全自动构建并原子发布全新 Release。
- **纯净便携化隔离**：
  - 用户个人配置（Profile）与缓存默认保存在解压目录同级的 `Data` 与 `Cache` 文件夹。
  - 便携目录可任意复制或放置在移动硬盘、U盘中使用，更换设备数据不丢失。
- **已默认阻止企业版策略**：
  - 插件配置中已默认开启 `ignore_policies=1`，彻底阻断从注册表强制读取企业管控策略。
- **全方位闭环自测**：
  - 构建阶段自动执行下载哈希/大小校验、PE 首位导入表注入对比、Windows 原生 API 验读带 BOM 的 `chrome++.ini`、含特殊字符路径下的 `开始.bat` 实测，以及 Headless 运行时便携目录生成验证。
- **精简纯净 & 一键快捷方式**：
  - 自动剔除 `GoogleUpdate.exe`、`GoogleUpdater.exe`、`updater.exe` 等常驻后台及遥测组件。
  - 压缩包根目录下内置 `开始.bat`，双击即可自动在当前目录创建指向 `Chrome\chrome.exe` 的快捷方式（兼容空格、中文及单引号路径）。

---

## 下载与使用

1. 从 [Releases 页面](https://github.com/symudo/Chrome_Portable/releases) 下载最新版的 `ChromePortable_<chrome版本>_<插件版本>_x64.7z`。
2. 解压到您喜欢的任意目录。
3. 双击根目录下的 `开始.bat`，即可生成 `Chrome.lnk` 快捷方式，启动即可使用。
4. **后续更新**：下载新版压缩包后，只需覆盖替换 `Chrome` 文件夹，同级的 `Data`（书签/密码/扩展）和 `Cache`（缓存）不受任何影响。

---

## 目录结构

解压后的标准目录层级如下：

```text
Chrome_Portable/
├── Chrome/                 <-- 浏览器主程序与插件目录（更新时只替换此文件夹）
│   ├── chrome.exe          <-- 已注入 version.dll 的主执行文件
│   ├── version.dll         <-- Chrome++ 增强核心 DLL
│   ├── chrome++.ini        <-- 便携版及插件配置文件（UTF-16LE with BOM，已开启 ignore_policies=1）
│   └── 158.0.8127.37/     <-- Chrome 核心资源与动态库
├── Data/                   <-- 自动生成的便携用户数据目录（书签、扩展、配置）
├── Cache/                  <-- 自动生成的便携网页缓存目录
├── 开始.bat                <-- 一键生成当前目录 Chrome.lnk 快捷方式
└── Chrome.lnk              <-- 快捷方式（运行 开始.bat 后生成）
```

---

## 本地构建与测试

本项目完全支持在本地 Windows 环境下一键构建与全闭环自测：

```powershell
# 1. 检查最新版本状态
python scripts/check_update.py

# 2. 本地执行全流程打包与全方位闭环自测
python scripts/build_portable.py

# 3. 原子发布到 GitHub Releases（需本地或环境变量配置 GitHub 凭据）
python scripts/release_helper.py --chrome-version "158.0.8127.37" --plus-version "1.20.1" --archive-path "build/assets/ChromePortable_158.0.8127.37_1.20.1_x64.7z"
```

---

## 致谢与上游项目

- **Google Chrome 离线安装包追踪**：[Bush2021/chrome_installer](https://github.com/Bush2021/chrome_installer)
- **Chrome++ 便携增强插件**：[Bush2021/chrome_plus](https://github.com/Bush2021/chrome_plus)
- **便携版流程设计参考**：[Piracola/Chrome-Portable](https://github.com/Piracola/Chrome-Portable)
- **原 Chrome++ 早期作者**：[Shuax](https://github.com/shuax/)

---

## 许可证

本项目遵循 [GPL-3.0](LICENSE) 开源许可证。

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.py —— 打 release 包。

产出（都在 dist/ 下）：
  1. 电影票房采集器_v<ver>_win64/      可执行程序目录（exe + 说明 + 文档）
     电影票房采集器_v<ver>_win64.zip   上面这个目录的压缩包
  2. MovieData_源码_<date>.zip         源码备份（无数据库、无 raw、无 output）

用法：
  python build_release.py            # 只打包（exe 已存在则复用）
  python build_release.py --build    # 先调 PyInstaller 重新构建 exe
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import ssl
import subprocess
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
VER = "1.3.3"
DATE = dt.date.today().strftime("%Y%m%d")
APP_NAME = "MovieData"
EXE = os.path.join(DIST, APP_NAME + ".exe")

SRC_FILES = [
    "gui.py", "history_quick_gui.py", "pipeline.py", "contract_test.py", "make_tables.py", "build_release.py",
    "启动采集器.bat", "启动单片全国历史.bat", "launch_source.bat", "启动说明.txt",
    "README.md", "CONTRACT.md", "使用教程.md", "QA_REPORT.md", "TABLES.md", "单片历史影院使用说明.md",
    "tests/test_movie_history.py",
    "sample.csv", "LICENSE",
]
DOC_IMAGES = ["ui_all.png", "ui_running.png"]
PKG_FILES = ["使用教程.md", "README.md", "单片历史影院使用说明.md", "启动说明.txt", "ui_all.png"]

START_TXT = """电影票房采集器 v{ver}
============================================================

一、怎么启动
   先完整解压，再双击「{name}.exe」。本包自带运行环境。
   若只用单片全国历史，双击「打开单片全国历史.bat」进入快捷窗口。

二、数据存在哪
   采集时在本文件所在目录创建：
       boxoffice.db     数据库（所有采集结果）
       raw/             原始接口响应，用于回溯
       output/          导出的 CSV / JSON
   已有进度时，关闭旧程序后，把 boxoffice.db 放到 {name}.exe 同一目录。
   数据库随程序目录一起复制才能带走已有进度。本程序包不含历史数据库。
   如目录无法写入，把整个解压目录移到有写入权限的位置再采集。

三、需要什么
   能访问 https://zgdypf.zgdypw.cn 的网络。
   除此之外不需要任何东西——不用装 Python，不用装库。

四、第一次用
   1. 打开后先点「估算」，看清这一轮要发多少次请求；
   2. 想小试，就只勾「02 全国大盘」，一天只要 2 次请求；
   3. 完整用法见同目录的「使用教程.md」（每个选项、每个数字都有解释）。

五、目录里都有什么
   使用教程.md      完整使用手册（推荐先看）
   README.md        工程说明与设计取舍
   ui_all.png       界面截图

数据来源：中国电影票房（zgdypf.zgdypw.cn，专资办口径，含服务费）
仅供研究使用，请遵守站点条款，控制请求频率。
"""


def log(msg):
    print(msg, flush=True)


def build_exe() -> None:
    log("▶ 调用 PyInstaller 构建 exe …")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--windowed", "--name", APP_NAME,
           "--distpath", DIST, "--workpath", os.path.join(ROOT, "build"),
           "--specpath", os.path.join(ROOT, "build"), "gui.py"]
    # Some build runtimes reference an extra trust-policy file beside ssl.py.
    # Keep it beside the frozen ssl module to preserve that policy at runtime.
    trust_policy = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
    if trust_policy.is_file():
        cmd[-1:-1] = ["--add-data", f"{trust_policy}{os.pathsep}."]
    subprocess.run(cmd, cwd=ROOT, check=True)


def make_program_dir() -> str:
    pkg = os.path.join(DIST, f"{APP_NAME}_v{VER}_win64")
    shutil.rmtree(pkg, ignore_errors=True)
    os.makedirs(pkg, exist_ok=True)
    shutil.copy2(EXE, os.path.join(pkg, APP_NAME + ".exe"))
    with open(os.path.join(pkg, "打开单片全国历史.bat"), "w", encoding="ascii", newline="\r\n") as fh:
        fh.write('@echo off\nstart "" "%~dp0' + APP_NAME + '.exe" --quick-history\n')
    for f in PKG_FILES:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(pkg, f))
    with open(os.path.join(pkg, "启动说明.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(START_TXT.format(ver=VER, name=APP_NAME))
    return pkg


def zip_dir(src_dir: str, zip_path: str) -> None:
    base = os.path.dirname(src_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, base))
    log(f"  ✓ {os.path.basename(zip_path)}  {os.path.getsize(zip_path)/1e6:.1f} MB")


def zip_sources() -> str:
    zip_path = os.path.join(DIST, f"MovieData_源码_{DATE}.zip")
    top = f"MovieData_源码_{DATE}"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in SRC_FILES + DOC_IMAGES:
            p = os.path.join(ROOT, f)
            if os.path.exists(p):
                z.write(p, f"{top}/{f}")
        pkg = os.path.join(ROOT, "zgdypf")
        for f in sorted(os.listdir(pkg)):
            if f.endswith(".py"):
                z.write(os.path.join(pkg, f), f"{top}/zgdypf/{f}")
    log(f"  ✓ {os.path.basename(zip_path)}  {os.path.getsize(zip_path)/1e6:.1f} MB")
    return zip_path


def zip_data() -> str:
    """数据备份：只打包数据库与样本名单。

    不含 raw/（163 MB 原始响应，可重新采集）与 output/（86 MB，可从库 8 秒重导）。
    """
    zip_path = os.path.join(DIST, f"MovieData_数据备份_{DATE}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in ("boxoffice.db", "sample.csv"):
            p = os.path.join(ROOT, f)
            if os.path.exists(p):
                z.write(p, f"MovieData_数据_{DATE}/{f}")
    log(f"  ✓ {os.path.basename(zip_path)}  {os.path.getsize(zip_path)/1e6:.1f} MB")
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="先重新构建 exe")
    ap.add_argument("--data", action="store_true", help="额外打一个数据库备份包")
    a = ap.parse_args()

    os.makedirs(DIST, exist_ok=True)
    if a.build or not os.path.exists(EXE):
        build_exe()
    else:
        log(f"▶ 复用已有 exe：{os.path.basename(EXE)}  {os.path.getsize(EXE)/1e6:.1f} MB")

    log("▶ 组装程序目录 …")
    pkg = make_program_dir()
    log("▶ 压缩 …")
    zip_dir(pkg, os.path.join(DIST, f"{APP_NAME}_v{VER}_win64.zip"))
    zip_sources()
    if a.data:
        log("▶ 压缩数据库 …")
        zip_data()

    log("\n完成，dist/ 内容：")
    for f in sorted(os.listdir(DIST)):
        p = os.path.join(DIST, f)
        if os.path.isfile(p):
            log(f"  {f:<46} {os.path.getsize(p)/1e6:8.2f} MB")
        else:
            log(f"  {f}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

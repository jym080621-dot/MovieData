#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zgdypf —— 中国电影票房（zgdypf.zgdypw.cn）站内接口的采集与解析层。

设计约束（来自 Phase 0 实测裁决，见 CONTRACT.md）：
  1. 一切以原始响应为准；所有响应原样落盘 raw/。
  2. 400/404 不重试（HardError）；5xx/超时/连接错误按 1s/4s/16s 退避重试。
  3. 204 = 无数据（正常）；但 getCinemaSumData 的空日是 200 + "--"，需单独处理。
  4. Desc 字段单位漂移（元 / 万），只做展示与兜底，数值运算优先走整数字段。
  5. 缺字段写 NULL 记日志，不抛异常中断整批。
"""

__version__ = "1.5.2"

import os as _os
import sys as _sys


def app_root() -> str:
    """程序根目录。

    源码运行  -> 项目目录（本包的上一级）
    PyInstaller 冻结后 -> exe 所在目录

    这一条是打包的命门：onefile 模式下 __file__ 指向临时解包目录
    (C:\\Users\\...\\AppData\\Local\\Temp\\_MEIxxxx)，若仍按 __file__ 取路径，
    数据库、raw、output 都会写进临时目录，程序一退出就全没了。
    """
    if getattr(_sys, "frozen", False):
        return _os.path.dirname(_os.path.abspath(_sys.executable))
    return _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


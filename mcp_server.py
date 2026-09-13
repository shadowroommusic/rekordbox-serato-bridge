#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rekordbox Serato Bridge — MCP 服务入口（stdio transport）。

由 Codex 以 `${PLUGIN_ROOT}` 为工作目录启动，把 `src/` 加进 sys.path 后运行
插件自身的 MCP 服务，无需事先 pip 安装。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from shadow_rb_serato.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()

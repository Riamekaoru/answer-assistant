# -*- coding: utf-8 -*-
"""工作进程包。

进程拓扑（三进程隔离）：

    主进程 main.py  ──►  UI（Tkinter）+ 编排 + 热键
                             │  frame_q（丢旧保新）
                             ▼
    截屏进程 capture_worker  ──►  mss 抓屏 + 帧差检测
                             │  frame_q
                             ▼
    搜索进程 search_worker   ──►  OCR + 模糊匹配
                             │  result_q / stat_q
                             ▼
                          主进程 UI 刷新

隔离收益：OCR 模型加载与推理占内存且可能崩溃，放在独立进程里，
即便 PaddleOCR 原生库异常退出也不会带走 UI；截屏进程以固定节奏工作，
不受 OCR 耗时抖动影响，保证框选区域变化能被及时捕捉。
"""

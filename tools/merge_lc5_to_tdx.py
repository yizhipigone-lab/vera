# -*- coding: utf-8 -*-
"""把下载的 .lc5 五分钟线合并进通达信 vipdoc 目录 (2026-08-02)。

背景: 网盘下载的 lc5 历史从 2004 年起、止于 2026-06-26; TDX 本地
vipdoc/{sh,sz,bj}/fzline 里的 lc5 从 2024-06-27 起、止于最新交易日。
直接覆盖会丢掉 TDX 已有的最近一个月, 所以做"增量合并":
    结果 = 下载文件全部 bar + TDX 文件中晚于下载末根的 bar
重叠段 (2024-06-27~2026-06-26) 两边数据已验证一致, 取下载版即可。
TDX 已有但下载没有的文件不动; 下载有而 TDX 没有的 (如部分 bj) 直接拷贝。

写法: 临时文件 + os.replace 原子替换, 单文件失败只跳过不中断。

用法:
    python tools/merge_lc5_to_tdx.py            # 全量合并
    python tools/merge_lc5_to_tdx.py --limit 5  # 只合前 5 个 (测试)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

LC5_DTYPE = np.dtype([
    ('date', '<u2'), ('time', '<u2'),
    ('open', '<f4'), ('high', '<f4'), ('low', '<f4'), ('close', '<f4'),
    ('amount', '<f4'), ('vol', '<u4'), ('res', '<u4'),
])

SRC = Path(r"E:\BaiduNetdiskDownload\通达信5分钟-7月22\全部")
DST = Path(r"E:\NEW_TDX\vipdoc")


def merge_file(src: Path, dst: Path) -> str:
    """合并单个 lc5。返回状态串。"""
    a_src = np.fromfile(src, dtype=LC5_DTYPE)
    if len(a_src) == 0:
        return "空文件跳过"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix('.lc5.tmp')
        a_src.tofile(tmp)
        os.replace(tmp, dst)
        return "新增"
    a_dst = np.fromfile(dst, dtype=LC5_DTYPE)
    if len(a_dst) == 0:
        tmp = dst.with_suffix('.lc5.tmp')
        a_src.tofile(tmp)
        os.replace(tmp, dst)
        return "替换空文件"
    # 下载末根的 (date,time) 键
    last_key = (int(a_src[-1]['date']), int(a_src[-1]['time']))
    dst_keys = a_dst['date'].astype(np.int64) * 10000 + a_dst['time'].astype(np.int64)
    src_last = last_key[0] * 10000 + last_key[1]
    tail = a_dst[dst_keys > src_last]
    if len(tail) == 0 and a_dst[0]['date'] <= a_src[0]['date']:
        return "TDX已覆盖, 无需合并"
    combined = np.concatenate([a_src, tail])
    tmp = dst.with_suffix('.lc5.tmp')
    combined.tofile(tmp)
    os.replace(tmp, dst)
    return f"合并(历史{len(a_src)}+新段{len(tail)})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(SRC.glob("*/*.lc5"))
    if args.limit:
        files = files[:args.limit]
    total = len(files)
    print(f"[vipdoc合并] {total} 个文件 -> {DST}", flush=True)
    stats: dict = {}
    t0 = time.time()
    for i, src in enumerate(files, 1):
        market = src.parent.name          # sh / sz / bj
        dst = DST / market / "fzline" / src.name
        try:
            status = merge_file(src, dst)
        except Exception as e:
            status = f"失败:{e}"
        stats[status.split('(')[0].split(':')[0]] = \
            stats.get(status.split('(')[0].split(':')[0], 0) + 1
        if i % 200 == 0 or i == total:
            print(f"[vipdoc合并] {i}/{total} {stats} | {time.time()-t0:.0f}s",
                  flush=True)
    print(f"[vipdoc合并] 完成 {stats} | 总耗时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

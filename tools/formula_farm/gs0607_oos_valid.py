# -*- coding: utf-8 -*-
"""GS0607 样本外验证 — 用训练段选定的参数在验证段回测。

训练段最优参数 (2024-09~2025-11 选出):
  - 冠军: c-0.2_a0.08_d0.005_Loff_t40_cd0 (年化最高 18.5%)
  - 卡玛王: act0.08/d0.005/t20 (卡玛 6.40, 与全段冠军一致)
验证段 (2025-12~2026-09, 定参时完全未看) 独立检验。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

# 构造验证用 combos: 训练段最优 + 邻近参数 (观察邻域稳健性)
combos = [
    {"cost": -0.20, "act": 0.08, "dd": 0.005, "ladder": "off", "levels": [],
     "time_days": 20, "cond_days": 0, "cond_profit": 0.0},   # 卡玛王 t20
    {"cost": -0.20, "act": 0.08, "dd": 0.005, "ladder": "off", "levels": [],
     "time_days": 40, "cond_days": 0, "cond_profit": 0.0},   # 训练冠军 t40
    {"cost": -0.20, "act": 0.08, "dd": 0.005, "ladder": "off", "levels": [],
     "time_days": 12, "cond_days": 0, "cond_profit": 0.0},   # 全段冠军(对照)
    {"cost": -0.20, "act": 0.08, "dd": 0.010, "ladder": "off", "levels": [],
     "time_days": 20, "cond_days": 0, "cond_profit": 0.0},   # dd 邻域
    {"cost": -0.12, "act": 0.08, "dd": 0.005, "ladder": "off", "levels": [],
     "time_days": 20, "cond_days": 0, "cond_profit": 0.0},   # cost 邻域
]
cf = os.path.join(ROOT, 'output', 'gs_filter', 'gs0607_valid_combos.json')
json.dump(combos, open(cf, 'w', encoding='utf-8'))
print('验证 combos 已写:', cf)

env = dict(os.environ, PYTHONUTF8='1', SWEEP_TAG='gs0607_oos_valid')
r = subprocess.run([sys.executable, '-X', 'utf8',
                    os.path.join(ROOT, 'tools', 'gs_5m_sweep.py'), 'run', 'GS0607',
                    '--start', '20251201', '--end', '20260906',
                    '--stage', 'refine', '--combos-file', cf],
                   cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                   errors='replace', env=env, timeout=3600)
print(r.stdout[-2000:])
print('exit:', r.returncode)

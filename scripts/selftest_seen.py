import sys, os
sys.path.insert(0, '/root/zf_lucky')
from datetime import datetime, timedelta
from app.zf.scanner import ScannedFlow, should_stop_by_seen

def mk(h, dt=None):
    return ScannedFlow(hash=h, flow_id=1, title='t',
                       created_at=dt or datetime(2026,11,18,10,0,0),
                       author_uid=1, author_hash='u', author_nick='n')

FAIL=0
def check(label, ok, detail=''):
    global FAIL
    print(('  ✅ ' if ok else '  ❌ ')+label+(f'  ({detail})' if detail else ''))
    if not ok: FAIL+=1

print('\n[连续已入库判断]')
items=[mk(f'h{i}') for i in range(12)]
known={f'h{i}' for i in range(12)}

check('全部已知 → 末尾5条命中 → 停', should_stop_by_seen(items, known, 5) is True)
check('全部未知 → 不停', should_stop_by_seen(items, set(), 5) is False)

# 只有末尾 4 条已知
k4={f'h{i}' for i in range(8,12)}
check('末尾仅4条已知 → 不停（需5条）', should_stop_by_seen(items, k4, 5) is False)

# 末尾 5 条已知
k5={f'h{i}' for i in range(7,12)}
check('末尾5条已知 → 停', should_stop_by_seen(items, k5, 5) is True)

# 中间有缺口（末尾第3条未知）
k_gap={f'h{i}' for i in range(12)} - {items[-3].hash}
check('末尾有缺口 → 不停', should_stop_by_seen(items, k_gap, 5) is False)

check('条数不足 confirm → 不停', should_stop_by_seen(items[:3], known, 5) is False)
check('空列表 → 不停', should_stop_by_seen([], known, 5) is False)
check('confirm=0 → 不停（防误判）', should_stop_by_seen(items, known, 0) is False)

print('\n结果:', 'PASS' if FAIL==0 else f'FAIL({FAIL})')
sys.exit(0 if FAIL==0 else 1)

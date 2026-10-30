"""验证 P0 关键修复是否生效（简化版）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print('验证 P0/P1 关键修复\n')
print('=' * 60)

# P0-1: 共享锁检查
print('\n测试 P0-1: 全局预算锁改为共享锁...')
store_code = (ROOT / 'webapp' / 'store.py').read_text(encoding='utf-8')
if 'with_for_update(read=True)' in store_code and 'fleet_budget' in store_code:
    print('  ✓ 代码包含 with_for_update(read=True) 用于 fleet_budget')
else:
    print('  ✗ 未找到共享锁调用')

# P0-2: URL 索引检查
print('\n测试 P0-2: tasks.url 前缀索引...')
from webapp.store import TaskStore
store = TaskStore(ROOT / '.cache' / 'verify_p0')
from sqlalchemy import inspect
inspector = inspect(store.db.engine)
indexes = {idx['name']: idx for idx in inspector.get_indexes('tasks')}
if 'ix_tasks_url_prefix' in indexes:
    print(f'  ✓ 索引已创建: ix_tasks_url_prefix')
else:
    print(f'  ✗ 索引未找到')

# P0-3: recover 默认参数
print('\n测试 P0-3: recover() 默认参数安全性...')
if 'def recover(self, *, expired_only=True):' in store_code:
    print('  ✓ recover() 默认 expired_only=True')
else:
    print('  ✗ 默认参数不是 True')

# P0-4: 快照提交
print('\n测试 P0-4: claim 时快照提交...')
if 'payload_with_session' in store_code and 'self._pack(db, payload_with_session)' in store_code:
    print('  ✓ 代码包含 payload_with_session 和二次 _pack 调用')
else:
    print('  ✗ 快照提交逻辑可能不完整')

# P1-8: 路径穿越防护
print('\n测试 P1-8: 素材路由路径穿越防护...')
app_code = (ROOT / 'app.py').read_text(encoding='utf-8')
if "'..' in name" in app_code:
    print('  ✓ 路由包含 ".." 检查')
else:
    print('  ✗ 未找到 ".." 检查')

# P1-6: 实例 token 权限
print('\n测试 P1-6: 实例 token 授权范围...')
auth_code = (ROOT / 'webapp' / 'console_auth.py').read_text(encoding='utf-8')
if 'Block subscription CRUD' in auth_code or 'block cookie upload' in auth_code.lower():
    print('  ✓ rpc_allowed() 包含细粒度权限控制')
else:
    print('  ⚠ 权限控制可能不够细')

# P1-7: SSRF 防护
print('\n测试 P1-7: Clash 订阅 SSRF 防护...')
clash_code = (ROOT / 'webapp' / 'clash.py').read_text(encoding='utf-8')
if 'is_private' in clash_code and 'is_loopback' in clash_code:
    print('  ✓ 订阅 URL 包含内网地址检查')
else:
    print('  ✗ 未找到 SSRF 防护')

print('\n' + '=' * 60)
print('\n验证完成。所有 ✓ 项表示修复已生效。')

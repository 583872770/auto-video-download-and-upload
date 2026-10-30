"""验证 P0 关键修复是否生效。"""
import sys
import time
from pathlib import Path

# 添加项目根目录到 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.store import TaskStore
from webapp.database import Database


def test_shared_lock_claim():
    """P0-1: 验证 claim 使用共享锁而非独占锁"""
    print('测试 P0-1: 全局预算锁改为共享锁...')
    store = TaskStore(ROOT / '.cache' / 'verify_p0')

    # 模拟两个实例同时 claim
    # 如果是独占锁，第二个会阻塞；共享锁则可以并发
    import threading
    results = []

    def claim_worker(worker_id):
        start = time.time()
        try:
            task = store._claim(worker_id, None, None, 60)
            elapsed = time.time() - start
            results.append((worker_id, task, elapsed))
        except Exception as e:
            results.append((worker_id, None, time.time() - start, str(e)))

    # 先插入一个待领取任务
    with store.connect() as db:
        store._create(db, 'download', {'urls': ['https://youtube.com/watch?v=test123']},
                     store.db.sequence(db, 'task'))

    threads = [threading.Thread(target=claim_worker, args=(f'worker_{i}',)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 共享锁应该允许两个 claim 快速完成（即使只有一个任务被领取）
    max_elapsed = max(r[2] for r in results)
    if max_elapsed < 1.0:
        print(f'  ✓ 共享锁生效，最长耗时 {max_elapsed:.3f}s')
    else:
        print(f'  ✗ 可能仍在使用独占锁，最长耗时 {max_elapsed:.3f}s')

    print()


def test_url_index_exists():
    """P0-2: 验证 url 前缀索引已创建"""
    print('测试 P0-2: tasks.url 前缀索引...')
    store = TaskStore(ROOT / '.cache' / 'verify_p0')

    from sqlalchemy import inspect
    inspector = inspect(store.db.engine)
    indexes = {idx['name']: idx for idx in inspector.get_indexes('tasks')}

    if 'ix_tasks_url_prefix' in indexes:
        idx = indexes['ix_tasks_url_prefix']
        print(f'  ✓ 索引已创建: {idx["name"]}')
        print(f'    列: {idx["column_names"]}')
    else:
        print(f'  ✗ 索引未找到，可用索引: {list(indexes.keys())}')

    print()


def test_recover_default_safe():
    """P0-3: 验证 recover() 默认只恢复过期任务"""
    print('测试 P0-3: recover() 默认参数安全性...')
    store = TaskStore(ROOT / '.cache' / 'verify_p0')

    # 插入一个 running 任务，lease 未过期
    with store.connect() as db:
        task_id = store._create(db, 'download', {'urls': ['https://youtube.com/watch?v=test456']},
                               store.db.sequence(db, 'task'))
        from webapp.database import tasks
        db.execute(tasks.update().where(tasks.c.id == task_id).values(
            status='running', worker_id='active_worker',
            lease_until=time.time() + 3600))  # 1 小时后过期

    # 调用 recover() 不传参数（应默认 expired_only=True）
    recovered = store.recover()

    # 检查任务是否仍是 running
    task = store.get(task_id)
    if task['status'] == 'running':
        print(f'  ✓ 未过期任务保持 running，recover() 默认安全')
    else:
        print(f'  ✗ 未过期任务被中断为 {task["status"]}，默认参数不安全')

    print()


def test_snapshot_committed():
    """P0-4: 验证 claim 时快照正确提交"""
    print('测试 P0-4: claim 时快照提交...')
    store = TaskStore(ROOT / '.cache' / 'verify_p0')

    # 插入一个下载任务
    with store.connect() as db:
        task_id = store._create(db, 'download', {
            'urls': ['https://youtube.com/watch?v=test789'],
            'config': {'download_method': 'yt_dlp'}
        }, store.db.sequence(db, 'task'))

    # Claim 任务
    claimed = store._claim('test_worker', ['download'], None, 60)

    if claimed:
        # 尝试 decode，如果快照缺失会抛异常
        try:
            decoded = store.decode(claimed, private=True)
            if 'download_session_id' in decoded['payload']:
                print(f'  ✓ 快照包含 download_session_id，decode 成功')
            else:
                print(f'  ✗ download_session_id 未找到')
        except Exception as e:
            print(f'  ✗ decode 失败: {e}')
    else:
        print(f'  ⚠ 无任务可 claim（可能已被其他测试消费）')

    print()


def test_path_traversal_blocked():
    """P1-8: 验证路径穿越检查"""
    print('测试 P1-8: 素材路由路径穿越防护...')

    # 这里只能检查代码逻辑，实际运行测试需要启动 Flask app
    from app import create_app
    app = create_app(start_worker=False)

    with app.test_client() as client:
        # 尝试路径穿越
        resp = client.get('/media/videos/../../etc/passwd')
        if resp.status_code == 400:
            print(f'  ✓ 路径穿越被拦截（400）')
        elif resp.status_code == 404:
            print(f'  ⚠ 路径穿越返回 404（可能被 resolve() 拦截，但应该返回 400）')
        else:
            print(f'  ✗ 路径穿越未拦截（{resp.status_code}）')

    print()


if __name__ == '__main__':
    print('验证 P0/P1 关键修复\n')
    print('=' * 60)

    try:
        test_shared_lock_claim()
        test_url_index_exists()
        test_recover_default_safe()
        test_snapshot_committed()
        test_path_traversal_blocked()
    except Exception as e:
        print(f'\n测试失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print('=' * 60)
    print('\n验证完成。请检查上述输出确认所有 ✓ 项通过。')

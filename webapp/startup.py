"""Small startup status snapshot; independent of database availability."""
import threading
import time
import uuid


class DownloadStartup:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.value = {'state': 'idle', 'stage': '', 'steps': []}

    def status(self):
        with self.lock:
            value = {**self.value, 'steps': [dict(step) for step in self.value['steps']]}
            stamp = value.get('finished_at', time.time())
            value['elapsed'] = round(max(0, stamp - value.get('started_at', stamp)), 1)
            for step in value['steps']:
                step['elapsed'] = round(max(0, step.get('finished_at', stamp) - step['started_at']), 1)
            if value['steps']:
                value['slowest'] = max(value['steps'], key=lambda step: step['elapsed'])['stage']
            return value

    def stage(self, name):
        with self.lock:
            if self.cancelled.is_set():
                raise InterruptedError('启动已取消，清单仍在待处理队列。')
            stamp = time.time()
            if self.value['steps']:
                self.value['steps'][-1]['finished_at'] = stamp
            self.value['stage'] = name
            self.value['steps'].append({'stage': name, 'started_at': stamp})

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.value['state'] == 'running':
                self.value.update(cancel_requested=True, message='已请求取消，等待当前检查返回；不会放行清单。')

    def start(self, operation):
        with self.lock:
            if self.value['state'] == 'running':
                return self.status()
            self.cancelled = threading.Event()
            self.value = dict(id=uuid.uuid4().hex, state='running', stage='checking_database',
                              started_at=time.time(), steps=[])

            def run():
                try:
                    operation(self.stage, self.cancelled)
                    result = dict(state='succeeded', message='启动检查通过，已放行待处理清单。')
                except InterruptedError as error:
                    result = dict(state='cancelled', message=str(error))
                except Exception as error:
                    from sqlalchemy.exc import SQLAlchemyError
                    from webapp.downloads import clean_download_error
                    result = dict(state='failed', message='无法连接数据库，请检查 MySQL 网络和服务。' if isinstance(error, SQLAlchemyError)
                                  else clean_download_error(error))
                with self.lock:
                    self.value.update(result, finished_at=time.time())

            threading.Thread(target=run, name='download-startup', daemon=True).start()
            return self.status()

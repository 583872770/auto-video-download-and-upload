"""Read-only ledger exports and editable session billing labels/rates."""
import csv
from io import StringIO
from decimal import Decimal

from flask import Response, abort, jsonify, request, stream_with_context

from webapp.statistics import UNITS, describe


def csv_cell(value):
    text = str('' if value is None else value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def register_statistics(app, store):
    statistics = store.statistics

    def session(identity):
        value = statistics.session(identity)
        if value is None:
            abort(404, description='下载会话不存在。')
        return value

    def body():
        request.max_content_length = 16384
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('请提交统计设置。')
        return value

    @app.get('/api/statistics/lists')
    def list_overview():
        return jsonify(store.lists.page(request.args.get('cursor'), request.args.get('q', '')[:200]))

    @app.put('/api/statistics/lists/<identity>')
    def list_update(identity):
        store.lists.update(identity, body())
        return jsonify(message='客户、清单名称及整单计费设置已保存，跨暂停区间累计。')

    @app.get('/api/statistics/lists/export')
    def list_export():
        query = request.args.get('q', '')[:200]

        @stream_with_context
        def generate():
            yield '\ufeff'
            buffer = StringIO(newline='')
            writer = csv.writer(buffer)

            def line(values):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([csv_cell(value) for value in values])
                return buffer.getvalue()

            yield line(['清单ID', '客户', '清单名称', '创建时间', '视频任务', '待处理', '运行中', '成功', '失败',
                        '测试成功', '跳过', '计费文件数', '计费精确字节', '测试文件数', '测试精确字节', '整单单价', '单位', '整单预计费用', '统计口径'])
            cursor = None
            while True:
                page = store.lists.page(cursor, query, limit=1000)
                for row in page['items']:
                    if not row['tracked']:
                        row = {**row, **dict.fromkeys(('file_count', 'file_bytes_exact', 'test_file_count',
                            'test_file_bytes_exact', 'price', 'unit', 'amount'), '')}
                    yield line([row[key] for key in ('id', 'customer', 'name', 'created_at', 'total', 'queued', 'running',
                        'succeeded', 'failed', 'test_succeeded', 'skipped', 'file_count', 'file_bytes_exact',
                        'test_file_count', 'test_file_bytes_exact', 'price', 'unit', 'amount')] + [
                            '自创建累计；与会话费用不可叠加' if row['tracked'] else '历史清单无完整独立账本，未补算'])
                if not page['next_cursor']:
                    break
                cursor = page['next_cursor']

        return Response(generate(), mimetype='text/csv', headers={
            'Content-Disposition': 'attachment; filename="customer-lists.csv"', 'Cache-Control': 'no-store'})

    @app.get('/api/statistics/lists/<identity>/files/export')
    def list_file_export(identity):
        from sqlalchemy import select
        from webapp.database import batches
        with store.connect() as db:
            if not db.execute(select(batches.c.id).where(batches.c.id == identity)).first():
                abort(404, description='清单不存在。')
        test = request.args.get('kind') == 'test'

        @stream_with_context
        def generate():
            yield '\ufeff'
            buffer = StringIO(newline='')
            writer = csv.writer(buffer)
            writer.writerow(['清单ID', '视频ID', '标题', '精确字节', '登记时间', '任务ID', '文件路径', '是否计费'])
            yield buffer.getvalue()
            cursor = None
            while True:
                rows = store.lists.files(identity, test=test, cursor=cursor)
                for row in rows:
                    buffer.seek(0)
                    buffer.truncate()
                    writer.writerow([csv_cell(value) for value in (identity, row['video_id'], row['title'], row['size'],
                        row['created_at'], row['task_id'], row['path'], '否（测试文件已删除）' if test else '是')])
                    yield buffer.getvalue()
                if len(rows) < 1000:
                    break
                cursor = rows[-1]['seq']

        return Response(generate(), mimetype='text/csv', headers={
            'Content-Disposition': f'attachment; filename="list-{identity[:12]}-files.csv"', 'Cache-Control': 'no-store'})

    @app.get('/api/statistics', endpoint='statistics_overview')
    def overview():
        return jsonify(statistics.overview(request.args.get('cursor'), request.args.get('q', '')[:200]))

    @app.get('/api/statistics/export', endpoint='statistics_summary_export')
    def summary_export():
        query = request.args.get('q', '')[:200]

        @stream_with_context
        def generate():
            yield '\ufeff'
            buffer = StringIO(newline='')
            writer = csv.writer(buffer)

            def line(values):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([csv_cell(value) for value in values])
                return buffer.getvalue()

            yield line(['会话编号', '客户/清单备注', '开始时间', '结束时间', '状态', '参与任务', '成功任务', '失败任务',
                        '跳过任务', '测试成功', '未完成', '交付视频数', '精确字节数', '计费大小', '计费单位', '单价(元/单位)', '预计费用(元)',
                        '测试视频数', '测试精确字节数(不计费)', '全部完成视频数', '全部完成精确字节数'])
            cursor = None
            while True:
                with store.connect() as db:
                    rows = statistics.session_rows(db, cursor, query, limit=1000)
                for row in rows:
                    item = describe(row)
                    yield line([item[key] if item[key] is not None else '' for key in ('id', 'label', 'started_at', 'ended_at',
                        'status', 'attempted', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'unfinished',
                        'file_count', 'file_bytes', 'quantity', 'unit', 'price', 'amount',
                        'test_file_count', 'test_file_bytes', 'downloaded_file_count', 'downloaded_file_bytes')])
                if len(rows) < 1000:
                    break
                cursor = str(rows[-1]['seq'])

        return Response(generate(), mimetype='text/csv', headers={
            'Content-Disposition': 'attachment; filename="download-sessions.csv"', 'Cache-Control': 'no-store'})

    @app.put('/api/statistics/pricing', endpoint='statistics_pricing')
    def defaults():
        return jsonify(pricing=statistics.set_defaults(body()), message='默认单价已保存，对新清单和下一次下载会话生效。')

    @app.get('/api/statistics/sessions/<identity>', endpoint='statistics_details')
    def details(identity):
        return jsonify(session=session(identity), methods=statistics.method_summary(identity), **statistics.details(identity,
            kind=request.args.get('kind', 'files'), cursor=request.args.get('cursor')))

    def methods_export(rows, filename):
        buffer = StringIO(newline='')
        writer = csv.writer(buffer)
        writer.writerow(['下载提供方', '尝试次数', '成功尝试(含测试)', '失败尝试', '测试成功', '跳过任务', '未完成',
                         '成功率(%)', '交付视频数', '交付精确字节数(计费)', '测试视频数', '测试精确字节数(不计费)',
                         '全部完成视频数', '全部完成精确字节数', '自动切换任务数'])
        for row in rows:
            writer.writerow([csv_cell(value) for value in [row['label'], row['attempted'],
                row['succeeded'] + row['test_succeeded'], row['failed'], row['test_succeeded'], row['skipped'],
                row['unfinished'], row['success_rate'], row['file_count'], row['file_bytes_exact'], row['test_file_count'],
                row['test_file_bytes_exact'], row['downloaded_file_count'], row['downloaded_file_bytes_exact'], row['fallback_tasks']]])
        return Response('\ufeff' + buffer.getvalue(), mimetype='text/csv', headers={
            'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control': 'no-store'})

    @app.get('/api/statistics/methods/export', endpoint='statistics_methods_export')
    def all_methods_export():
        data = statistics.overview(query=request.args.get('q', '')[:200])
        return methods_export(data['methods'], 'download-methods.csv')

    @app.put('/api/statistics/sessions/<identity>', endpoint='statistics_update')
    def update(identity):
        session(identity)
        return jsonify(session=statistics.update(identity, body()), message='客户备注和本次计费设置已保存。')

    @app.get('/api/statistics/sessions/<identity>/export', endpoint='statistics_export')
    def export(identity):
        current = session(identity)
        kind = request.args.get('kind', 'files')
        if kind == 'methods':
            return methods_export(statistics.method_summary(identity), f'download-{identity[:12]}-methods.csv')
        if kind not in ('files', 'test_files', 'failed', 'tasks'):
            raise ValueError('导出类型无效。')
        # Fix the upper bound at request time, so an active session's CSV is finite.
        first = statistics.details(identity, kind=kind, limit=1000)

        @stream_with_context
        def generate():
            yield '\ufeff'
            buffer = StringIO(newline='')
            writer = csv.writer(buffer)

            def line(values):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([csv_cell(value) for value in values])
                return buffer.getvalue()

            if kind in ('files', 'test_files'):
                yield line(['会话编号', '客户/清单备注', '开始时间', '结束时间', '视频ID', '视频标题', '文件路径',
                            '精确字节数', '大小(GB)', '大小(GiB)', '计费单位', '单价(元/单位)', '登记时间', '保存方式'])
            else:
                yield line(['会话编号', '客户/清单备注', '视频ID', '任务ID', '导入批次ID', '结果', '开始时间', '结束时间', '失败原因'])
            page = first
            while True:
                for item in page['items']:
                    if kind in ('files', 'test_files'):
                        yield line([identity, current['label'], current['started_at'], current['ended_at'], item['video_id'],
                            item['title'], item['path'], str(item['size']),
                            format(Decimal(item['size']) / UNITS['GB'], '.9f'),
                            format(Decimal(item['size']) / UNITS['GiB'], '.9f'), current['unit'],
                            '0' if kind == 'test_files' else current['price'], item['created_at'],
                            '测试模式（文件已自动删除，不计费）' if kind == 'test_files' else '指定目录保存'])
                    else:
                        yield line([identity, current['label'], item['video_id'], item['task_id'], item['batch_id'],
                            item['status'], item['started_at'], item['finished_at'], item['error']])
                if not page['next_cursor']:
                    break
                page = statistics.details(identity, kind=kind, cursor=page['next_cursor'], limit=1000)

        return Response(generate(), mimetype='text/csv', headers={
            'Content-Disposition': f'attachment; filename="download-{identity[:12]}-{kind}.csv"',
            'Cache-Control': 'no-store'})

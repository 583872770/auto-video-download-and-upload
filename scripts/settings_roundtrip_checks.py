"""Browser checks shared by the isolated local and remote settings fixtures."""
import json
import yaml
from playwright.sync_api import expect


def reveal_setting(page, field):
    """Navigate the real settings tabs and expand ancestors before editing."""
    pane = field.locator('xpath=ancestor::*[@data-settings-category][1]')
    if pane.count() and not pane.is_visible():
        key = pane.get_attribute('data-settings-category')
        page.locator(f'[role=tab][data-settings-target="{key}"]').click()
    for details in field.locator('xpath=ancestor::details').all():
        if not details.evaluate('(el) => el.open'):
            details.locator(':scope > summary').click()
    expect(field).to_be_visible()
    return field


def check_settings_roundtrip(page, app, config_path):
    fleet = app.extensions['fleet']
    if fleet:
        from webapp.database import instances
        store = app.extensions['task_store']
        with store.connect() as db:
            overrides = json.loads(fleet.row(db)['limits'])
            overrides.update(download_methods=['yt_dlp'], download_method='yt_dlp',
                             scheduler_mode='ordered_fallback', storage_mode='local',
                             download_channels=[], downmp3_timeout=300, max_log_mb=2)
            db.execute(instances.update().where(instances.c.id == fleet.id).values(limits=json.dumps(overrides)))
    form = page.locator('#settings-form')
    save = form.locator('button[type=submit]')
    submitted = []
    page.on('request', lambda req: submitted.append(req.post_data_json)
            if req.method == 'PUT' and req.url.endswith('/api/config') else None)

    def persist(values):
        with page.expect_response(lambda response: response.request.method == 'PUT'
                                  and response.url.endswith('/api/config')):
            save.click()
        expect(save).to_be_enabled()
        expect(page.locator('#settings-save-state')).to_contain_text('后续领取')
        config = app.test_client().get('/api/config').json['config']
        disk = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        for key, value in values.items():
            assert config[key] == value, (key, config[key], value)
            assert disk[key] == value, (key, disk[key], value)
            fleet = app.extensions['fleet']
            if fleet:
                assert fleet.local_settings()['config'][key] == value, key
        page.reload()
        expect(form.locator('[name=scheduler_mode]')).to_have_value(config['scheduler_mode'])
        for element in form.locator('input[name],select[name],textarea[name]').all():
            key = element.get_attribute('name')
            value = config[key]
            text = str(value).lower() if isinstance(value, bool) else str(value)
            expect(element).to_have_value(text)
        for method in ['yt_dlp', 'yt5s', 'downmp3', 'yt1d', 'bergung']:
            expect(page.locator(f'[data-provider={method}] input')).to_be_checked(
                checked=method in config['download_methods'])
        return config

    # Start in auto mode, then change exactly the candidate list the user reported.
    reveal_setting(page, form.locator('[name=throughput_mode]')).select_option('auto')
    reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('auto')
    persist({'scheduler_mode': 'auto', 'throughput_mode': 'auto'})
    for method in ['yt_dlp', 'yt5s', 'downmp3', 'yt1d', 'bergung']:
        checkbox = page.locator(f'[data-provider={method}] input')
        if not checkbox.is_checked():
            checkbox.check()
    save.click()
    expect(save).to_be_enabled()
    expect(page.locator('#settings-save-state')).to_contain_text('后续领取')
    for method in ['yt_dlp', 'yt5s', 'yt1d']:
        page.locator(f'[data-provider={method}] input').uncheck()
    persist({'download_methods': ['downmp3', 'bergung']})
    assert submitted[-1]['download_methods'] == ['downmp3', 'bergung'], submitted[-1]

    # Every ordinary input/select, including string booleans and resource limits.
    values = {'storage_mode': 'local', 'path': 'roundtrip-videos', 'dir': 'nested',
              'yt5s_quality': '720', 'yt1d_quality': '1440', 'bergung_quality': '2160',
              'downmp3_quality': '480', 'yt1d_timeout': 121, 'bergung_timeout': 122,
              'downmp3_timeout': 241, 'fragment_concurrency': 3, 'download_attempts': 4,
              'shard_downloads': False, 'scheduler_mode': 'balanced',
              'auto_channel_attempts': 5, 'shared_parser': False,
              'download_connection_limit': 25, 'throughput_mode': 'manual',
              'worker_count': 4, 'yt5s_browser_count': 3, 'bandwidth_mbps': 51,
              'min_free_gb': 1, 'max_log_mb': 3, 'log_retention_days': 2,
              'disk_budget_gb': 21, 'disk_test_budget_gb': 5,
              'disk_browser_budget_mb': 101, 'disk_log_budget_mb': 65,
              'disk_import_budget_mb': 257, 'disk_cache_budget_mb': 102,
              'disk_min_free_gb': 2, 'disk_resume_free_gb': 3,
              'disk_upload_limit_mb': 129, 'disk_success_log_hours': 25,
              'disk_failure_log_days': 8}
    # Provider-specific fields are visible only while that provider is selected.
    # Exercise them through the current UI instead of forcing hidden controls.
    for method in ['yt_dlp', 'yt5s', 'downmp3', 'yt1d', 'bergung']:
        page.locator(f'[data-provider={method}] input').check()
    for key, value in values.items():
        element = reveal_setting(page, form.locator(f'[name={key}]'))
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if element.evaluate('(el) => el.tagName') == 'SELECT':
            element.select_option(text)
        else:
            element.fill(text)
    # Valid normalization must not be mistaken for a failed write.
    reveal_setting(page, form.locator('[name=fragment_concurrency]')).fill('03')
    persist(values)
    for method in ['yt_dlp', 'yt5s', 'yt1d']:
        page.locator(f'[data-provider={method}] input').uncheck()
    persist({'download_methods': ['downmp3', 'bergung']})

    # Channel entries have no name attributes and must be serialized separately.
    reveal_setting(page, form.locator('[name=scheduler_mode]'))
    form.locator('#channel-settings details').last.locator('summary').click()
    form.locator('#channel-add').click()
    entry = form.locator('.channel-entry').last
    channel = {'id': 'fixture', 'label': 'Roundtrip', 'site_url': 'https://fixture.example',
               'api_url': 'https://api.fixture.example', 'upstream_group': 'fixture-media',
               'resolver_group': 'fixture-parser', 'quota_group': 'fixture-quota',
               'transfer_limit': 5, 'resolver_limit': 3, 'connection_limit': 9,
               'task_limit': 6, 'weight': 2, 'verification': 'none'}
    for key, value in channel.items():
        field = entry.locator(f'[data-channel-field={key}]')
        if key == 'verification':
            field.select_option(value)
        else:
            field.fill(str(value))
    channel.update(adapter='bergung', site_url=channel['site_url']+'/', api_url=channel['api_url']+'/')
    persist({'download_channels': [channel]})
    for mode in ['ordered_fallback', 'auto', 'balanced']:
        reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option(mode)
        persist({'scheduler_mode': mode})
    reveal_setting(page, form.locator('[name=scheduler_mode]'))
    form.locator('#channel-settings details').last.locator('summary').click()
    expect(form.locator('[data-channel-field=id]')).to_have_value('fixture')
    form.locator('[data-channel-remove]').click()
    persist({'download_channels': []})

    # A 200 acknowledgement alone must never discard an unpersisted strategy.
    reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('auto')
    expect(page.locator('#strategy-save-state')).to_contain_text('有未保存')
    reveal_setting(page, page.locator('[data-provider=yt1d] input')).check()
    expect(page.locator('#download-method-note')).to_contain_text('自动候选：downmp3、Bergung / Cobalt、yt1d。')
    def acknowledge_without_saving(route):
        if route.request.method == 'PUT':
            route.fulfill(json={'message': 'ok'})
        else:
            route.continue_()
    page.route('**/api/config', acknowledge_without_saving)
    save.click()
    expect(save).to_be_enabled()
    expect(page.locator('#settings-save-state')).to_contain_text('未确认')
    expect(page.locator('#settings-save-state')).to_contain_text('下载方式')
    expect(page.locator('#settings-save-state')).to_contain_text('渠道选择策略')
    expect(page.locator('#settings-save-state')).to_contain_text('提交：downmp3、Bergung / Cobalt、yt1d')
    expect(page.locator('#settings-save-state')).to_contain_text('读回：downmp3、Bergung / Cobalt')
    expect(form.locator('[name=scheduler_mode]')).to_have_value('auto')
    expect(page.locator('[data-provider=yt1d] input')).to_be_checked()
    expect(page.locator('#strategy-save-state')).to_contain_text('有未保存')
    page.unroute('**/api/config', acknowledge_without_saving)
    persist({'scheduler_mode': 'auto', 'download_methods': ['downmp3', 'bergung', 'yt1d']})

    # Even if PUT succeeded, a failed readback must retain the editable draft.
    reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('ordered_fallback')
    reveal_setting(page, page.locator('[data-provider=yt1d] input'))
    page.locator('[data-provider=yt1d] input').uncheck()
    def fail_readback(route):
        if route.request.method == 'GET':
            route.fulfill(status=503, json={'error': 'fixture readback unavailable'})
        else:
            route.continue_()
    page.route('**/api/config', fail_readback)
    save.click()
    expect(save).to_be_enabled()
    expect(page.locator('#settings-save-state')).to_contain_text('fixture readback unavailable')
    expect(form.locator('[name=scheduler_mode]')).to_have_value('ordered_fallback')
    expect(page.locator('[data-provider=yt1d] input')).not_to_be_checked()
    expect(page.locator('#strategy-save-state')).to_contain_text('有未保存')
    page.unroute('**/api/config', fail_readback)
    persist({'scheduler_mode': 'ordered_fallback', 'download_methods': ['downmp3', 'bergung']})

    # Edits cannot be lost to a slow save/readback while the form is busy.
    pending = []
    def hold_save(route):
        if route.request.method == 'PUT':
            pending.append(route)
        else:
            route.continue_()
    reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('balanced')
    page.route('**/api/config', hold_save)
    save.click()
    expect(form).to_have_attribute('aria-busy', 'true')
    assert form.evaluate('(el) => el.inert')
    assert pending, 'save request not issued'
    pending[0].continue_()
    expect(save).to_be_enabled()
    assert not form.evaluate('(el) => el.inert')
    page.unroute('**/api/config', hold_save)
    page.reload()
    expect(form.locator('[name=scheduler_mode]')).to_have_value('balanced')

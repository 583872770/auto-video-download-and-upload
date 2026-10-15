"""Kuaishou uploader using the current creator form and positive UI receipts."""
import asyncio
from datetime import datetime
from pathlib import Path
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from Uploaders.browser import (
    UPLOAD_INPUT, UPLOAD_URLS, browser_session, check_login, generate_login,
    confirm_publication, refresh_login_after_publish, wait_for_upload,
)


async def cookie_auth(account_file, executable_path=None):
    async with async_playwright() as playwright:
        return await check_login(playwright, 'kuaishou', account_file, executable_path)


async def kuaishou_setup(account_file, handle=False):
    if Path(account_file).is_file() and await cookie_auth(account_file):
        return True
    if not handle:
        return False
    await kuaishou_cookie_gen(account_file)
    return True


async def kuaishou_cookie_gen(account_file, executable_path=None):
    async with async_playwright() as playwright:
        await generate_login(playwright, 'kuaishou', account_file, executable_path)


class KuaiShouVideo:
    def __init__(self, title, file_path, cover_path, tags, publish_date: datetime, account_file):
        self.title = title
        self.file_path = file_path
        self.cover_path = cover_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.local_executable_path = ''
        self.visibility = 'public'

    async def set_schedule_time_kuaishou(self, page, publish_date):
        await page.get_by_text('定时发布', exact=True).click()
        field = page.locator('input[placeholder="选择日期时间"]')
        value = publish_date.strftime('%Y-%m-%d %H:%M:%S')
        await field.click()
        picker = page.locator('.ant-picker-dropdown:visible')
        date = publish_date.strftime('%Y-%m-%d')
        for _ in range(2):
            cell = picker.locator(f'td[title="{date}"]:not(.ant-picker-cell-disabled)')
            if await cell.count():
                await cell.click()
                break
            await picker.locator('button.ant-picker-header-next-btn').click()
        else:
            raise RuntimeError('快手日期选择器中没有可用的指定日期')
        columns = picker.locator('.ant-picker-time-panel-column')
        for index, number in enumerate((publish_date.hour, publish_date.minute, publish_date.second)):
            await columns.nth(index).locator(
                '.ant-picker-time-panel-cell:not(.ant-picker-time-panel-cell-disabled)'
            ).filter(has_text=re.compile(f'^{number:02d}$')).click()
        await picker.get_by_role('button', name='确定', exact=True).click()
        if await field.input_value() != value:
            raise RuntimeError('快手未接受指定排期，已停止提交')

    async def handle_upload_error(self, page):
        await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)

    async def prepare(self, page):
        """Fill the actual form without pressing Publish; also used for live QA."""
        labels = {'public': '所有人可见', 'friends': '好友可见', 'private': '仅自己可见'}
        if self.visibility not in labels:
            raise ValueError('kuaishou_visibility 必须是 public/friends/private')
        try:
            await page.goto(UPLOAD_URLS['kuaishou'], wait_until='domcontentloaded', timeout=60000)
            await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)
        except Exception as error:
            raise RuntimeError('快手上传页打开失败，请检查登录状态、网络和页面元素') from error
        await wait_for_upload(
            page, 'text="预览作品"', 'text="上传失败"',
            lambda: self.handle_upload_error(page),
            timeout=getattr(self, 'upload_timeout_seconds', 1800),
        )
        print('快手视频上传完成，正在填写表单。', flush=True)
        # First-time creator onboarding otherwise intercepts every form click.
        skip = page.get_by_role('button', name='Skip', exact=True)
        try:
            await skip.wait_for(state='visible', timeout=2000)
        except PlaywrightTimeoutError:
            pass
        else:
            await skip.click()
        description = page.locator('#work-description-edit')
        await description.fill(self.title)
        for tag in self.tags:
            await page.type('#work-description-edit', ' #' + tag)
            await page.press('#work-description-edit', 'Space')
        # The persistent image input applies the cover directly. Opening the old
        # crop dialog and confirming afterward can overwrite it with a frame.
        await page.locator('input[type="file"][accept*="image/"]').first.set_input_files(self.cover_path)
        await page.locator('.ant-message-notice').filter(has_text='封面应用成功').first.wait_for(
            state='visible', timeout=60000)
        print('快手自定义封面已应用。', flush=True)
        await page.get_by_text(labels[self.visibility], exact=True).click()
        if self.publish_date != 0:
            await self.set_schedule_time_kuaishou(page, self.publish_date)

    async def upload(self, playwright):
        async with browser_session(
            playwright, account_file=self.account_file, headless=True,
            executable_path=self.local_executable_path or None,
        ) as context:
            page = await context.new_page()
            await self.prepare(page)
            await page.locator('div[class*="_button-primary_"]').filter(
                has_text=re.compile(r'^发布$')).click()
            receipt = await confirm_publication(page, 'kuaishou')
            await refresh_login_after_publish(context, self.account_file)
            return receipt

    async def main(self):
        async with async_playwright() as playwright:
            return await asyncio.wait_for(
                self.upload(playwright), timeout=getattr(self, 'upload_timeout_seconds', 1800))


if __name__ == '__main__':
    raise SystemExit('请从项目根目录运行 main.py --publish --platforms kuaishou')

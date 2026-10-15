"""Douyin creator uploader with current dual-cover and publication controls."""
import asyncio
from datetime import datetime
from pathlib import Path
import re

from playwright.async_api import async_playwright
from Uploaders.browser import (
    UPLOAD_INPUT, UPLOAD_URLS, browser_session, check_login, generate_login,
    confirm_publication, refresh_login_after_publish, wait_for_upload, ensure_upload_origin,
)


async def cookie_auth(account_file, executable_path=None):
    async with async_playwright() as playwright:
        return await check_login(playwright, 'douyin', account_file, executable_path)


async def douyin_setup(account_file, handle=False):
    if Path(account_file).is_file() and await cookie_auth(account_file):
        return True
    if not handle:
        return False
    await douyin_cookie_gen(account_file)
    return True


async def douyin_cookie_gen(account_file, executable_path=None):
    async with async_playwright() as playwright:
        await generate_login(playwright, 'douyin', account_file, executable_path)


class DouYinVideo:
    def __init__(self, title, file_path, cover_path, tags, publish_date: datetime, account_file):
        self.title = title
        self.file_path = file_path
        self.cover_path = cover_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.local_executable_path = ''
        self.horizontal_cover_path = None
        self.visibility = 'public'

    async def set_schedule_time_douyin(self, page, publish_date):
        await page.locator('label').filter(has_text=re.compile(r'^定时发布\s*$')).click()
        field = page.locator('input[placeholder="日期和时间"]')
        value = publish_date.strftime('%Y-%m-%d %H:%M')
        await field.fill(value)
        await field.press('Enter')
        if await field.input_value() != value:
            raise RuntimeError('抖音未接受指定排期，已停止提交')

    async def handle_upload_error(self, page):
        await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)

    async def upload_covers(self, page):
        await page.locator('[class^="coverControl-"]').nth(0).click()
        image_input = '.dy-creator-content-modal-body [class^="selectArea-"]:visible input[type="file"]'
        await page.locator(image_input).first.set_input_files(self.horizontal_cover_path or self.cover_path)
        await page.get_by_role('button', name='设置竖封面', exact=True).click()
        await page.locator(image_input).first.set_input_files(self.cover_path)
        await page.get_by_role('button', name='完成', exact=True).click()
        await page.locator('.dy-creator-content-modal-body').wait_for(state='hidden', timeout=60000)
        await page.locator('[class^="coverControl-"] img').nth(1).wait_for(state='attached', timeout=60000)
        print('抖音横、竖自定义封面已设置。', flush=True)

    async def prepare(self, page):
        labels = {'public': '公开', 'friends': '好友可见', 'private': '仅自己可见'}
        if self.visibility not in labels:
            raise ValueError('douyin_visibility 必须是 public/friends/private')
        try:
            await page.goto(UPLOAD_URLS['douyin'], wait_until='commit', timeout=60000)
            await page.locator(UPLOAD_INPUT).first.wait_for(state='attached', timeout=60000)
            ensure_upload_origin(page, 'douyin')
            await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError('抖音上传页打开失败，请检查登录状态、网络和页面元素') from error
        await page.wait_for_url('**/creator-micro/content/post/video**', timeout=60000)
        await wait_for_upload(
            page, 'text="重新上传"', 'text="上传失败"', lambda: self.handle_upload_error(page),
            timeout=getattr(self, 'upload_timeout_seconds', 1800),
        )
        know = page.get_by_role('button', name='我知道了', exact=True).first
        if await know.is_visible():
            await know.click()
        await page.locator('input[placeholder="填写作品标题，为作品获得更多流量"]').fill(self.title[:30])
        description = page.locator('[contenteditable="true"].zone-container')
        await description.fill(self.title)
        for tag in self.tags:
            await page.type('[contenteditable="true"].zone-container', ' #' + tag)
            await page.press('[contenteditable="true"].zone-container', 'Space')
        await self.upload_covers(page)
        visibility = page.locator('label').filter(has_text=re.compile('^' + labels[self.visibility] + r'\s*$'))
        await visibility.click()
        if not await visibility.locator('input').is_checked():
            raise RuntimeError('抖音未确认指定可见范围，已停止提交')
        if self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)
        print('抖音上传表单已填写，尚未点击发布。', flush=True)

    async def upload(self, playwright):
        async with browser_session(
            playwright, account_file=self.account_file, headless=True,
            executable_path=self.local_executable_path or None,
        ) as context:
            page = await context.new_page()
            await self.prepare(page)
            await page.get_by_role('button', name='发布', exact=True).click()
            receipt = await confirm_publication(page, 'douyin')
            await refresh_login_after_publish(context, self.account_file)
            return receipt

    async def main(self):
        async with async_playwright() as playwright:
            return await asyncio.wait_for(
                self.upload(playwright), timeout=getattr(self, 'upload_timeout_seconds', 1800))


if __name__ == '__main__':
    raise SystemExit('请从项目根目录运行 main.py --publish --platforms douyin')

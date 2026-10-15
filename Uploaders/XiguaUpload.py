# -*- coding: utf-8 -*-
import pathlib
from Uploaders.browser import (UPLOAD_INPUT, browser_session, check_login,
    generate_login, confirm_publication, refresh_login_after_publish, wait_for_upload, ensure_upload_origin)
from datetime import datetime

from playwright.async_api import Playwright, async_playwright, TimeoutError as PlaywrightTimeoutError
import os
import asyncio
import yaml

async def cookie_auth(account_file, executable_path=None):
    async with async_playwright() as playwright:
        return await check_login(playwright, 'xigua', account_file, executable_path)

async def xigua_setup(account_file, handle=False):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            # Todo alert message
            return False
        print('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await xigua_cookie_gen(account_file)
    return True

async def xigua_cookie_gen(account_file, executable_path=None):
    async with async_playwright() as playwright:
        await generate_login(playwright, 'xigua', account_file, executable_path)




class XiGuaVideo(object):
    def __init__(self, title, file_path, cover_path, tags, publish_date: datetime, account_file):
        self.title = title  # 视频标题
        self.file_path = file_path
        self.cover_path = cover_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.date_format = '%Y年%m月%d日 %H:%M'
        self.local_executable_path = ""  # change me
        self.ua = {
            "web": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                   "Safari/537.36",
            "app": "com.ss.android.ugc.aweme/110101 (Linux; U; Android 5.1.1; zh_CN; MI 9; Build/NMF26X; "
                   "Cronet/TTNetVersion:b4d74d15 2020-04-23 QuicVersion:0144d358 2020-03-24)"
        }

    async def set_schedule_time_xigua(self, page, publish_date):
        # 选择包含特定文本内容的 label 元素
        label_element = page.locator("label.byte-radio:has-text('定时发布')")
        # 在选中的 label 元素下点击 checkbox
        await label_element.click()
        await asyncio.sleep(1)
        # 获取日期部分（年月日）
        date_part = publish_date.strftime("%Y-%m-%d")

        # 获取时间部分（时分）
        time_part = publish_date.strftime("%H:%M")


        input_locator=page.locator('input[placeholder="请选择时间"]').first
        # 设置新的 value 值
        # await input_locator.type(new_value)
        # 使用 JavaScript 来设置新的 value 值(只是表面改了，实际并没改，无用)
        # 移除 readonly 属性
        await page.evaluate("document.querySelector('input.byte-input-prefix.byte-input.byte-input-size-default').readOnly = false;")
        # 现在尝试设置值
        await input_locator.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(date_part))


        await page.locator('input[placeholder="请选择时间"]').nth(1).click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(time_part))

        await page.keyboard.press("Enter")

        await page.evaluate("document.querySelector('input.byte-input-prefix.byte-input.byte-input-size-default').readOnly = true;")



    async def handle_upload_error(self, page):
        print("视频出错了，重新上传中")
        await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)


    async def upload(self, playwright: Playwright) -> dict:
        async with browser_session(
            playwright, account_file=self.account_file,
            headless=False, executable_path=self.local_executable_path or None,
            user_agent=self.ua["web"],
        ) as context:
            page = await context.new_page()

            try:
                print('[+]正在上传-------{}.mp4'.format(self.title))
                # 等待页面跳转到指定的 URL，没进入，则自动等待到超时
                await asyncio.sleep(1)
                await page.goto("https://studio.ixigua.com/upload?from=post_article")
                # 点击 "上传视频" 按钮
                await page.locator(UPLOAD_INPUT).first.wait_for(state='attached', timeout=30000)
                ensure_upload_origin(page, 'xigua')
                await page.locator(UPLOAD_INPUT).first.set_input_files(self.file_path)
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError('西瓜上传页打开失败，请检查登录状态、网络和页面元素') from e

            await asyncio.sleep(1)
            print("  [-] 正在填充标题和话题...")

            title_selector='div.DraftEditor-root'
            await page.locator(title_selector).first.click()
            await page.keyboard.type(self.title)

            tag_selector='div.arco-input-tag-inner'
            await page.locator(tag_selector).click()
            for i in self.tags:
                await page.keyboard.type(i)
                await asyncio.sleep(2)
                await page.keyboard.press("Enter")
                # await asyncio.sleep(0.3)



            # 取消同步到抖音
             # 等待元素出现在页面上
            await page.wait_for_selector('div.m-xigua-label:has-text("同步至抖音")')

            # 执行鼠标悬停操作
            await page.locator('div.m-xigua-label:has-text("同步至抖音")').hover()

            # 等待半秒
            await asyncio.sleep(0.5)

            # 点击 "不同步至抖音" 选项
            await page.locator('div.m-xigua-option:has-text("不同步至抖音")').click()
            # 点击放弃同步
            await page.locator('button.m-button.red:has-text("放弃同步")').click()



            # 点击原创
            await page.locator('.byte-radio-inner-text:has-text("原创")').click()

            # 视频简介
            await page.locator('div.public-DraftStyleDefault-block.public-DraftStyleDefault-ltr').nth(1).click()
            await page.keyboard.type(f'{self.title}')



            # 点击上传封面
            await page.locator('div.m-xigua-upload').click()
            # 点击本地上传
            await page.locator('li:has-text("本地上传")').click()
            # 上传封面(西瓜特殊，input[type=file]是隐藏的，CTRL+F找input)

            await page.locator('input[accept="image/jpg,image/jpeg,image/png,image/x-png,image/webp"]').set_input_files(self.cover_path)
            # <input type="file" accept="image/jpg,image/jpeg,image/png,image/x-png,image/webp" style="display: none;">


            # 封面编辑
            # 点击完成裁剪

            # 经过dlcover后不需要点击完成裁剪
            # await page.locator('div.clip-btn-content:has-text("完成裁剪")').click()
            # 点击智能美化
            # await page.locator('div.tooltip:has-text("智能美化")').click()
            # 点击确定
            await page.locator('button.btn-l.btn-sure.ml16:has-text("确定")').click()
            # 再次点击确定
            await page.locator('button.m-button.red:has-text("确定")').click()


            if self.publish_date != 0:
                await self.set_schedule_time_xigua(page, self.publish_date)


            await wait_for_upload(
                page, 'text=上传成功', 'text=上传失败',
                lambda: self.handle_upload_error(page),
                timeout=getattr(self, 'upload_timeout_seconds', 1800),
            )



            # 点击发布按钮
            await asyncio.sleep(0.1)
            await page.locator('button.action-footer-btn.submit.m-button.red:has-text("发布")').click()


            receipt = await confirm_publication(page, 'xigua')
            await refresh_login_after_publish(context, self.account_file)
            return receipt




    async def main(self):
        async with async_playwright() as playwright:
            return await asyncio.wait_for(self.upload(playwright), timeout=getattr(self, 'upload_timeout_seconds', 1800))



if __name__ == '__main__':
    with open('config.yaml', encoding='utf-8') as f:
        config = yaml.load(f.read(), Loader=yaml.FullLoader)
    f.close()
    fullpath = config['path']+config['dir']
    print(fullpath)
    # videoFile = getFileNames(fullpath)
    # print(f"{fullpath} 文件夹下有 {len(videoFile)} 个视频")

    account_file = 'xigua.json'
    # asyncio.run(kuaishou_cookie_gen(account_file))

    # publish_datetimes = generate_schedule_time_next_day(file_num, 1, daily_times=[16])
    # cookie_setup = asyncio.run(douyin_setup(account_file, handle=False))

    tags=["我的世界","mc不灭",'撒旦发射点']
    file= r"D:\desktop\myself\video\我的世界：变成钻石骷髅在MC中生存100天！【第1集】.mp4"
    title="我的世界：变成在MC中生存100天【第一集】"
    cover=r"D:\desktop\myself\videos\image1.png"
    app = XiGuaVideo(title, file, cover, tags, datetime(2024, 3, 25, 10, 30, 0), account_file)
    asyncio.run(app.main(), debug=False)

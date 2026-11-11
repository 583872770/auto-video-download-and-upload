"""Let the user sign in normally, then save this independent YouTube session."""
import argparse
import asyncio
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright, Error as BrowserError, TimeoutError as BrowserTimeoutError
from webapp.browser import browser_session
from webapp.email_codes import email_inbox, WAIT_SECONDS
from Utils.config import DEFAULT_CONFIG, load_config
from Utils.runtime import ROOT, configure_runtime
from Utils.youtube_login import save_youtube_cookies


class LoginCancelled(RuntimeError):
    pass


def navigating(error):
    detail = str(error).lower()
    return 'execution context was destroyed' in detail or 'cannot find context' in detail


async def current_account_name(page):
    """Read only the active YouTube account header, never other signed-in accounts."""
    try:
        await page.locator('#avatar-btn').first.click(timeout=2000)
        header = page.locator('ytd-active-account-header-renderer').first
        await header.wait_for(state='visible', timeout=3000)
        for selector in ('#email', '#account-name', '#channel-handle'):
            try:
                label = await header.locator(selector).first.inner_text(timeout=1000)
            except BrowserError:
                continue
            label = ' '.join(label.split())
            if label:
                return label
    except BrowserError:
        pass
    return None


async def login(config, output, *, on_status=None, cancelled=None, timeout_seconds=600,
                credentials=None, expected_email=None):
    """Use a fresh visible session; cancellation also closes its browser."""
    def report(state, message):
        if on_status:
            on_status(state, message)

    async def watch_cancel():
        while cancelled is None or not cancelled.is_set():
            await asyncio.sleep(0.2)

    options = {'credentials': credentials, 'expected_email': expected_email} if credentials or expected_email else {}
    operation = asyncio.create_task(_login(config, output, report, **options))
    cancellation = asyncio.create_task(watch_cancel())
    try:
        done, _ = await asyncio.wait((operation, cancellation), timeout=timeout_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        if cancellation in done:
            raise LoginCancelled('登录已取消，已有 Cookie 保持不变。')
        raise RuntimeError('10 分钟内未确认 YouTube 登录，未覆盖已有下载凭据。')
    finally:
        operation.cancel()
        cancellation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


class GoogleSignIn:
    """Submit each saved field once on Google's sign-in origin, then hand off."""
    IDENTIFIER = 'input#identifierId:visible, input[name="identifier"]:visible, input[type="email"]:visible'
    PASSWORD = 'input[name="Passwd"]:visible, input[type="password"]:visible'
    CHALLENGE = ('input[name="totpPin"]:visible, input[name="idvPin"]:visible, '
                 'input[type="tel"]:visible, iframe[src*="recaptcha"]:visible, input[name="ca"]:visible')
    EMAIL_PIN = ('input#idvPinId:visible, input[name="Pin"]:visible, '
                 'input#idvPin:visible, input[name="idvPin"]:visible')
    EMAIL_NEXT = '#idvPreregisteredEmailNext button:visible, #idvEmailNext button:visible'
    HUMAN_CHECK = 'iframe[src*="recaptcha"]:visible, input[name="ca"]:visible'

    def __init__(self, credentials, report, *, inbox=None):
        self.email = credentials['email']
        self.password = credentials['password']
        self.report = report
        self.email_sent = self.password_sent = self.stopped = False
        self.inbox = inbox
        self.code_started = None
        self.code_sent = False
        self.choice_started = None
        self.choice_sent_at = None
        self.consent_started = None
        self.consent_sent_at = None

    @staticmethod
    def trusted(page):
        address = urlsplit(page.url)
        return (address.scheme == 'https' and address.hostname == 'accounts.google.com'
                and address.port in (None, 443))

    def email_challenge(self, page):
        return self.trusted(page) and bool(re.search(r'/challenge/ipe(?:/verify)?/?$', urlsplit(page.url).path))

    async def choose_email(self, page):
        if not self.inbox or self.inbox.error:
            self.hand_off('邮件接码不可用，请在浏览器窗口选择验证方式并手动完成验证。')
            return
        if self.choice_sent_at is not None:
            if time.monotonic() - self.choice_sent_at >= 15:
                self.hand_off('邮件验证方式已点击，但页面尚未进入验证码输入步骤，请在浏览器窗口继续。')
            return
        if self.choice_started is None:
            self.choice_started = time.monotonic()
            self.report('waiting', '正在选择发送到当前账号邮箱的验证码验证方式…')
        # Match the visible destination and action together. Phone, device,
        # masked destinations and a different recovery mailbox need the user.
        label = re.compile(r'^(?:Get a verification code at|获取验证码(?:[，,]?\s*发送至|[，,]?\s*发送到)?|将验证码发送至)\s*'
                           + re.escape(self.email) + r'\s*$', re.IGNORECASE)
        choices = page.get_by_text(label)
        for index in range(min(await choices.count(), 20)):
            choice = choices.nth(index)
            if not await choice.is_visible():
                continue
            if not self.trusted(page) or not re.search(r'/challenge/selection/?$', urlsplit(page.url).path):
                return
            self.choice_sent_at = time.monotonic()
            await choice.click(timeout=3000)
            self.report('waiting', '已选择当前账号的邮件验证码方式，正在等待验证码页面。')
            return
        if time.monotonic() - self.choice_started >= 10:
            self.hand_off('未找到明确发送到当前账号邮箱的验证码选项，请在浏览器窗口选择验证方式。')

    def hand_off(self, message):
        self.stopped = True
        self.password = None
        self.report('waiting', message)

    async def confirm_email(self, page):
        # This confirmation belongs to the exact mailbox option we selected.
        # A direct/unverified consent page still needs the user to choose it.
        if self.choice_sent_at is None or not self.inbox or self.inbox.error:
            self.hand_off('请在浏览器窗口确认邮件验证码的发送目标并继续验证。')
            return
        if self.consent_started is None:
            self.consent_started = time.monotonic()
            self.report('waiting', '正在等待 Google 邮件发送确认页加载，加载后自动点击发送。')
        if self.consent_sent_at is not None:
            if time.monotonic() - self.consent_sent_at >= 30:
                self.hand_off('已点击发送，但 Google 尚未进入验证码输入页，请在浏览器窗口继续验证。')
            return
        button = page.get_by_role('button', name=re.compile(r'^(?:Send|发送)$', re.IGNORECASE)).first
        if await button.is_visible() and await button.is_enabled():
            if (not self.trusted(page)
                    or not re.search(r'/challenge/ipe/consent/?$', urlsplit(page.url).path)):
                return
            self.consent_sent_at = time.monotonic()
            await button.click(timeout=3000)
            self.report('waiting', '已确认向当前账号邮箱发送验证码，正在等待验证码输入页。')
        elif time.monotonic() - self.consent_started >= 30:
            self.hand_off('Google 邮件发送确认页持续加载或发送按钮不可用，请在浏览器窗口继续验证。')

    async def receive_email_code(self, page):
        pin = page.locator(self.EMAIL_PIN).first
        if self.code_sent:
            if await pin.get_attribute('aria-invalid') == 'true':
                self.hand_off('邮件验证码未被接受，请在浏览器窗口继续验证；不会自动重复提交。')
            return
        if not self.inbox or self.inbox.error:
            self.hand_off('邮件接码不可用，请检查账号的接码网址，或在浏览器窗口手动填写验证码。')
            return
        if self.code_started is None:
            self.code_started = time.monotonic()
            self.password = None
            self.report('waiting', '正在等待此账号的新邮件验证码，每 30 秒检查一次，最多等待 3 分钟。')
        if time.monotonic() - self.code_started >= WAIT_SECONDS:
            self.hand_off('3 分钟内未收到可确认的新邮件验证码，请在浏览器窗口继续验证。')
            return
        if await pin.input_value():
            self.hand_off('验证码框已有内容，请在浏览器窗口继续验证。')
            return
        try:
            code = await self.inbox.poll()
        except (ValueError, BrowserError):
            self.hand_off('邮件接码暂不可用或收到多封待确认邮件，请在浏览器窗口继续验证。')
            return
        # The user may have navigated or entered a code while the inbox refreshed.
        if not code or not self.email_challenge(page) or not await pin.is_visible():
            return
        if await page.locator(self.HUMAN_CHECK).first.is_visible() or await pin.input_value():
            self.hand_off('需要在浏览器窗口继续完成验证，自动接码已停止。')
            return
        if not self.email_challenge(page):
            return
        self.code_sent = True
        await pin.fill(code, timeout=3000)
        if self.email_challenge(page):
            next_button = page.locator(self.EMAIL_NEXT).first
            if not await next_button.is_visible():
                next_button = page.get_by_role('button', name=re.compile(r'^(?:Next|下一步)$', re.IGNORECASE)).first
            if await next_button.is_visible():
                await next_button.click(timeout=3000)
            else:
                await pin.press('Enter', timeout=3000)
        self.report('waiting', '已提交新收到的邮件验证码，正在等待 Google 确认登录。')

    async def advance(self, page):
        if self.stopped or not self.trusted(page):
            return
        try:
            if (re.search(r'/challenge/ipe/consent/?$', urlsplit(page.url).path)
                    and not await page.locator(self.HUMAN_CHECK).first.is_visible()):
                await self.confirm_email(page)
                return
            if (re.search(r'/challenge/selection/?$', urlsplit(page.url).path)
                    and not await page.locator(self.HUMAN_CHECK).first.is_visible()):
                await self.choose_email(page)
                return
            if (self.email_challenge(page) and await page.locator(self.EMAIL_PIN).first.is_visible()
                    and not await page.locator(self.HUMAN_CHECK).first.is_visible()):
                await self.receive_email_code(page)
                return
            challenge = page.locator(self.CHALLENGE).first
            if await challenge.is_visible():
                self.stopped = True
                self.password = None
                self.report('waiting', '需要验证码或二次验证，请在浏览器窗口继续完成登录。')
                return
            # Google serves identifierId as type=text; hidden decoy password
            # inputs must not hide the real field later in the document.
            identifier = page.locator(self.IDENTIFIER).first
            password = page.locator(self.PASSWORD).first
            if not self.email_sent and await identifier.is_visible():
                self.email_sent = True
                if not self.trusted(page):
                    return
                await identifier.fill(self.email, timeout=3000)
                if self.trusted(page):
                    await page.locator('#identifierNext button').first.click(timeout=3000)
                self.report('waiting', '已提交邮箱，正在等待密码页面；需要时可在浏览器中接管。')
            elif not self.password_sent and await password.is_visible():
                self.password_sent = True
                if not self.trusted(page):
                    return
                await password.fill(self.password, timeout=3000)
                self.password = None
                if self.trusted(page):
                    await page.locator('#passwordNext button').first.click(timeout=3000)
                self.report('waiting', '已提交密码，等待登录确认；如有验证码或二次确认，请在浏览器窗口完成。')
        except BrowserError as error:
            if navigating(error):
                return
            self.stopped = True
            self.password = None
            self.report('waiting', '自动填写未完成，请在浏览器窗口继续登录；不会重复提交密码。')


async def _login(config, output, report, *, credentials=None, expected_email=None):
    async with async_playwright() as driver:
        async with (
            browser_session(driver, headless=False, proxy_server=config.get('proxy', ''),
                            executable_path=config.get('browser_executable') or None) as context,
            email_inbox(driver, credentials, config, report) as inbox,
        ):
            page = await context.new_page()
            report('starting', '正在打开 YouTube，请稍候…')
            await page.goto('https://www.youtube.com/', wait_until='commit', timeout=60000)
            if expected_email:
                await page.goto('https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F',
                                wait_until='commit', timeout=60000)
            await page.bring_to_front()
            report('waiting', '请在独立浏览器中登录 YouTube，最多等待 10 分钟；登录成功后自动保存并关闭窗口。')
            print('YouTube 登录窗口已打开，请在网页中完成正常登录，最多等待 10 分钟。', flush=True)
            rejected = page.get_by_text(re.compile(
                r'此浏览器或应用可能不安全|This browser or app may not be secure', re.IGNORECASE)).first
            assistant = GoogleSignIn(credentials, report, inbox=inbox) if credentials else None
            credentials = None
            while True:
                try:
                    refused = await rejected.is_visible()
                except BrowserError as error:
                    if not navigating(error):
                        raise
                    await asyncio.sleep(0.2)
                    continue
                if refused:
                    raise RuntimeError('Google 拒绝自动化浏览器登录；请在正常浏览器手动登录并导出 YouTube 专用 Netscape Cookie。')
                if assistant:
                    await assistant.advance(page)
                if expected_email:
                    address = urlsplit(page.url)
                    if address.scheme != 'https' or address.hostname != 'www.youtube.com':
                        await asyncio.sleep(0.5)
                        continue
                try:
                    await page.locator('#avatar-btn img').first.wait_for(state='visible', timeout=2000)
                except BrowserTimeoutError:
                    continue
                # Freeze the session before exporting, as recommended by yt-dlp.
                report('saving', '已确认登录，正在保存 YouTube Cookie…')
                try:
                    account_name = await asyncio.wait_for(current_account_name(page), timeout=5)
                except asyncio.TimeoutError:
                    # Account labels are optional; a menu change must not lose a valid login.
                    account_name = None
                if expected_email and (not isinstance(account_name, str) or account_name.strip().lower() != expected_email.lower()):
                    raise RuntimeError('登录邮箱与所选账号不一致或无法确认邮箱，未保存或关联 Cookie，请重新登录。')
                await page.goto('https://www.youtube.com/robots.txt', wait_until='domcontentloaded', timeout=30000)
                cookies = await context.cookies(['https://www.youtube.com/'])
                save_youtube_cookies(cookies, output)
                print(f'YouTube 登录已确认，Netscape Cookie 已保存到：{output}', flush=True)
                return {'account_name': account_name}


def main(argv=None):
    parser = argparse.ArgumentParser(description='在独立浏览器登录 YouTube 并保存下载凭据')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', type=Path, default=ROOT / 'Cookies/youtube.txt')
    args = parser.parse_args(argv)
    configure_runtime()
    try:
        config, _ = load_config(args.config)
        asyncio.run(login(config, args.output.resolve()))
    except (OSError, ValueError, RuntimeError, BrowserError) as error:
        parser.exit(1, f'YouTube 登录未完成：{error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


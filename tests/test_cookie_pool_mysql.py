"""Run the Cookie protection contract on isolated, disposable MySQL databases."""
from tests.test_mysql import MySQLFixture
from tests import test_cookie_pool as contract
from webapp.cookie_profiles import CookieProfiles


class MySQLCookiePoolTests(MySQLFixture, contract.CookiePoolTests):
    def setUp(self):
        MySQLFixture.setUp(self)
        self.base = self.root
        self.store.fleet = None
        self.profiles = CookieProfiles(self.base, self.store)
        self.ids = [self.profiles.add(str(i), contract.COOKIE.replace(b'TEST_SECRET', str(i).encode()))['id']
                    for i in range(3)]
        self.config = {'download_method': 'yt_dlp', 'youtube_cookie_pool': self.ids,
                       'youtube_start_interval': 60, 'worker_count': 3}

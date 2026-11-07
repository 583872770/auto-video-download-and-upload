"""Optional ownership/race contract on disposable MySQL databases."""
from tests import test_cookie_binding as contract
from tests.test_mysql import MySQLFixture


class MySQLCookieBindingTests(MySQLFixture, contract.CookieBindingTests):
    def setUp(self):
        MySQLFixture.setUp(self)
        self.url = self.store.db.url
        self.setup_fleet()

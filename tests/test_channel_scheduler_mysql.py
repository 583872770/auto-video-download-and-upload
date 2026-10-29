"""Run the same admission contracts against an isolated MySQL database.

Uses VIDEO_TEST_MYSQL_URL with the existing test administrator fixture;
never point that variable at the application database.
"""
from tests import test_channel_scheduler as contract
from tests import test_mysql as mysql
from webapp.store import TaskStore


class MySQLChannelSchedulerTests(mysql.MySQLFixture, contract.AdmissionTests):
    def setUp(self):
        mysql.MySQLFixture.setUp(self)
        self.first = TaskStore(self.root / 'a', self.store.db.url)
        self.second = TaskStore(self.root / 'b', self.store.db.url)
        self.config = {'download_method':'bergung', 'worker_count':8,
            'download_channels':[contract.channel('bergung', transfer_limit=1),
                                 contract.channel('alias'), contract.channel('other','independent')]}

"""Actual installed Node/EJS, replaying a public player offline in release checks."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.extractor.youtube.jsc.provider import (JsChallengeRequest,JsChallengeType,NChallengeInput,SigChallengeInput)
from webapp.downloader import build_ydl_options
from webapp.parallel_http import ParallelYoutubeDL
from webapp.player_cache import PlayerCache


class ActualPlayerCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name=os.environ.get('VIDEO_TEST_PLAYER_FIXTURE')
        if not name:raise unittest.SkipTest('Requires offline public-player release fixture')
        directory=Path(name)
        metadata=json.loads((directory/'player.json').read_text())
        cls.url=metadata['url'];cls.source=(directory/'player.js').read_text()
        assert hashlib.sha256(cls.source.encode()).hexdigest()==metadata['sha256']

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name);self.loads=0

    def run_solver(self,identity,*,cache=True):
        config={'youtube_sleep_preset':False,'disk_min_free_gb':0,'js_runtimes':{'node':{'path':'/usr/local/bin/node'}}}
        if cache:config['_disk_state']=str(self.state)
        options=build_ydl_options(config,self.state)
        def source(*args,**kwargs):self.loads+=1;return self.source
        requests=[JsChallengeRequest(type=JsChallengeType.N,input=NChallengeInput(player_url=self.url,
            challenges=['fixture_value_123456','another_value_654321']),video_id=identity),
            JsChallengeRequest(type=JsChallengeType.SIG,input=SigChallengeInput(player_url=self.url,
                challenges=[''.join(map(chr,range(81)))]),video_id=identity)]
        with patch.object(YoutubeIE,'_download_webpage',source),ParallelYoutubeDL(options) as ydl:
            ie=ydl.get_info_extractor('Youtube');ie.initialize()
            results=ie._jsc_director.bulk_solve(requests)
            self.assertEqual(len(results),2)
            return {response.type.value:response.output.results for _,response in results}

    def test_production_cold_and_new_downloader_warm_outputs_match_uncached(self):
        original=self.run_solver('original',cache=False)
        first=self.run_solver('account-a');second=self.run_solver('account-b')
        self.assertEqual(original,first);self.assertEqual(first,second)
        self.assertEqual(self.loads,2)  # Original path plus one shared cold load.
        self.assertNotIn('fixture_value',next((self.state/'runtime/cache/youtube-players').glob('*.json')).read_text())

    def test_valid_hash_but_broken_prepared_code_rebuilds_with_actual_node(self):
        first=self.run_solver('cold')
        cache=PlayerCache(self.state,{'disk_min_free_gb':0})
        path=cache.path(self.url);value=json.loads(path.read_text())
        value['prepared']=cache.blob('throw new Error("invalid cached code");',compat=value['prepared']['compat'])
        path.write_text(json.dumps(value))
        second=self.run_solver('recovery')
        self.assertEqual(first,second);self.assertEqual(self.loads,2)


if __name__=='__main__':unittest.main()

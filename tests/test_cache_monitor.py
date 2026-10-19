"""Offline integration regressions: no user config, SSH, network or notifications."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ['APPLE_STOCK_WATCH_HOME'] = tempfile.mkdtemp(prefix='apple-public-unit-')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import apple_stock as m
import public_cache as c
NOW=1000000
CFG={'stores':['R609'],'cities':['大连'],'parts':{'MK2M4CH/A':'iPhone Duo'},
     'source':'public_cache','interval':60,'local_notify':False}
class Tests(unittest.TestCase):
    def run_watch_sequence(self, sequence, acknowledgements, part_count=1, push_delay=0):
        class End(Exception): pass
        clock = [float(NOW)]
        cfg = dict(CFG, bark='set', parts={str(i): 'Duo ' + str(i) for i in range(part_count)})
        steps = iter(sequence)
        starts = []
        def check(_):
            try: state, stamp = next(steps)
            except StopIteration: raise End
            starts.append(clock[0])
            return [{'number':'R609','name':'大连恒隆广场','city':'大连',
                     'parts':{part:{'state':state,'quote':'','title':title,
                                    'last_checked_ms':stamp,'source_cycle':stamp}
                              for part,title in cfg['parts'].items()}}]
        acks = iter(acknowledgements)
        def push(*args, **kwargs):
            clock[0] += push_delay
            return next(acks)
        def sleep(seconds): clock[0] += seconds
        m.state_path().unlink(missing_ok=True)
        with patch.object(m, 'check_once', side_effect=check), \
                patch.object(m.time, 'time', side_effect=lambda:clock[0]), \
                patch.object(m.time, 'monotonic', side_effect=lambda:clock[0]), \
                patch.object(m.time, 'sleep', side_effect=sleep), \
                patch.object(m, 'notify', side_effect=push) as send:
            with self.assertRaises(End): m.watch(cfg)
        return send, starts, m.load_state()

    def test_retry_survives_one_missing_observation(self):
        sent, _, state = self.run_watch_sequence(
            [('available',1),('unavailable',2),('available',3),('available',4)],
            [[],['Bark']])
        self.assertEqual(sent.call_count, 2)
        self.assertFalse(state['pairs']['R609|0']['alert_pending'])

    def test_repeated_cache_is_not_two_missing_observations(self):
        sent, _, state = self.run_watch_sequence(
            [('available',1),('unavailable',2),('unavailable',2)], [['Bark']])
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(state['pairs']['R609|0']['miss'], 1)
        sent, _, state = self.run_watch_sequence(
            [('available',1),('unavailable',2),('unavailable',2),('unavailable',3)],
            [['Bark'],['Bark']])
        self.assertEqual(sent.call_count, 2)
        self.assertFalse(state['pairs']['R609|0']['on'])

    def test_multiple_stock_alerts_one_push_and_sixty_second_poll(self):
        sent, starts, _ = self.run_watch_sequence(
            [('available',1),('available',2)], [[],[]], part_count=5, push_delay=15)
        self.assertEqual(sent.call_count, 2)  # One failed push per poll, not five.
        self.assertEqual(starts, [float(NOW), float(NOW+60)])

    def test_failed_cache_never_calls_apple(self):
        with patch.object(c,'query',side_effect=c.CacheError('offline')), \
             patch.object(m,'query_pickup',side_effect=AssertionError('Apple requested')):
            with self.assertRaises(m.QueryError): m.check_once(CFG)

    def test_init_supported_subset_without_apple_queries(self):
        args=argparse.Namespace(source='cache',city=['大连市'],store=None,family='iphone-duo',
            part=None,location=None,model=None,capacity=['256GB'],color=['夜空色'],
            interval=30,bark='',buy_url=None,no_local_notify=True)
        with patch.object(m,'query_pickup',side_effect=AssertionError('Apple inventory queried')), \
             patch.object(m,'fetch_catalog',side_effect=AssertionError('Apple catalog queried')):
            self.assertTrue(m.try_init_public_cache(args))
        cfg=m.load_config()
        self.assertEqual(cfg['source'],'public_cache'); self.assertEqual(cfg['interval'],60)
        self.assertEqual(set(cfg['parts']),{'MK2N4CH/A'})

    def test_bark_requires_json_success_and_hides_secret_errors(self):
        class Reply(io.BytesIO): status=200
        for code,want in [(200,['Bark']),(400,[])]:
            with patch.object(m.urllib.request,'urlopen',return_value=Reply(json.dumps({'code':code}).encode())):
                self.assertEqual(m.notify(dict(CFG,bark='https://example.invalid/key'),'test','test'),want)
        with patch.object(m.urllib.request,'urlopen',side_effect=OSError('DO_NOT_LOG_KEY')), \
             patch.object(m,'log') as log:
            self.assertEqual(m.notify(dict(CFG,bark='https://example.invalid/key'),'test','test'),[])
        self.assertNotIn('DO_NOT_LOG_KEY',str(log.call_args_list))

if __name__=='__main__': unittest.main()

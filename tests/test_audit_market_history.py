import json
import tempfile
import unittest
from pathlib import Path
from audit_market_history import audit

class AuditTests(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'history.json'
            p.write_text(json.dumps({'oi':[{'ts':3600000,'oiBtc':100.0},{'ts':7200000,'oiBtc':101.0}], 'candles':[{'ts':3600000,'open':1,'high':2,'low':1,'close':2,'volume':0,'confirm':'1'}]}))
            self.assertTrue(audit(p, now_ms=10800000)['structurally_valid'])
    def test_duplicate_oi(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'history.json'
            p.write_text(json.dumps({'oi':[{'ts':3600000,'oiBtc':100},{'ts':3600000,'oiBtc':100}], 'candles':[]}))
            self.assertFalse(audit(p, now_ms=10800000)['structurally_valid'])

if __name__ == '__main__': unittest.main()

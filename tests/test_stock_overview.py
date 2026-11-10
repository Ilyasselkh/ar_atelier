from datetime import datetime
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestStockOverview(TransactionCase):
    def test_carryover_and_missing_history(self):
        self.env['ar.atelier.of.list'].create({
            'name': 'OF-CARRYOVER', 'valid_from': '2026-11-09', 'valid_to': '2026-11-15',
        })
        session = self.env['ar.atelier.session'].create({})
        box = self.env['ar.atelier.box'].create({
            'session_id': session.id, 'section': 3, 'position': 0,
            'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 3).id,
            'datamatrix_raw': 'CARRYOVER', 'scan_datetime': '2026-11-08 09:00:00',
        })
        box.message_ids.write({'date': '2026-11-08 09:00:00'})
        Overview = self.env['ar.atelier.stock.overview'].with_context(tz='UTC')
        with patch('odoo.fields.Datetime.now', return_value=datetime(2026, 11, 11, 13)):
            result = Overview.get_weekly_counters()
        self.assertEqual(result['total'], 0)
        self.assertEqual([day['remaining'] for day in result['days'][:3]], [1, 1, 1])
        # Do not pretend an undated legacy history is an exact daily balance.
        box.message_ids.unlink()
        with patch('odoo.fields.Datetime.now', return_value=datetime(2026, 11, 11, 13)):
            result = Overview.get_weekly_counters()
        self.assertTrue(result['history_incomplete'])
        self.assertIsNone(result['days'][0]['remaining'])
        self.assertIsNone(result['treated'])
        self.assertEqual(result['days'][2]['remaining'], 1)

    def test_daily_anomaly_balances_and_menu_counts(self):
        self.env['ar.atelier.of.list'].create({
            'name': 'OF-DAILY-WEEK', 'valid_from': '2026-11-09', 'valid_to': '2026-11-15',
        })
        # Fixed UTC+1 avoids making this boundary test depend on DST tables.
        Overview = self.env['ar.atelier.stock.overview'].with_context(tz='Etc/GMT-1')
        Session = self.env['ar.atelier.session']
        session = Session.create({'mode': 'initial'})

        def create_box(target, section, date):
            record = self.env['ar.atelier.box'].create({
                'session_id': target.id, 'section': section, 'position': 0,
                'pallet_id': target.pallet_ids.filtered(lambda p: p.number == section).id,
                'datamatrix_raw': 'DAILY-BOX', 'scan_datetime': date,
            })
            record.message_ids.write({'date': date})
            return record

        def change(record, values, date):
            before = record.message_ids
            record.write(values)
            (record.message_ids - before).write({'date': date})

        boxes = self.env['ar.atelier.box']
        for _ in range(10):
            boxes |= create_box(session, 3, '2026-11-09 09:00:00')
        stock = session.pallet_ids.filtered(lambda p: p.number == 1)
        for record in boxes[:6]:
            change(record, {'section': 1, 'pallet_id': stock.id}, '2026-11-09 11:00:00')
        for record in boxes[6:8]:
            change(record, {'section': 1, 'pallet_id': stock.id}, '2026-11-10 11:00:00')
        # A failed attempt leaves the BOX in anomaly and must not count as treated.
        change(boxes[8], {'anomaly_reason_codes': 'qty_mismatch'}, '2026-11-10 12:00:00')
        for mode in ('post_tri', 'warehouse', 'warehouse_out'):
            other = Session.create({'mode': mode})
            # UTC Monday 23:30 belongs to Tuesday in the selected timezone.
            create_box(other, 2 if mode == 'warehouse_out' else 1, '2026-11-09 23:30:00')
        with patch('odoo.fields.Datetime.now', return_value=datetime(2026, 11, 11, 13)):
            result = Overview.get_weekly_counters()
        monday, tuesday, wednesday, thursday = result['days'][:4]
        self.assertEqual((monday['treated'], monday['remaining']), (6, 4))
        self.assertEqual((tuesday['treated'], tuesday['remaining']), (2, 2))
        self.assertEqual((wednesday['treated'], wednesday['remaining']), (0, 2))
        self.assertIsNone(thursday['remaining'])
        self.assertEqual(monday['initial'], 10)
        self.assertEqual(tuesday['post_tri'], 1)
        self.assertEqual(tuesday['warehouse'], 1)
        self.assertEqual(tuesday['warehouse_out'], 1)
        self.assertEqual(result['total'], 13)
        self.assertEqual(result['treated'], 8)
        self.assertFalse(result['history_incomplete'])
        # Cancellation closes an anomaly, but is not a successful correction.
        change(boxes[8], {'active': False}, '2026-11-11 10:00:00')
        with patch('odoo.fields.Datetime.now', return_value=datetime(2026, 11, 11, 13)):
            result = Overview.get_weekly_counters()
        self.assertEqual(result['days'][0]['remaining'], 4)
        self.assertEqual(result['days'][1]['remaining'], 2)
        self.assertEqual(result['days'][2]['remaining'], 1)
        self.assertEqual(result['days'][2]['treated'], 0)
        self.assertEqual(result['days'][2]['cancelled'], 1)
        self.assertEqual(result['anomalies'], 1)

    def test_counts_and_independent_access(self):
        viewer = new_test_user(
            self.env, login='atelier_stock_viewer',
            groups='base.group_user,ar_atelier.group_atelier_stock_overview',
        )
        operator = new_test_user(
            self.env, login='atelier_stock_operator',
            groups='base.group_user,ar_atelier.group_atelier_user',
        )
        Overview = self.env['ar.atelier.stock.overview'].with_user(viewer)
        with self.assertRaises(AccessError):
            self.env['ar.atelier.stock.overview'].with_user(operator).get_counters()
        with self.assertRaises(AccessError):
            self.env['ar.atelier.stock.overview'].with_user(operator).get_weekly_counters()
        self.assertFalse(Overview.get_weekly_counters()['period'])
        self.assertFalse(viewer.has_group('ar_atelier.group_atelier_user'))
        before = Overview.get_counters()
        boxes = self.env['ar.atelier.box']
        for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
            session = self.env['ar.atelier.session'].create({'mode': mode})
            for section in (3, 2 if mode == 'warehouse_out' else 1):
                boxes |= self.env['ar.atelier.box'].create({
                    'session_id': session.id,
                    'pallet_id': session.pallet_ids.filtered(lambda p: p.number == section).id,
                    'section': section, 'position': 0,
                    # Same BOX at different stages must count once per stage.
                    'datamatrix_raw': 'OVERVIEW-SAME-BOX-%s' % section,
                })
        after = Overview.get_counters()
        for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
            self.assertEqual(after[mode] - before[mode], 2)
        self.assertEqual(after['total'] - before['total'], 8)
        self.assertEqual(after['anomalies'] - before['anomalies'], 4)
        stock_box = boxes.filtered(lambda b: b.session_id.mode == 'initial' and b.section == 1)
        stock_box.write({'active': False})
        tri_anomaly = boxes.filtered(lambda b: b.session_id.mode == 'post_tri' and b.section == 3)
        tri_anomaly.session_id.write({'state': 'cancelled'})
        remaining = Overview.get_counters()
        self.assertEqual(remaining['total'] - before['total'], 5)
        self.assertEqual(remaining['anomalies'] - before['anomalies'], 3)
        # Correcting an anomaly changes the remaining count, not scan totals.
        warehouse_anomaly = boxes.filtered(lambda b: b.session_id.mode == 'warehouse' and b.section == 3)
        warehouse_anomaly.write({
            'section': 1,
            'pallet_id': warehouse_anomaly.session_id.pallet_ids.filtered(lambda p: p.number == 1).id,
        })
        corrected = Overview.get_counters()
        self.assertEqual(corrected['total'], remaining['total'])
        self.assertEqual(corrected['anomalies'], remaining['anomalies'] - 1)

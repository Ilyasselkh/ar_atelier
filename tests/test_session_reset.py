from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from ..models.gs1_parser import parse_label


@tagged('post_install', '-at_install')
class TestSessionReset(TransactionCase):
    def test_scan_rules_follow_latest_reset(self):
        self.env['ar.atelier.uc.ref'].create({'name': 'RESET-MAT', 'qty_per_carton': 10})
        today = fields.Date.today()
        self.env['ar.atelier.of.list'].create({
            'name': 'RESET-OF-LIST', 'valid_from': today, 'valid_to': today,
            'of_ids': [Command.create({'name': 'RESET-PO'})],
        })
        self.env['ar.atelier.tri.list'].create({
            'name': 'RESET-TRI-LIST', 'valid_from': today, 'valid_to': today,
            'ref_ids': [Command.create({'name': 'RESET-MAT'})],
        })
        Session = self.env['ar.atelier.session']
        Box = self.env['ar.atelier.box']
        code = '{MAT:RESET-MAT;QTY:10;PO:RESET-PO;HU:RESET-CYCLE}'

        def box(session, section, raw=code):
            return Box.create({
                'session_id': session.id,
                'pallet_id': session.pallet_ids.filtered(lambda p: p.number == section).id,
                'section': section, 'position': 0, 'datamatrix_raw': raw,
            })

        def analyze(mode, raw=code):
            return Session.new({'mode': mode})._analyze_scan(parse_label(raw))

        closed = Session.create({'state': 'done'})
        old = box(closed, 2)
        self.assertIn('duplicate_other_session', analyze('initial')[1])
        self.assertEqual(analyze('post_tri'), (1, []))
        self.assertEqual(analyze('warehouse_out'), (2, []))
        wizard = self.env['ar.atelier.reset'].create({})
        wizard.action_reset()
        # Even sessions closed before reset no longer satisfy scan rules.
        self.assertEqual(closed.state, 'done')
        self.assertTrue(old.exists())
        self.assertNotIn('duplicate_other_session', analyze('initial')[1])
        self.assertIn('not_in_tri', analyze('post_tri')[1])
        self.assertIn('warehouse_no_entry', analyze('warehouse')[1])
        self.assertIn('warehouse_out_no_tri', analyze('warehouse_out')[1])
        current = Session.create({'state': 'in_progress'})
        fresh = box(current, 2)
        self.assertIn('duplicate_other_session', analyze('initial')[1])
        self.assertEqual(analyze('post_tri'), (1, []))
        self.assertEqual(analyze('warehouse_out'), (2, []))
        anomalies = box(current, 3, 'RESET-BAD-A') | box(current, 3, 'RESET-BAD-B')
        wizard.action_reset()
        self.assertTrue(current.reset_pending)
        self.assertEqual(current.state, 'in_progress')
        self.assertIn('duplicate_other_session', analyze('initial')[1])
        self.assertEqual(analyze('post_tri'), (1, []))
        self.assertEqual(analyze('warehouse_out'), (2, []))
        anomalies[:1].write({'active': False})
        self.assertTrue(current.reset_pending)
        # Treating the last anomaly releases this session's old history.
        anomalies[1:].write({'section': 1, 'pallet_id': current.pallet_ids.filtered(lambda p: p.number == 1).id})
        self.assertFalse(current.reset_pending)
        self.assertNotIn('duplicate_other_session', analyze('initial')[1])
        self.assertIn('not_in_tri', analyze('post_tri')[1])
        self.assertIn('warehouse_out_no_tri', analyze('warehouse_out')[1])
        box(current, 3, 'RESET-NEW-ANOMALY')
        self.assertFalse(current.reset_pending)
        self.assertNotIn(fresh, current._period_boxes(parse_label(code)))
        # Scans after the boundary remain visible even in an old open session.
        new_box = box(current, 2)
        self.assertIn(new_box, current._period_boxes(parse_label(code)))
        self.assertNotIn(old, current._period_boxes(parse_label(code)))
        bad_qty = code.replace('QTY:10', 'QTY:9')
        self.assertIn('qty_mismatch', analyze('initial', bad_qty)[1])

    def test_partial_reset_and_history(self):
        actor = new_test_user(self.env, login='atelier_reset_actor',
                              groups='base.group_user,ar_atelier.group_atelier_reset')
        operator = new_test_user(self.env, login='atelier_reset_operator',
                                 groups='base.group_user,ar_atelier.group_atelier_user')
        Session = self.env['ar.atelier.session']
        clean = Session
        for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
            for state in ('draft', 'in_progress'):
                clean |= Session.create({'mode': mode, 'state': state, 'user_id': operator.id})
        blocked = Session.create({'state': 'in_progress', 'user_id': operator.id})
        anomaly = self.env['ar.atelier.box'].create({
            'session_id': blocked.id,
            'pallet_id': blocked.pallet_ids.filtered(lambda p: p.number == 3).id,
            'section': 3, 'position': 0, 'datamatrix_raw': 'RESET-ANOMALY',
        })
        closed = Session.create({'state': 'done'})
        cancelled = Session.create({'state': 'cancelled'})
        with self.assertRaises(AccessError):
            self.env['ar.atelier.reset'].with_user(operator).create({})
        wizard = self.env['ar.atelier.reset'].with_user(actor).create({})
        with self.assertRaises(AccessError):
            wizard.with_user(operator).action_reset()
        wizard.action_reset()
        self.assertEqual(set(clean.mapped('state')), {'reset'})
        self.assertTrue(all(clean.mapped('date_end')))
        self.assertEqual(blocked.state, 'in_progress')
        self.assertFalse(blocked.date_end)
        self.assertEqual(closed.state, 'done')
        self.assertEqual(cancelled.state, 'cancelled')
        self.assertIn(blocked.name, wizard.result)
        self.assertIn('relancer la réinitialisation', wizard.result)
        notes = clean[:1].message_ids.filtered(lambda m: 'terminée par réinitialisation' in str(m.body))
        self.assertEqual(notes[:1].author_id, actor.partner_id)
        self.assertEqual(wizard.action_history()['domain'], [('state', '=', 'reset')])
        with self.assertRaises(UserError):
            clean[:1].action_reset_to_draft()
        with self.assertRaises(UserError):
            clean[:1].action_done()
        self.assertFalse(clean[:1].with_user(operator).action_scan('START;123;Operator')['success'])
        first_dates = clean.mapped('date_end')
        anomaly.write({'active': False})
        wizard.action_reset()
        self.assertEqual(blocked.state, 'reset')
        self.assertTrue(anomaly.exists())
        self.assertEqual(clean.mapped('date_end'), first_dates)
        wizard.action_reset()
        self.assertIn('Aucune session non clôturée', wizard.result)

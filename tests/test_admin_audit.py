from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from unittest.mock import patch
from ..models.gs1_parser import parse_label
from datetime import timedelta


@tagged('post_install', '-at_install')
class TestAtelierAdminAudit(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(cls.env, login='atelier_audit_operator',
                                    groups='base.group_user,ar_atelier.group_atelier_user')
        cls.manager = new_test_user(cls.env, login='atelier_audit_manager',
                                   groups='base.group_user,ar_atelier.group_atelier_manager')
        cls.admin = new_test_user(cls.env, login='atelier_audit_admin',
                                 groups='base.group_user,base.group_system,ar_atelier.group_atelier_manager')
        cls.outsider = new_test_user(cls.env, login='atelier_audit_outsider', groups='base.group_user')
        cls.deleter = new_test_user(cls.env, login='atelier_audit_deleter',
                                   groups='base.group_user,ar_atelier.group_atelier_user,ar_atelier.group_atelier_delete')
        cls.uc = cls.env['ar.atelier.uc.ref'].with_user(cls.manager).create({
            'name': 'AUDIT-MAT', 'qty_per_carton': 10,
        })
        today = fields.Date.today()
        cls.tri = cls.env['ar.atelier.tri.list'].with_user(cls.manager).create({
            'name': 'AUDIT-TRI', 'valid_from': today, 'valid_to': today,
            'ref_ids': [Command.create({'name': 'AUDIT-MAT'})],
        })
        cls.of = cls.env['ar.atelier.of.list'].with_user(cls.manager).create({
            'name': 'AUDIT-OF', 'valid_from': today, 'valid_to': today,
            'of_ids': [Command.create({'name': 'AUDIT-PO'})],
        })

    def test_period_duplicates_and_cancellation(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        initial = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        initial.action_scan('START;123;Operator')
        code = '{MAT:AUDIT-MAT;QTY:10;PO:AUDIT-PO;HU:PERIOD}'
        self.assertEqual(initial.action_scan(code)['section'], 2)
        original = initial.box_ids
        self.assertEqual(initial.action_scan(code)['section'], 3)
        duplicate = initial.box_ids - original
        self.assertEqual(duplicate.duplicate_box_id, original)
        self.assertIn(initial.name, initial.get_box_detail(duplicate.id)['duplicate_message'])
        with self.assertRaises(AccessError):
            initial.with_user(self.outsider).cancel_duplicate_box(duplicate.id, 'Doublon vérifié')
        self.assertTrue(initial.cancel_duplicate_box(duplicate.id, 'Doublon vérifié')['success'])
        self.assertFalse(duplicate.active)
        self.assertTrue(original.active)
        notes = initial.message_ids.filtered(lambda m: 'BOX annulée' in str(m.body))
        self.assertTrue(notes)
        self.assertEqual(notes[:1].author_id, self.operator.partner_id)
        self.assertIn(code, str(notes[:1].body))
        post = Session.browse(Session.get_or_create_active(mode='post_tri', raw_code='START;123;Test Operator'))
        post.action_scan('START;123;Operator')
        self.assertEqual(post.action_scan(code)['section'], 1)
        self.assertEqual(post.action_scan(code)['section'], 3)
        another = self.env['ar.atelier.session'].with_user(self.manager).create({'mode': 'post_tri'})
        another.action_scan('START;123;Manager')
        self.assertEqual(another.action_scan(code)['section'], 3)
        self.assertEqual(another.box_ids.duplicate_box_id.session_id, post)
        # Records before the OF period must not block a new initial scan.
        original.sudo().write({'scan_datetime': fields.Datetime.now() - timedelta(days=2)})
        self.assertEqual(initial.action_scan(code)['section'], 2)

    def test_cancel_stock_and_tri_boxes(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        session.action_scan('START;123;Operator')
        for section in (1, 2):
            pallet = session.pallet_ids.filtered(lambda p: p.number == section)
            box = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': session.id, 'pallet_id': pallet.id,
                'section': section, 'position': 0, 'datamatrix_raw': 'CANCEL-%s' % section,
            })
            self.assertTrue(session.get_box_detail(box.id)['can_cancel_duplicate'])
            with self.assertRaises(UserError):
                session.cancel_duplicate_box(box.id, '   ')
            with self.assertRaises(AccessError):
                session.with_user(self.outsider).cancel_duplicate_box(box.id, 'Annulation vérifiée')
            self.assertTrue(session.cancel_duplicate_box(box.id, 'Annulation vérifiée')['success'])
            self.assertTrue(box.exists())
            self.assertFalse(box.active)
            self.assertEqual(pallet.box_count, 0)
            self.assertFalse(session.get_box_detail(box.id)['can_cancel_duplicate'])
            notes = box.message_ids.filtered(lambda m: 'BOX annulée' in str(m.body))
            self.assertTrue(notes)
            self.assertIn('Annulation vérifiée', str(notes[:1].body))
            self.assertEqual(notes[:1].author_id, self.operator.partner_id)
            with self.assertRaises(UserError):
                session.cancel_duplicate_box(box.id, 'Deuxième annulation')

    def test_cancel_all_anomalies(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        session.action_scan('START;123;Operator')
        for reason in ('unknown_ref', 'qty_mismatch', 'inactive_of', 'not_in_tri', 'unknown_format'):
            box = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': session.id,
                'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 3).id,
                'section': 3, 'position': 0, 'datamatrix_raw': 'CANCEL-' + reason,
                'anomaly_reason_codes': reason,
            })
            self.assertTrue(session.get_box_detail(box.id)['can_cancel_duplicate'])
            for empty in (None, '', '   '):
                with self.assertRaises(UserError):
                    session.cancel_duplicate_box(box.id, empty)
                self.assertTrue(box.active)
            other = self.env['ar.atelier.session'].with_user(self.manager).create({})
            with self.assertRaises(UserError):
                other.cancel_duplicate_box(box.id, 'Étiquette vérifiée <test>')
            self.assertTrue(session.cancel_duplicate_box(box.id, 'Étiquette vérifiée <test>')['success'])
            self.assertTrue(box.exists())
            self.assertFalse(box.active)
            self.assertFalse(session.get_box_detail(box.id)['can_cancel_duplicate'])
            notes = session.message_ids.filtered(lambda m: 'BOX annulée' in str(m.body) and box.datamatrix_raw in str(m.body))
            self.assertTrue(notes)
            self.assertIn(box.anomaly_labels, str(notes[:1].body))
            self.assertIn('Étiquette vérifiée &lt;test&gt;', str(notes[:1].body))
            self.assertEqual(notes[:1].author_id, self.operator.partner_id)
            with self.assertRaises(UserError):
                session.cancel_duplicate_box(box.id, 'Étiquette vérifiée <test>')

    def test_warehouse_scan_paths(self):
        self.uc.write({'name': '284704000', 'qty_per_carton': 600})
        self.of.of_ids.write({'name': '15402750'})
        self.tri.ref_ids.write({'name': 'OTHER'})
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        initial = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        post = Session.browse(Session.get_or_create_active(mode='post_tri', raw_code='START;123;Test Operator'))
        warehouse = Session.browse(Session.get_or_create_active(mode='warehouse', raw_code='START;123;Test Operator'))
        self.assertEqual(set(warehouse.pallet_ids.mapped('number')), {1, 3})
        for session in (initial, post, warehouse):
            session.action_scan('START;123;Operator')
        for index, code in enumerate(('{MAT:284704000;QTY:600;PO:15402750;HU:WH}',
                                      '284704000BXG000021B0000600154027501030+')):
            self.tri.ref_ids.write({'name': 'OTHER'})
            self.assertEqual(initial.action_scan(code)['section'], 1)
            self.assertEqual(warehouse.action_scan(code)['section'], 1)
            self.assertEqual(warehouse.action_scan(code)['section'], 3)
            duplicate = warehouse.box_ids.filtered(lambda b: b.datamatrix_raw == code and b.section == 3)
            self.assertTrue(duplicate.duplicate_box_id)
            self.assertTrue(warehouse.cancel_duplicate_box(duplicate.id, 'Doublon magasin vérifié')['success'])
            tri_code = code.replace('HU:WH', 'HU:WH-TRI').replace('1030+', '1031+')
            self.tri.ref_ids.write({'name': '284704000'})
            self.assertEqual(initial.action_scan(tri_code)['section'], 2)
            result = warehouse.action_scan(tri_code)
            self.assertIn('warehouse_still_in_tri', result['anomaly_reasons'])
            waiting = warehouse.box_ids.filtered(lambda b: b.datamatrix_raw == tri_code)
            warehouse.cancel_duplicate_box(waiting.id, 'BOX renvoyée au tri')
            self.assertEqual(post.action_scan(tri_code)['section'], 1)
            self.assertEqual(warehouse.action_scan(tri_code)['section'], 1)
        missing = '{MAT:284704000;QTY:600;PO:15402750;HU:MISSING}'
        self.assertIn('warehouse_no_entry', warehouse.action_scan(missing)['anomaly_reasons'])
        bad = '{MAT:284704000;QTY:599;PO:15402750;HU:BAD}'
        self.assertEqual(initial.action_scan(bad)['section'], 3)
        result = warehouse.action_scan(bad)
        self.assertIn('warehouse_entry_anomaly', result['anomaly_reasons'])
        self.assertIn('qty_mismatch', result['anomaly_reasons'])
        old = '{MAT:284704000;QTY:600;PO:15402750;HU:OLD}'
        initial.action_scan(old)
        initial.box_ids.filtered(lambda b: b.datamatrix_raw == old).sudo().write({
            'scan_datetime': fields.Datetime.now() - timedelta(days=2)})
        self.assertIn('warehouse_no_entry', warehouse.action_scan(old)['anomaly_reasons'])
        cancelled = '{MAT:284704000;QTY:599;PO:15402750;HU:CANCELLED}'
        initial.action_scan(cancelled)
        box = initial.box_ids.filtered(lambda b: b.datamatrix_raw == cancelled)
        initial.cancel_duplicate_box(box.id, 'Entrée annulée')
        self.assertIn('warehouse_no_entry', warehouse.action_scan(cancelled)['anomaly_reasons'])

    def test_resume_specific_session(self):
        for mode, tag in [('initial', 'ar_atelier_scan_app'),
                          ('post_tri', 'ar_atelier_scan_app_post_tri'),
                          ('warehouse', 'ar_atelier_scan_app_warehouse')]:
            session = self.env['ar.atelier.session'].with_user(self.manager).create({
                'mode': mode, 'user_id': self.operator.id})
            with self.assertRaises(UserError):
                session.with_user(self.operator).action_resume_scan()
            session.action_start()
            session.action_done()
            session.action_reset_to_draft()
            newer = self.env['ar.atelier.session'].with_user(self.manager).create({
                'mode': mode, 'user_id': self.operator.id})
            newer.action_start()
            session.action_start()
            count = self.env['ar.atelier.session'].search_count([])
            action = session.with_user(self.operator).action_resume_scan()
            self.assertEqual(action['tag'], tag)
            self.assertEqual(action['params'], {'session_id': session.id, 'mode': mode})
            self.assertNotEqual(action['params']['session_id'], newer.id)
            self.assertEqual(self.env['ar.atelier.session'].search_count([]), count)
            with self.assertRaises(AccessError):
                session.with_user(self.outsider).action_resume_scan()
            newer.user_id = self.manager
            with self.assertRaises(AccessError):
                newer.with_user(self.operator).action_resume_scan()
            self.assertEqual(newer.with_user(self.manager).action_resume_scan()['params']['session_id'], newer.id)
            session.action_done()
            with self.assertRaises(UserError):
                session.with_user(self.operator).action_resume_scan()

    def test_idle_closure_and_activity(self):
        now = fields.Datetime.now()
        for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
            session = self.env['ar.atelier.session'].with_user(self.manager).create({
                'mode': mode, 'user_id': self.operator.id})
            session.action_start()
            session.write({'last_activity_at': now - timedelta(minutes=6)})
            session.with_user(self.operator).check_inactivity()
            self.env['ar.atelier.session']._cron_close_idle_sessions()
            self.assertEqual(session.state, 'in_progress')
            session.with_user(self.operator).action_scan('START;123;Operator')
            if mode == 'warehouse_out':
                session.with_user(self.operator).save_warehouse_out_reason('Sortie pour tri')
            self.assertGreaterEqual(session.last_activity_at, now)
            session.write({'last_activity_at': now - timedelta(minutes=4)})
            session.with_user(self.operator).check_inactivity()
            self.assertEqual(session.state, 'in_progress')
            session.write({'last_activity_at': now - timedelta(minutes=6)})
            box = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': session.id,
                'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 3).id,
                'section': 3, 'position': 0, 'datamatrix_raw': 'IDLE-' + mode,
                'anomaly_reason_codes': 'unknown_ref'})
            self.env['ar.atelier.session']._cron_close_idle_sessions()
            self.assertEqual(session.state, 'in_progress')
            session.with_user(self.operator).cancel_duplicate_box(box.id, 'Anomalie traitée')
            self.assertGreaterEqual(session.last_activity_at, now)
            session.with_user(self.operator).check_inactivity()
            self.assertEqual(session.state, 'in_progress')
            session.write({'last_activity_at': now - timedelta(minutes=6)})
            session.with_user(self.operator).record_activity()
            self.assertGreaterEqual(session.last_activity_at, now)
            with self.assertRaises(AccessError):
                session.with_user(self.outsider).record_activity()
            session.write({'last_activity_at': now - timedelta(minutes=6)})
            session.with_user(self.operator).action_scan('INVALID')
            self.assertGreaterEqual(session.last_activity_at, now)
            session.write({'last_activity_at': now - timedelta(minutes=6)})
            self.env['ar.atelier.session']._cron_close_idle_sessions()
            self.assertEqual(session.state, 'done')
            self.assertTrue(session.date_end)
            notes = session.message_ids.filtered(lambda m: 'Clôture automatique' in str(m.body))
            self.assertEqual(len(notes), 1)
            self.env['ar.atelier.session']._cron_close_idle_sessions()
            self.assertEqual(len(session.message_ids.filtered(lambda m: 'Clôture automatique' in str(m.body))), 1)

    def test_warehouse_out_without_of_validity(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        target = Session.browse(Session.get_or_create_active(mode='warehouse_out', raw_code='START;123;Test Operator'))
        self.assertEqual(set(target.pallet_ids.mapped('number')), {2, 3})
        target.action_scan('START;123;Operator')
        origin = self.env['ar.atelier.session'].with_user(self.manager).create({})
        post = self.env['ar.atelier.session'].with_user(self.manager).create({'mode': 'post_tri'})
        self.of.write({'active': False})
        self.tri.write({'active': False})
        self.uc.write({'active': False})
        self.assertFalse(Session.get_referential_status(mode='warehouse_out')['missing'])
        self.assertTrue(Session.get_referential_status(mode='initial')['missing'])
        codes = ['{MAT:AUDIT-MAT;QTY:10;PO:OLD;HU:OUT}',
                 '284704000BXG000021B0000600154027501030+']
        for code in codes:
            entry = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': origin.id, 'pallet_id': origin.pallet_ids.filtered(lambda p: p.number == 2).id,
                'section': 2, 'position': 0, 'datamatrix_raw': code,
                'scan_datetime': fields.Datetime.now() - timedelta(days=30),
            })
            self.assertEqual(target.action_scan(code)['section'], 2)
            exit_box = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': post.id, 'pallet_id': post.pallet_ids.filtered(lambda p: p.number == 1).id,
                'section': 1, 'position': 0, 'datamatrix_raw': code,
            })
            self.assertIn('warehouse_out_in_stock', target.action_scan(code)['anomaly_reasons'])
            exit_box.sudo().write({'active': False})
            self.assertEqual(target.action_scan(code)['section'], 2)
            entry.sudo().write({'active': False})
            self.assertIn('warehouse_out_no_tri', target.action_scan(code)['anomaly_reasons'])
        self.assertIn('warehouse_out_no_tri', target.action_scan('{MAT:UNKNOWN;HU:NEW}')['anomaly_reasons'])
        self.assertEqual(target.action_resume_scan()['tag'], 'ar_atelier_scan_app_warehouse_out')

    def test_warehouse_out_reason_required_for_closure(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session = Session.browse(Session.get_or_create_active(mode='warehouse_out', raw_code='START;123;Operator'))
        self.assertTrue(session.action_close_and_new()['reason_required'])
        self.assertTrue(session.action_scan('END;SCAN')['reason_required'])
        with self.assertRaises(UserError):
            session.with_user(self.manager).action_done()
        session.sudo().write({'last_activity_at': fields.Datetime.now() - timedelta(minutes=6)})
        session.check_inactivity()
        self.assertEqual(session.state, 'in_progress')
        session.save_warehouse_out_reason('   ')
        self.assertTrue(session.action_close_and_new()['reason_required'])
        with self.assertRaises(AccessError):
            session.with_user(self.outsider).save_warehouse_out_reason('Test')
        session.save_warehouse_out_reason('Retour atelier pour tri')
        self.assertEqual(session.get_pallet_state()['warehouse_out_reason'], 'Retour atelier pour tri')
        session.sudo().write({'last_activity_at': fields.Datetime.now() - timedelta(minutes=6)})
        session.check_inactivity()
        self.assertEqual(session.state, 'done')

    def test_configurable_closure_duration(self):
        Settings = self.env['ar.atelier.closure.settings']
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        self.assertEqual(Session._get_idle_timeout_minutes(), 5)
        with self.assertRaises(AccessError):
            Settings.with_user(self.operator).create({'minutes': 3})
        from odoo.exceptions import ValidationError
        for invalid in (0, -1):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                Settings.with_user(self.admin).create({'minutes': invalid})
        for minutes in (3, 10):
            settings = Settings.with_user(self.admin).create({'minutes': minutes})
            settings.action_save()
            self.assertEqual(Settings.with_user(self.admin).default_get(['minutes'])['minutes'], minutes)
            for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
                session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;123;Operator'))
                if mode == 'warehouse_out':
                    session.save_warehouse_out_reason('Retour tri')
                session.sudo().write({'last_activity_at': fields.Datetime.now() - timedelta(minutes=minutes - 1)})
                session.check_inactivity()
                self.assertEqual(session.state, 'in_progress')
                session.sudo().write({'last_activity_at': fields.Datetime.now() - timedelta(minutes=minutes + 1)})
                self.env['ar.atelier.session']._cron_close_idle_sessions()
                self.assertEqual(session.state, 'done')

    def test_barcode_parser(self):
        code = '284704000BXG000021B0000600154027501030+'
        self.assertEqual(parse_label(code), {
            'raw': code, '_format': 'barcode', 'mat': '284704000',
            'bat': 'BXG000021B', 'qty': 600, 'po': '15402750', 'hu': '1030+',
        })
        self.assertEqual(parse_label('{MAT:284704000;QTY:600;PO:15402750}')['qty'], 600)
        for invalid in ('', code[:-1], code[:20], code.replace('0000600', '000X600')):
            self.assertIsNone(parse_label(invalid))

    def test_barcode_scan_routing_duplicates_correction_and_post_tri(self):
        self.uc.write({'name': '284704000', 'qty_per_carton': 600})
        self.of.of_ids.write({'name': '15402750'})
        self.tri.ref_ids.write({'name': '284704000'})
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        session.action_scan('START;123;Barcode Operator')
        code = '284704000BXG000021B0000600154027501030+'
        self.assertTrue(session.action_scan(code)['success'])
        box = session.box_ids
        self.assertEqual((box.mat, box.bat, box.qty, box.po, box.hu, box.label_format),
                         ('284704000', 'BXG000021B', 600, '15402750', '1030+', 'barcode'))
        self.assertEqual(box.section, 2)
        self.assertEqual(session.action_scan(code)['section'], 3)
        post = Session.browse(Session.get_or_create_active(mode='post_tri', raw_code='START;123;Test Operator'))
        post.action_scan('START;123;Barcode Operator')
        self.assertTrue(post.action_scan(code)['success'])
        self.assertEqual(post.box_ids.section, 1)
        self.assertTrue(post.action_scan(code.replace('1030+', '1031+'))['success'])
        self.assertIn('not_in_tri', post.box_ids.filtered(lambda b: b.hu == '1031+').anomaly_reason_codes)
        other = self.env['ar.atelier.session'].with_user(self.manager).create({})
        other.action_scan('START;123;Barcode Operator')
        self.assertTrue(other.action_scan(code)['success'])
        self.assertIn('duplicate_other_session', other.box_ids.anomaly_reason_codes)
        # Same HU, different full content: no duplicate; quantity validation still applies.
        wrong_qty = code.replace('0000600', '0000599')
        self.assertTrue(session.action_scan(wrong_qty)['success'])
        anomaly = session.box_ids.filtered(lambda b: b.datamatrix_raw == wrong_qty)
        self.assertEqual(anomaly.anomaly_reason_codes, 'qty_mismatch')
        self.tri.ref_ids.write({'name': 'OTHER-REFERENCE'})
        corrected = code.replace('1030+', '1032+')
        self.assertTrue(Session.with_user(self.manager).correct_box(anomaly.id, corrected, 'Quantité corrigée')['success'])
        self.assertEqual((anomaly.section, anomaly.qty, anomaly.label_format), (1, 600, 'barcode'))
        qr = '{MAT:284704000;QTY:600;PO:15402750;HU:QR-1}'
        self.assertTrue(session.action_scan(qr)['success'])
        self.assertEqual(session.box_ids.filtered(lambda b: b.hu == 'QR-1').section, 1)
        count = len(session.box_ids)
        self.assertFalse(session.action_scan('invalid barcode')['success'])
        self.assertEqual(len(session.box_ids), count)

    def test_chatter_formatting_and_html_escaping(self):
        body = str(self.uc._audit_body('write', {
            'qty_per_carton': {'avant': 0, 'après': 60},
            'description': {'avant': False, 'après': '<script>alert(1)</script>'},
        }))
        self.assertIn('Quantité par carton', body)
        self.assertIn('0 → 60', body)
        self.assertIn('Non renseigné', body)
        self.assertNotIn('<script>', body)
        self.assertNotIn('<pre>', body)
        body = str(self.uc._audit_body('create', {'active': False, 'description': False}))
        self.assertIn('Non', body)
        self.assertNotIn('Description', body)

    def test_correction_keeps_before_after_and_changed_fields(self):
        Session = self.env['ar.atelier.session'].with_user(self.manager)
        session = Session.create({})
        old_code = '{MAT:UNKNOWN;QTY:10;PO:OLD}'
        box = self.env['ar.atelier.box'].with_user(self.manager).create({
            'session_id': session.id,
            'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 3).id,
            'section': 3, 'position': 0, 'datamatrix_raw': old_code,
            'mat': 'UNKNOWN', 'qty': 10, 'po': 'OLD',
            'anomaly_reason_codes': 'unknown_ref,inactive_of',
        })
        with patch.object(type(session), '_analyze_scan', return_value=(3, ['unknown_ref'])):
            self.assertTrue(Session.correct_box(box.id, old_code, 'OF activé puis étiquette revérifiée')['success'])
        first = box.correction_ids[:1]
        snapshot = first.history_snapshot
        self.assertTrue(snapshot['same_label'])
        self.assertIn('OF non active', snapshot['resolved'])
        self.assertEqual(snapshot['after'], ['Boxe sans UC (réf inconnue)'])
        self.assertEqual(snapshot['changes'], [])
        with patch.object(type(session), '_analyze_scan', return_value=(1, [])):
            self.assertTrue(Session.correct_box(box.id, old_code.replace('PO:OLD', 'PO:NEW'), 'OF remplacé')['success'])
        latest = (box.correction_ids - first).history_snapshot
        self.assertFalse(latest['same_label'])
        self.assertEqual(latest['after'], [])
        self.assertIn({'field': 'po', 'label': 'N° OF (PO)', 'before': 'OLD', 'after': 'NEW'}, latest['changes'])
        self.assertEqual(first.history_snapshot, snapshot)
        detail = Session.get_box_detail(box.id)
        self.assertEqual(len(detail['corrections']), 2)
        self.assertTrue(all(c['summary'] for c in detail['corrections']))

    def test_operator_read_only_and_manager_edit(self):
        for record, changes in [(self.uc, {'qty_per_carton': 20}),
                                (self.tri, {'name': 'Updated TRI'}),
                                (self.of, {'name': 'Updated OF'}),
                                (self.tri.ref_ids, {'description': 'Updated line'}),
                                (self.of.of_ids, {'description': 'Updated line'})]:
            with self.assertRaises(AccessError):
                record.with_user(self.operator).write(changes)
            record.with_user(self.manager).write(changes)
            with self.assertRaises(AccessError):
                record.with_user(self.manager).unlink()
        messages = self.uc.message_ids.filtered(lambda m: 'Quantité par carton' in str(m.body) and 'Modification' in str(m.body))
        self.assertTrue(messages)
        self.assertEqual(messages[:1].author_id, self.manager.partner_id)
        for value in ('Quantité par carton', '10', '→', '20'):
            self.assertIn(value, str(messages[:1].body))

    def test_deletion_requires_separate_group(self):
        for user in (self.operator, self.manager, self.outsider):
            with self.assertRaises(AccessError):
                self.uc.with_user(user).unlink()
        with self.assertRaises(AccessError):
            self.uc.with_user(self.deleter).write({'qty_per_carton': 20})
        self.uc.with_user(self.deleter).unlink()
        self.assertFalse(self.uc.exists())
        self.tri.with_user(self.deleter).unlink()
        self.of.with_user(self.deleter).unlink()
        self.assertFalse(self.tri.exists())
        self.assertFalse(self.of.exists())

    def test_odoo_admin_can_delete_without_atelier_groups(self):
        admin = new_test_user(self.env, login='atelier_system_only',
                              groups='base.group_user,base.group_system')
        self.assertFalse(admin.has_group('ar_atelier.group_atelier_delete'))
        # Inline trash uses a write on the parent and deletion of its child.
        line = self.of.of_ids
        self.of.with_user(admin).write({'of_ids': [Command.delete(line.id)]})
        self.assertFalse(line.exists())
        notes = self.of.message_ids.filtered(lambda m: 'Suppression' in str(m.body))
        self.assertTrue(notes)
        self.assertEqual(notes[:1].author_id, admin.partner_id)
        for record in (self.of, self.tri, self.uc):
            record.with_user(admin).unlink()
            self.assertFalse(record.exists())
        session = self.env['ar.atelier.session'].with_user(admin).create({})
        pallets = session.pallet_ids
        session.with_user(admin).unlink()
        self.assertFalse(session.exists())
        self.assertFalse(pallets.exists())

    def test_operator_scan_both_modes_without_direct_write(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        for mode in ('initial', 'post_tri'):
            session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;123;Test Operator'))
            self.assertEqual(session.user_id, self.operator)
            with self.assertRaises(AccessError):
                session.write({'operator_name': 'Manual change'})
            with self.assertRaises(AccessError):
                session.pallet_ids[:1].write({'capacity': 999})
            self.assertTrue(session.action_scan('START;123;Test Operator')['success'])
            result = session.action_scan('{MAT:AUDIT-MAT;QTY:10;PO:AUDIT-PO;HU:AUDIT-%s}' % mode)
            self.assertTrue(result['success'])
            box = session.box_ids[:1]
            self.assertEqual(box.user_id, self.operator)
            with self.assertRaises(AccessError):
                box.write({'qty': 50})
            with self.assertRaises(AccessError):
                box.with_user(self.manager).write({'active': False})
            messages = box.message_ids.filtered(lambda m: 'Étiquette scannée' in str(m.body))
            self.assertTrue(messages)
            self.assertEqual(messages[:1].author_id, self.operator.partner_id)
            if box.section == 3:
                box.with_user(self.manager).write({
                    'section': 1,
                    'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 1).id,
                })
            new_id = session.action_close_and_new()
            self.assertFalse(new_id)

    def test_closure_blocked_until_active_anomalies_resolved(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        for mode in ('initial', 'post_tri'):
            session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;123;Test Operator'))
            self.assertTrue(session.action_scan('START;123;Test Operator')['success'])
            box = self.env['ar.atelier.box'].with_user(self.manager).create({
                'session_id': session.id,
                'pallet_id': session.pallet_ids.filtered(lambda p: p.number == 3).id,
                'section': 3, 'position': 0, 'datamatrix_raw': 'ANOMALY-' + mode,
            })
            session_count = Session.search_count([])
            for result in (session.action_close_and_new(), session.action_scan('END;SCAN'),
                           session.action_scan('START;123;Another operator')):
                self.assertTrue(result['closure_blocked'])
                self.assertEqual(result['anomaly_count'], 1)
            self.assertEqual(session.state, 'in_progress')
            self.assertFalse(session.date_end)
            self.assertEqual(Session.search_count([]), session_count)
            with self.assertRaises(UserError):
                session.with_user(self.manager).action_done()
            with self.assertRaises(UserError):
                session.with_user(self.manager).write({'state': 'done'})
            box.with_user(self.admin).write({'active': False})
            new_id = session.action_close_and_new()
            self.assertFalse(new_id)
            self.assertEqual(session.state, 'done')
            self.assertFalse(new_id)

    def test_identification_required_and_reset_after_closure(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        for mode in ('initial', 'post_tri', 'warehouse', 'warehouse_out'):
            count = Session.search_count([])
            self.assertFalse(Session.get_or_create_active(mode=mode))
            self.assertEqual(Session.search_count([]), count)
            for code in ('START;;', '{MAT:AUDIT-MAT;QTY:10}', 'END;SCAN'):
                with self.assertRaises(UserError):
                    Session.get_or_create_active(mode=mode, raw_code=code)
                self.assertEqual(Session.search_count([]), count)
            session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;123;Operator'))
            self.assertEqual(session.state, 'in_progress')
            self.assertEqual(session.operator_code, '123')
            self.assertEqual(Session.get_or_create_active(mode=mode), session.id)
            if mode == 'warehouse_out':
                session.save_warehouse_out_reason('Sortie pour tri')
            self.assertFalse(session.action_close_and_new())
            self.assertEqual(session.state, 'done')
            self.assertFalse(Session.get_or_create_active(mode=mode))
            self.assertEqual(Session.search_count([]), count + 1)
            next_session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;456;Other'))
            self.assertNotEqual(next_session.id, session.id)
            self.assertEqual(next_session.operator_code, '456')
            if mode == 'warehouse_out':
                next_session.save_warehouse_out_reason('Sortie pour tri')
            self.assertTrue(next_session.action_scan('END;SCAN')['success'])
            self.assertFalse(Session.get_or_create_active(mode=mode))

    def test_identification_allowed_without_referentials(self):
        self.uc.with_user(self.manager).write({'active': False})
        self.tri.with_user(self.manager).write({'active': False})
        self.of.with_user(self.manager).write({'active': False})
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        for mode in ('initial', 'post_tri'):
            session = Session.browse(Session.get_or_create_active(mode=mode, raw_code='START;123;Test Operator'))
            self.assertTrue(session.action_scan('START;123;Test Operator')['success'])
            self.assertEqual(session.operator_code, '123')
            self.assertFalse(session.box_ids)

    def test_scan_rejects_other_user_and_untrusted_defaults(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session_id = Session.with_context(default_user_id=self.manager.id,
                                         default_operator_name='Injected', tracking_disable=True).get_or_create_active(raw_code='START;123;Test Operator')
        session = Session.browse(session_id)
        self.assertEqual(session.user_id, self.operator)
        self.assertEqual(session.operator_name, 'Test Operator')
        foreign = self.env['ar.atelier.session'].with_user(self.manager).create({})
        with self.assertRaises(AccessError):
            foreign.with_user(self.operator).action_scan('START;123;Other user')
        with self.assertRaises(AccessError):
            foreign.with_user(self.operator).action_close_and_new()
        with self.assertRaises(AccessError):
            Session.with_user(self.outsider).get_or_create_active(raw_code='START;123;Test Operator')

    def test_line_changes_and_deletion_tracked_in_parent_chatter(self):
        line = self.tri.ref_ids
        messages = self.tri.message_ids
        line.with_user(self.manager).write({'description': 'Trace in parent'})
        self.assertTrue(self.tri.message_ids - messages)
        messages = self.tri.message_ids
        line.with_user(self.deleter).unlink()
        self.assertFalse(line.exists())
        deletion = (self.tri.message_ids - messages).filtered(lambda m: 'Suppression' in str(m.body))
        self.assertTrue(deletion)
        self.assertEqual(deletion[:1].author_id, self.deleter.partner_id)
        self.assertIn('Trace in parent', str(deletion[:1].body))

    def test_archived_boxes_and_chatter_removed_on_session_delete(self):
        Session = self.env['ar.atelier.session'].with_user(self.operator)
        session = Session.browse(Session.get_or_create_active(raw_code='START;123;Test Operator'))
        self.assertTrue(session.action_scan('START;123;Test Operator')['success'])
        self.assertTrue(session.action_scan('{MAT:AUDIT-MAT;QTY:10;PO:AUDIT-PO;HU:CASCADE}')['success'])
        box = session.box_ids[:1]
        box.with_user(self.admin).write({'active': False})
        box_id = box.id
        session.with_user(self.deleter).unlink()
        self.assertFalse(box.exists())
        self.assertFalse(self.env['mail.message'].search([
            ('model', '=', 'ar.atelier.box'), ('res_id', '=', box_id),
        ]))

from collections import defaultdict
from datetime import datetime, time, timedelta

from lxml import html
import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class AtelierStockOverview(models.AbstractModel):
    _name = 'ar.atelier.stock.overview'
    _description = 'Suivi du stock Atelier'

    @api.model
    def _anomaly_timeline(self, boxes):
        """Replay dated box audit notes; never date a resolution from write_date.

        Audit notes contain the state before/after a write, so later corrections
        cannot erase a previous day's remaining anomalies.
        """
        changes = defaultdict(list)
        initial = defaultdict(dict)
        labels = {'Section': 'section', 'Actif': 'active', 'Active': 'active'}
        for lang in (False, self.env.lang):
            metadata = boxes.with_context(lang=lang).fields_get(['section', 'active'])
            labels.update({meta['string']: name for name, meta in metadata.items()})
        messages = self.env['mail.message'].sudo().search([
            ('model', '=', 'ar.atelier.box'), ('res_id', 'in', boxes.ids),
        ], order='date, id')
        for message in messages:
            tree = html.fragment_fromstring(str(message.body or '<div/>'), create_parent='div')
            updates = {}
            for item in tree.xpath('./ul/li'):
                strong = item.find('strong')
                if strong is None:
                    continue
                label = ''.join(strong.itertext()).strip().rstrip(':').strip()
                name = labels.get(label)
                if not name:
                    continue
                value = (strong.tail or '').strip()
                parts = value.split(' → ', 1)

                def decode(text):
                    if name == 'section':
                        return int(text) if text in ('1', '2', '3') else None
                    if text.lower() in ('oui', 'yes', 'true'):
                        return True
                    if text.lower() in ('non', 'no', 'false'):
                        return False
                    return None

                before = decode(parts[0])
                if before is None:
                    continue
                initial[message.res_id].setdefault(name, before)
                if len(parts) == 2:
                    after = decode(parts[1])
                    if after is not None:
                        updates[name] = after
            if updates:
                changes[message.res_id].append((message.date, message.id, updates))
        events, unknown = [], []
        for box in boxes:
            snapshot = initial[box.id]
            if 'section' not in snapshot:
                unknown.append(box.scan_datetime)
                continue
            section, active = snapshot['section'], snapshot.get('active', True)
            if active and section == 3:
                events.append((box.scan_datetime, 1, 0, 0))
            for date, _, update in changes[box.id]:
                was_open = active and section == 3
                section, active = update.get('section', section), update.get('active', active)
                is_open = active and section == 3
                if was_open != is_open:
                    events.append((date, int(is_open) - int(was_open),
                                   int(was_open and not is_open and active),
                                   int(was_open and not active)))
        return sorted(events), unknown

    @api.model
    def get_weekly_counters(self):
        # Keep the same independent permission as the global counters.
        if not self.env.su and not self.env.user.has_group('ar_atelier.group_atelier_stock_overview'):
            raise AccessError(_("Vous n'avez pas accès au suivi du stock Atelier."))
        now = fields.Datetime.now()
        tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'Africa/Casablanca')
        today = pytz.UTC.localize(now).astimezone(tz).date()
        of_list = self.env['ar.atelier.of.list'].sudo().get_active_list(target_date=today)
        counts = dict.fromkeys(('initial', 'post_tri', 'warehouse', 'warehouse_out'), 0)
        result = {**counts, 'total': 0, 'anomalies': 0, 'treated': 0, 'cancelled': 0,
                  'days': [], 'period': False, 'history_incomplete': False}
        if not of_list:
            return result
        start, end = of_list.valid_from, of_list.valid_to
        result['period'] = {'name': of_list.name, 'start': str(start), 'end': str(end),
                            'week': start.isocalendar().week, 'timezone': str(tz)}

        def utc_midnight(day):
            return tz.localize(datetime.combine(day, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)

        Box = self.env['ar.atelier.box'].sudo().with_context(active_test=False)
        boxes = Box.search([('session_id.state', '!=', 'cancelled'), ('scan_datetime', '<=', now)])
        days = {}
        day = start
        while day <= end:
            days[day] = {'date': str(day), **counts, 'total': 0, 'treated': 0,
                         'cancelled': 0, 'remaining': 0, 'future': day > today,
                         'today': day == today}
            day += timedelta(days=1)
        for box in boxes.filtered('active'):
            date = pytz.UTC.localize(box.scan_datetime).astimezone(tz).date()
            if date in days and box.session_id.mode in counts:
                days[date][box.session_id.mode] += 1
                days[date]['total'] += 1
                result[box.session_id.mode] += 1
                result['total'] += 1
        events, unknown = self._anomaly_timeline(boxes)
        result['history_incomplete'] = bool(unknown)
        # Sweep from the beginning to retain anomalies carried from earlier days.
        balance, cursor = 0, 0
        for day, row in days.items():
            limit = min(utc_midnight(day + timedelta(days=1)), now + timedelta(microseconds=1))
            lower = utc_midnight(day)
            while cursor < len(events) and events[cursor][0] < limit:
                date, delta, treated, cancelled = events[cursor]
                balance += delta
                if date >= lower:
                    row['treated'] += treated
                    row['cancelled'] += cancelled
                cursor += 1
            if row['future']:
                row.update(remaining=None, treated=None, cancelled=None)
            elif day == today:
                row['remaining'] = len(boxes.filtered(lambda b: b.active and b.section == 3))
                if any(date < limit for date in unknown):
                    row.update(treated=None, cancelled=None)
            elif any(date < limit for date in unknown):
                row.update(remaining=None, treated=None, cancelled=None)
            else:
                row['remaining'] = balance
            result['treated'] += row['treated'] or 0
            result['cancelled'] += row['cancelled'] or 0
        result['days'] = list(days.values())
        result['anomalies'] = len(boxes.filtered(lambda b: b.active and b.section == 3))
        if unknown:
            result.update(treated=None, cancelled=None)
        return result

    @api.model
    def get_counters(self):
        if not self.env.su and not self.env.user.has_group('ar_atelier.group_atelier_stock_overview'):
            raise AccessError(_("Vous n'avez pas accès au suivi du stock Atelier."))
        # This separate permission grants aggregate counts only, without granting
        # access to the underlying boxes, sessions or scan operations.
        Box = self.env['ar.atelier.box'].sudo().with_context(active_test=False)
        domain = [('active', '=', True), ('session_id.state', '!=', 'cancelled')]
        groups = Box._read_group(domain, ['session_id'], ['__count'])
        counts = dict.fromkeys(('initial', 'post_tri', 'warehouse', 'warehouse_out'), 0)
        for session, count in groups:
            if session.mode in counts:
                counts[session.mode] += count
        return {
            **counts,
            'total': sum(counts.values()),
            'anomalies': Box.search_count(domain + [('section', '=', 3)]),
        }

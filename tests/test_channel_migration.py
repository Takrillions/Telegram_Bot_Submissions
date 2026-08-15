import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from database import CURRENT_SCHEMA_VERSION, Database, DEFAULT_MIGRATIONS, Migration, apply_legacy_schema, utc_now
from handlers import render_topic_card, statistics_duration, statistics_keyboard, statistics_text, topic_name, validate_topic_template


class ChannelMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'legacy.sqlite3')
        self.backups = Path(self.temp.name) / 'backups'

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def legacy(self, owners=(1,), with_links=True):
        conn = await aiosqlite.connect(self.path)
        await apply_legacy_schema(conn)
        stamp = '2026-01-01T00:00:00+00:00'
        for owner in owners:
            group = -100000 - owner
            await conn.execute('INSERT INTO tenants VALUES(?,?,?,?,?,?,?,?,?,?)', (owner, group, f'Group {owner}', stamp, stamp, 30 + owner, f'Notice {owner}', 'Asia/Tashkent', stamp, 1))
            if with_links:
                user=owner + 100
                await conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)', (user, f'User {owner}', None, f'user{owner}', stamp, stamp, 0))
                await conn.execute('INSERT INTO tenant_subscribers VALUES(?,?,?,?)', (owner,user,stamp,stamp))
                await conn.execute('INSERT INTO active_tenant VALUES(?,?,?)', (user,owner,stamp))
                await conn.execute('INSERT INTO topics VALUES(?,?,?,?,?,?)', (owner,user,group,owner+10,stamp,stamp))
                await conn.execute('INSERT INTO notification_log VALUES(?,?,?,?)', (owner,stamp,user,stamp))
        await conn.commit(); await conn.close()

    async def test_migration_preserves_all_legacy_relations_and_settings(self):
        await self.legacy((1,2))
        db=Database(self.path, backup_dir=self.backups); await db.init()
        self.assertEqual(db.applied_migration_versions, tuple(range(1, CURRENT_SCHEMA_VERSION + 1)))
        channels=await db.list_enabled_channels(); self.assertEqual(len(channels),2)
        for channel in channels:
            owner=int(channel['owner_id']); cid=int(channel['channel_id']); user=owner+100
            self.assertEqual(channel['group_title'],f'Group {owner}')
            self.assertEqual(channel['reset_interval_days'],30+owner)
            self.assertEqual(channel['auto_cleanup_enabled'],1); self.assertEqual(channel['anonymous_prefix'],'Анон')
            self.assertEqual((await db.get_legacy_channel_for_owner(owner))['channel_id'],cid)
            self.assertEqual((await db.get_active_channel_for_user(user))['channel_id'],cid)
            self.assertEqual((await db.get_topic_for_user(channel_id=cid,user_id=user))['topic_id'],owner+10)
            self.assertEqual(await db.count_channel_subscribers(cid),1)
            self.assertEqual(len(await db.get_unnotified_subscribers(channel_id=cid,cycle_at='other')),1)
        self.assertEqual((await (await db.conn.execute('SELECT COUNT(*) AS c FROM channel_notification_log')).fetchone())['c'],2)
        self.assertEqual(len(list(self.backups.glob('*.sqlite3'))),1)
        await db.run_preflight(); await db.close()

    async def test_channels_limit_groups_and_duplicate_setup(self):
        db=Database(self.path); await db.init()
        for n in range(5):
            status, channel=await db.register_channel(owner_id=7,group_id=-200-n,group_title=str(n),default_reset_days=30,default_notice_text='n',default_timezone='UTC')
            self.assertEqual(status,'created'); self.assertIsNotNone(channel)
        status,_=await db.register_channel(owner_id=7,group_id=-999,group_title='x',default_reset_days=30,default_notice_text='n',default_timezone='UTC'); self.assertEqual(status,'owner_channel_limit')
        status, existing=await db.register_channel(owner_id=7,group_id=-200,group_title='again',default_reset_days=30,default_notice_text='n',default_timezone='UTC'); self.assertEqual(status,'existing'); self.assertEqual(existing['group_title'],'again')
        status,_=await db.register_channel(owner_id=8,group_id=-200,group_title='steal',default_reset_days=30,default_notice_text='n',default_timezone='UTC'); self.assertEqual(status,'group_has_other_owner')
        await db.close()

    async def test_initial_channel_prefix_is_normalized_and_repeat_setup_preserves_it(self):
        db=Database(self.path); await db.init()
        status, channel = await db.register_channel(
            owner_id=7, group_id=-207, group_title='Prefix',
            default_reset_days=30, default_notice_text='n', default_timezone='UTC',
            anonymous_prefix='  Гость  ',
        )
        self.assertEqual(status, 'created')
        self.assertEqual(channel['anonymous_prefix'], 'Гость')
        await db.upsert_user(user_id=701, first_name='U701', last_name=None, username=None)
        await db.attach_subscriber(channel_id=int(channel['channel_id']), user_id=701)
        self.assertEqual(await db.ensure_anonymous_tag(channel_id=int(channel['channel_id']), user_id=701), 'Гость-1')
        status, repeated = await db.register_channel(
            owner_id=7, group_id=-207, group_title='Prefix renamed',
            default_reset_days=30, default_notice_text='n', default_timezone='UTC',
            anonymous_prefix='Новый',
        )
        self.assertEqual(status, 'existing')
        self.assertEqual(repeated['anonymous_prefix'], 'Гость')
        await db.close()

    async def test_channel_isolation_counter_cycles_topics_and_disabled_cleanup(self):
        db=Database(self.path); await db.init()
        _, first=await db.register_channel(owner_id=10,group_id=-10,group_title='A',default_reset_days=30,default_notice_text='A',default_timezone='UTC')
        _, second=await db.register_channel(owner_id=10,group_id=-11,group_title='B',default_reset_days=31,default_notice_text='B',default_timezone='UTC')
        a,b=int(first['channel_id']),int(second['channel_id'])
        for user_id in (40, 41, 42):
            await db.upsert_user(user_id=user_id,first_name='u',last_name=None,username=None)
        self.assertEqual(
            [
                await db.ensure_anonymous_tag(channel_id=a, user_id=40),
                await db.ensure_anonymous_tag(channel_id=a, user_id=41),
                await db.ensure_anonymous_tag(channel_id=b, user_id=40),
            ],
            ['Анон-1', 'Анон-2', 'Анон-1'],
        )
        await db.attach_subscriber(channel_id=a,user_id=42); await db.attach_subscriber(channel_id=b,user_id=42)
        await db.create_topic_mapping(channel_id=a,user_id=42,group_id=-10,topic_id=1); await db.create_topic_mapping(channel_id=b,user_id=42,group_id=-11,topic_id=1)
        await db.mark_notification_sent(channel_id=a,cycle_at='cycle',user_id=42)
        self.assertEqual(await db.get_unnotified_subscribers(channel_id=a,cycle_at='cycle'),[])
        self.assertEqual(await db.get_unnotified_subscribers(channel_id=b,cycle_at='cycle'),[42])
        await db.set_auto_cleanup_enabled(a,False)
        self.assertEqual((await db.get_channel_by_id(a))['auto_cleanup_enabled'],0)
        stale = datetime.now(timezone.utc) - timedelta(days=10)
        await db.conn.execute("UPDATE channels SET next_reset_at=? WHERE channel_id=?", (stale.isoformat(), a))
        await db.conn.commit()
        next_reset = await db.enable_auto_cleanup(channel_id=a, days=7)
        enabled = await db.get_channel_by_id(a)
        self.assertEqual(enabled['auto_cleanup_enabled'],1)
        self.assertEqual(enabled['reset_interval_days'],7)
        self.assertGreater(next_reset, datetime.now(timezone.utc) + timedelta(days=6))
        self.assertEqual((await db.get_topic_for_user(channel_id=b,user_id=42))['group_id'],-11)
        await db.close()

    async def test_channel_migration_failure_rolls_back_legacy_schema(self):
        await self.legacy((1,))
        async def fail(conn):
            await DEFAULT_MIGRATIONS[1].apply(conn)
            raise RuntimeError('after channel copy')
        db=Database(self.path,migrations=(DEFAULT_MIGRATIONS[0],Migration(2,'channel_model',fail)),backup_dir=self.backups)
        with self.assertRaisesRegex(RuntimeError,'after channel copy'): await db.init()
        conn=sqlite3.connect(self.path)
        try:
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='tenants'").fetchone())
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='channels'").fetchone())
            self.assertEqual([r[0] for r in conn.execute('SELECT version FROM schema_migrations')],[1])
        finally: conn.close()

    async def test_migration_is_idempotent_and_new_database_needs_no_backup(self):
        await self.legacy((1,)); first=Database(self.path,backup_dir=self.backups); await first.init(); await first.close()
        backups=[p.name for p in self.backups.glob('*.sqlite3')]
        again=Database(self.path,backup_dir=self.backups); await again.init(); self.assertEqual(again.applied_migration_versions,()); await again.close()
        self.assertEqual([p.name for p in self.backups.glob('*.sqlite3')],backups)
        fresh=str(Path(self.temp.name)/'fresh.sqlite3'); new=Database(fresh,backup_dir=self.backups); await new.init(); self.assertFalse(list(self.backups.glob('*fresh*'))); await new.close()

    async def test_legacy_deep_link_mapping_stays_stable_after_new_channel(self):
        await self.legacy((55,), with_links=False)
        db=Database(self.path); await db.init()
        legacy=await db.get_legacy_channel_for_owner(55)
        _, extra=await db.register_channel(owner_id=55,group_id=-9999,group_title='Extra',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        self.assertNotEqual(legacy['channel_id'], extra['channel_id'])
        self.assertEqual((await db.get_legacy_channel_for_owner(55))['channel_id'],legacy['channel_id'])
        await db.close()

    async def test_inspection_does_not_apply_channel_migration(self):
        await self.legacy((1,), with_links=False)
        db=Database(self.path,backup_dir=self.backups)
        pending=await db.inspect_pending_migrations()
        self.assertEqual([migration.version for migration in pending],list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        conn=sqlite3.connect(self.path)
        try:
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='tenants'").fetchone())
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='channels'").fetchone())
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
        finally: conn.close()

    async def test_user_channel_selection_is_explicit_and_authorized(self):
        db=Database(self.path); await db.init()
        _, first=await db.register_channel(owner_id=1,group_id=-1,group_title='First',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _, second=await db.register_channel(owner_id=2,group_id=-2,group_title='Second',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _, hidden=await db.register_channel(owner_id=3,group_id=-3,group_title='Hidden',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        await db.upsert_user(user_id=99,first_name='User',last_name=None,username=None)
        await db.attach_subscriber(channel_id=int(first['channel_id']),user_id=99)
        await db.attach_subscriber(channel_id=int(second['channel_id']),user_id=99)
        channels=await db.list_enabled_channels_for_user(99)
        self.assertEqual([row['group_title'] for row in channels],['First','Second'])
        self.assertTrue(await db.set_active_channel(user_id=99,channel_id=int(second['channel_id'])))
        self.assertEqual((await db.get_active_channel_for_user(99))['channel_id'],second['channel_id'])
        self.assertFalse(await db.set_active_channel(user_id=99,channel_id=int(hidden['channel_id'])))
        self.assertEqual((await db.get_active_channel_for_user(99))['channel_id'],second['channel_id'])
        await db.close()

    async def test_privacy_migration_and_anonymous_tags_are_isolated(self):
        db=Database(self.path); await db.init()
        _, first=await db.register_channel(owner_id=1,group_id=-1,group_title='First',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _, second=await db.register_channel(owner_id=2,group_id=-2,group_title='Second',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        a,b=int(first['channel_id']),int(second['channel_id'])
        await db.upsert_user(user_id=77,first_name='Visible',last_name=None,username='visible')
        await db.attach_subscriber(channel_id=a,user_id=77); await db.attach_subscriber(channel_id=b,user_id=77)
        await db.create_topic_mapping(channel_id=a,user_id=77,group_id=-1,topic_id=10)
        self.assertIsNotNone(await db.get_topic_for_user(channel_id=a,user_id=77))
        tag_a=await db.set_privacy_mode(channel_id=a,user_id=77,privacy_mode='anonymous')
        self.assertEqual(tag_a,'\u0410\u043d\u043e\u043d-1')
        self.assertEqual(await db.set_privacy_mode(channel_id=a,user_id=77,privacy_mode='identified'),None)
        self.assertEqual(await db.set_privacy_mode(channel_id=a,user_id=77,privacy_mode='anonymous'),tag_a)
        self.assertEqual(await db.set_privacy_mode(channel_id=b,user_id=77,privacy_mode='anonymous'),'\u0410\u043d\u043e\u043d-1')
        await db.create_topic_mapping(channel_id=a,user_id=77,privacy_mode='anonymous',group_id=-1,topic_id=11)
        self.assertEqual((await db.get_topic_for_user(channel_id=a,user_id=77,privacy_mode='identified'))['topic_id'],10)
        self.assertEqual((await db.get_topic_for_user(channel_id=a,user_id=77,privacy_mode='anonymous'))['topic_id'],11)
        await db.close()

    async def test_message_event_log_stores_metadata_without_content(self):
        db=Database(self.path); await db.init()
        _, channel=await db.register_channel(owner_id=1,group_id=-1,group_title='Stats',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        cid=int(channel['channel_id']); await db.upsert_user(user_id=5,first_name='u',last_name=None,username=None)
        now=datetime.now(timezone.utc)
        await db.record_message_event(channel_id=cid,user_id=5,privacy_mode='anonymous',direction='subscriber_to_admin',message_type='photo',occurred_at=now,source_chat_id=100,source_message_id=10,media_group_id='album')
        await db.record_message_event(channel_id=cid,user_id=5,privacy_mode='anonymous',direction='admin_to_subscriber',message_type='text',occurred_at=now,source_chat_id=-1,source_message_id=11,admin_id=99)
        rows=await (await db.conn.execute('SELECT * FROM message_events ORDER BY event_id')).fetchall()
        self.assertEqual([(r['direction'],r['message_type'],r['privacy_mode']) for r in rows],[('subscriber_to_admin','photo','anonymous'),('admin_to_subscriber','text','anonymous')])
        columns={r['name'] for r in await (await db.conn.execute('PRAGMA table_info(message_events)')).fetchall()}
        self.assertFalse({'text','caption','content','message_text'} & columns)
        await db.close()

    async def test_sanction_reasons_are_required_visible_and_channel_scoped(self):
        db=Database(self.path); await db.init()
        _,a=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _,b=await db.register_channel(owner_id=2,group_id=-2,group_title='B',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        await db.upsert_user(user_id=42,first_name='Anon',last_name=None,username=None)
        with self.assertRaises(ValueError): await db.apply_subscriber_sanction(channel_id=int(a['channel_id']),user_id=42,admin_id=7,action='mute',reason_choice=None)
        for choice in ('spam','flood','insult','rules','advertising','suspicious_activity'):
            self.assertTrue(await db.apply_subscriber_sanction(channel_id=int(a['channel_id']),user_id=42,admin_id=7,action='mute',reason_choice=choice,show_reason_to_subscriber=True))
        reason=await db.apply_subscriber_sanction(channel_id=int(a['channel_id']),user_id=42,admin_id=7,action='mute',reason_choice='other',custom_reason='custom',show_reason_to_subscriber=False)
        self.assertEqual(reason,'custom'); state=await db.get_subscriber_moderation(channel_id=int(a['channel_id']),user_id=42)
        self.assertEqual((state['sanction_reason'],state['show_reason_to_subscriber']),('custom',0)); self.assertIsNone(await db.get_subscriber_moderation(channel_id=int(b['channel_id']),user_id=42))
        log=(await db.list_moderation_actions(channel_id=int(a['channel_id']),user_id=42))[0]; self.assertEqual((log['reason'],log['show_reason_to_subscriber']),('custom',0)); await db.close()

    async def test_moderation_log_is_channel_scoped(self):
        db=Database(self.path); await db.init()
        _,a=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _,b=await db.register_channel(owner_id=2,group_id=-2,group_title='B',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        await db.upsert_user(user_id=42,first_name='User',last_name=None,username=None)
        await db.record_moderation_action(channel_id=int(a['channel_id']),user_id=42,admin_id=7,action='mark_spam',reason='spam')
        self.assertEqual((await db.list_moderation_actions(channel_id=int(a['channel_id']),user_id=42))[0]['action'],'mark_spam')
        self.assertEqual(await db.list_moderation_actions(channel_id=int(b['channel_id']),user_id=42),[])
        await db.close()

    async def test_active_restrictions_are_channel_scoped(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, second = await db.register_channel(owner_id=2, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        await db.upsert_user(user_id=42, first_name='User', last_name=None, username=None)
        now = datetime.now(timezone.utc)
        a, b = int(first['channel_id']), int(second['channel_id'])
        await db.update_subscriber_moderation(channel_id=a, user_id=42, permanently_blocked=True)
        self.assertEqual((await db.active_subscriber_restriction(channel_id=a, user_id=42, now=now))[0], 'permanently_blocked')
        self.assertIsNone(await db.active_subscriber_restriction(channel_id=b, user_id=42, now=now))
        await db.update_subscriber_moderation(channel_id=a, user_id=42, permanently_blocked=False, muted_until=now + timedelta(minutes=10))
        kind, until = await db.active_subscriber_restriction(channel_id=a, user_id=42, now=now)
        self.assertEqual(kind, 'muted')
        self.assertGreater(until, now)
        await db.close()

    async def test_subscriber_moderation_state_is_channel_scoped(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, second = await db.register_channel(owner_id=2, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        await db.upsert_user(user_id=42, first_name='User', last_name=None, username=None)
        a, b = int(first['channel_id']), int(second['channel_id'])
        await db.update_subscriber_moderation(channel_id=a, user_id=42, rate_limit_seconds=900, permanently_blocked=True, marked_spam=True, internal_note='spam')
        state = await db.get_subscriber_moderation(channel_id=a, user_id=42)
        self.assertEqual((state['rate_limit_seconds'], state['permanently_blocked'], state['marked_spam'], state['internal_note']), (900, 1, 1, 'spam'))
        self.assertIsNone(await db.get_subscriber_moderation(channel_id=b, user_id=42))
        await db.update_subscriber_moderation(channel_id=a, user_id=42, marked_spam=False)
        self.assertEqual((await db.get_subscriber_moderation(channel_id=a, user_id=42))['marked_spam'], 0)
        await db.close()

    async def test_subscriber_card_data_is_channel_scoped_and_anonymous_safe(self):
        db = Database(self.path)
        await db.init()
        _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        channel_id = int(channel['channel_id'])
        await db.upsert_user(user_id=42, first_name='Irina', last_name='I.', username='ira')
        await db.attach_subscriber(channel_id=channel_id, user_id=42)
        await db.set_privacy_mode(channel_id=channel_id, user_id=42, privacy_mode='anonymous')
        await db.record_message_event(channel_id=channel_id, user_id=42, privacy_mode='anonymous', direction='subscriber_to_admin', message_type='text', occurred_at=datetime.now(timezone.utc), source_chat_id=42, source_message_id=1)
        card = await db.get_subscriber_card_data(channel_id=channel_id, user_id=42, privacy_mode='anonymous')
        self.assertEqual(card['message_count'], 1)
        user = type('UserStub', (), {'id': 42, 'first_name': 'Irina', 'last_name': 'И.', 'username': 'ira'})()
        anonymous_text = await render_topic_card(db, channel_id, user, card, privacy_mode='anonymous', anonymous_tag='Anon-1')
        self.assertIn('Anon-1', anonymous_text)
        self.assertNotIn('Irina', anonymous_text)
        self.assertNotIn('Telegram ID: <code>42</code>', anonymous_text)
        identified_text = await render_topic_card(db, channel_id, user, card, privacy_mode='identified')
        self.assertIn('Telegram ID: <code>42</code>', identified_text)
        self.assertIn('Irina', identified_text)
        await db.close()

    async def test_topic_templates_are_safe_and_channel_specific(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, second = await db.register_channel(owner_id=2, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        await db.set_channel_topic_template(channel_id=int(first['channel_id']), privacy_mode='identified', template='#{user_id} - {name}')
        await db.set_channel_topic_template(channel_id=int(first['channel_id']), privacy_mode='anonymous', template='Аноним {anonymous_tag}')
        user = type('UserStub', (), {'id': 42, 'first_name': 'Irina', 'last_name': None, 'username': 'ira'})()
        changed = await db.get_channel_by_id(int(first['channel_id']))
        untouched = await db.get_channel_by_id(int(second['channel_id']))
        self.assertEqual(topic_name(changed, user, privacy_mode='identified'), '#42 - Irina')
        self.assertEqual(topic_name(changed, user, privacy_mode='anonymous', anonymous_tag='Анон-3'), 'Аноним Анон-3')
        self.assertEqual(untouched['identified_topic_template'], '{name} — {username}')
        with self.assertRaises(ValueError):
            validate_topic_template('{user_id}', privacy_mode='anonymous')
        with self.assertRaises(ValueError):
            validate_topic_template('{name.__class__}', privacy_mode='identified')
        await db.close()

    async def test_owner_active_panel_channel_is_authorized_and_independent(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, second = await db.register_channel(owner_id=1, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, other = await db.register_channel(owner_id=2, group_id=-3, group_title='C', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        self.assertEqual([int(row['channel_id']) for row in await db.list_enabled_channels_for_owner(1)], [int(first['channel_id']), int(second['channel_id'])])
        self.assertTrue(await db.set_active_admin_channel(owner_id=1, channel_id=int(second['channel_id'])))
        self.assertEqual(int((await db.get_active_admin_channel(1))['channel_id']), int(second['channel_id']))
        self.assertFalse(await db.set_active_admin_channel(owner_id=1, channel_id=int(other['channel_id'])))
        self.assertIsNone(await db.get_active_admin_channel(2))
        await db.close()

    async def test_channel_cleanup_policy_is_independent_and_two_phase(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        _, second = await db.register_channel(owner_id=2, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        a, b = int(first['channel_id']), int(second['channel_id'])
        for user_id, channel_id, group_id, topic_id in ((10, a, -1, 1), (11, a, -1, 2), (12, a, -1, 3), (13, b, -2, 1)):
            await db.upsert_user(user_id=user_id, first_name='u', last_name=None, username=None)
            await db.create_topic_mapping(channel_id=channel_id, user_id=user_id, privacy_mode='identified', group_id=group_id, topic_id=topic_id)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        await db.conn.execute("UPDATE channel_topics SET created_at=?, last_activity_at=? WHERE channel_id=?", (old.isoformat(), old.isoformat(), a))
        await db.conn.execute("UPDATE channel_topics SET created_at=?, last_activity_at=? WHERE channel_id=?", (old.isoformat(), old.isoformat(), b))
        await db.conn.commit()
        await db.set_topic_status(channel_id=a, user_id=11, privacy_mode='identified', status='new')
        await db.set_topic_status(channel_id=a, user_id=12, privacy_mode='identified', status='answered')
        await db.set_channel_cleanup_policy(channel_id=a, basis='last_activity_at', status_scope='answered_closed', action='close_then_delete', final_delete_days=3)
        a_row = await db.get_channel_by_id(a)
        close_rows, delete_rows = await db.topics_due_for_auto_cleanup(channel=a_row, cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc))
        self.assertEqual([int(row['user_id']) for row in close_rows], [12])
        self.assertEqual(delete_rows, [])
        await db.mark_topic_auto_closed(channel_id=a, user_id=12, privacy_mode='identified', closed_at=datetime.now(timezone.utc) - timedelta(days=4))
        close_rows, delete_rows = await db.topics_due_for_auto_cleanup(channel=a_row, cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc))
        self.assertEqual(close_rows, [])
        self.assertEqual([int(row['user_id']) for row in delete_rows], [12])
        b_row = await db.get_channel_by_id(b)
        close_rows, delete_rows = await db.topics_due_for_auto_cleanup(channel=b_row, cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc))
        self.assertEqual(close_rows, [])
        self.assertEqual([int(row['user_id']) for row in delete_rows], [13])
        await db.close()

    async def test_automatic_cleanup_excludes_protected_topics(self):
        db = Database(self.path)
        await db.init()
        _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        channel_id = int(channel['channel_id'])
        for user_id, topic_id in ((10, 1), (11, 2), (12, 3), (13, 4)):
            await db.upsert_user(user_id=user_id, first_name='u', last_name=None, username=None)
            await db.create_topic_mapping(channel_id=channel_id, user_id=user_id, privacy_mode='identified', group_id=-1, topic_id=topic_id)
        await db.set_topic_status(channel_id=channel_id, user_id=11, privacy_mode='identified', status='in_progress')
        await db.set_topic_cleanup_protection(channel_id=channel_id, user_id=12, privacy_mode='identified', important=True)
        await db.set_topic_cleanup_protection(channel_id=channel_id, user_id=13, privacy_mode='identified', pinned=True)
        eligible = await db.topics_created_before(channel_id=channel_id, cutoff=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual([(int(row['user_id']), int(row['topic_id'])) for row in eligible], [(10, 1)])
        self.assertTrue(await db.set_topic_cleanup_protection(channel_id=channel_id, user_id=12, privacy_mode='identified', important=False))
        eligible = await db.topics_created_before(channel_id=channel_id, cutoff=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual({int(row['user_id']) for row in eligible}, {10, 12})
        await db.close()

    async def test_topic_status_defaults_and_isolated_updates(self):
        db=Database(self.path); await db.init()
        _, first=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _, second=await db.register_channel(owner_id=2,group_id=-2,group_title='B',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        a,b=int(first['channel_id']),int(second['channel_id']); await db.upsert_user(user_id=10,first_name='u',last_name=None,username=None)
        await db.create_topic_mapping(channel_id=a,user_id=10,privacy_mode='identified',group_id=-1,topic_id=1)
        await db.create_topic_mapping(channel_id=a,user_id=10,privacy_mode='anonymous',group_id=-1,topic_id=2)
        await db.create_topic_mapping(channel_id=b,user_id=10,privacy_mode='identified',group_id=-2,topic_id=1)
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-1, topic_id=1))['status'],'new')
        self.assertTrue(await db.set_topic_status(channel_id=a,user_id=10,privacy_mode='identified',status='in_progress'))
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-1, topic_id=1))['status'],'in_progress')
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-1, topic_id=2))['status'],'new')
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-2, topic_id=1))['status'],'new')
        await db.close()

    async def test_auto_answer_does_not_reopen_closed_topic_and_auto_close_syncs_status(self):
        db=Database(self.path); await db.init()
        _, channel=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        channel_id=int(channel['channel_id'])
        await db.upsert_user(user_id=10,first_name='u',last_name=None,username=None)
        await db.create_topic_mapping(channel_id=channel_id,user_id=10,privacy_mode='identified',group_id=-1,topic_id=1)
        self.assertTrue(await db.mark_topic_answered(channel_id=channel_id,user_id=10,privacy_mode='identified'))
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-1, topic_id=1))['status'],'answered')
        await db.set_topic_status(channel_id=channel_id,user_id=10,privacy_mode='identified',status='closed')
        self.assertFalse(await db.mark_topic_answered(channel_id=channel_id,user_id=10,privacy_mode='identified'))
        self.assertEqual((await db.get_topic_by_group_thread(group_id=-1, topic_id=1))['status'],'closed')
        await db.set_topic_status(channel_id=channel_id,user_id=10,privacy_mode='identified',status='answered')
        await db.mark_topic_auto_closed(channel_id=channel_id,user_id=10,privacy_mode='identified')
        row=await db.get_topic_by_group_thread(group_id=-1,topic_id=1)
        self.assertEqual(row['status'],'closed')
        self.assertIsNotNone(row['auto_closed_at'])
        await db.close()


    async def test_channel_statistics_a1_periods_responses_legacy_and_isolation(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='Tashkent', default_reset_days=30, default_notice_text='n', default_timezone='Asia/Tashkent')
        _, second = await db.register_channel(owner_id=1, group_id=-2, group_title='Other', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        a, b = int(first['channel_id']), int(second['channel_id'])
        now = datetime(2026, 5, 10, 20, 30, tzinfo=timezone.utc)  # 01:30 next day in Tashkent
        for user_id in (10, 11, 12):
            await db.upsert_user(user_id=user_id, first_name='u', last_name=None, username=None)
        for user_id in (10, 11):
            await db.attach_subscriber(channel_id=a, user_id=user_id)
        await db.attach_subscriber(channel_id=b, user_id=10)
        # Make user 10 new today in channel A, but user 11 old there.
        await db.conn.execute("UPDATE channel_subscribers SET first_seen_at=? WHERE channel_id=? AND user_id=10", ('2026-05-10T20:00:00+00:00', a))
        await db.conn.execute("UPDATE channel_subscribers SET first_seen_at=? WHERE channel_id=? AND user_id=11", ('2026-04-01T00:00:00+00:00', a))
        await db.conn.commit()
        async def event(channel_id, user_id, direction, when, message_id, conversation_id=None):
            await db.record_message_event(channel_id=channel_id, user_id=user_id, privacy_mode='identified', direction=direction, message_type='text', occurred_at=when, source_chat_id=channel_id if direction == 'admin_to_subscriber' else user_id, source_message_id=message_id, admin_id=77 if direction == 'admin_to_subscriber' else None, conversation_id=conversation_id)
        # Two known conversations: 100 answered in ten minutes; 101 unanswered.
        await event(a, 10, 'subscriber_to_admin', now-timedelta(minutes=30), 1, 100)
        await event(a, 10, 'admin_to_subscriber', now-timedelta(minutes=20), 2, 100)
        await event(a, 11, 'subscriber_to_admin', now-timedelta(days=2), 3, 101)
        # Reply before a conversation starts is not a response.
        await event(a, 11, 'admin_to_subscriber', now-timedelta(days=3), 4, 101)
        # Legacy event still counts as a message and marks precision incomplete.
        await event(a, 10, 'subscriber_to_admin', now-timedelta(hours=1), 5, None)
        # Same real user in another channel must not affect channel A.
        await event(b, 10, 'subscriber_to_admin', now-timedelta(minutes=5), 6, 999)
        # Duplicate source event is ignored by the journal unique constraint.
        await event(a, 10, 'subscriber_to_admin', now, 1, 100)

        today = await db.get_channel_statistics(a, period='today', now=now)
        self.assertEqual((today['unique_subscribers'], today['new_subscribers']), (2, 1))
        self.assertEqual((today['subscriber_messages'], today['admin_replies']), (2, 1))
        self.assertEqual((today['conversation_count'], today['answered_conversation_count']), (1, 1))
        self.assertEqual(today['answered_conversation_share'], 100.0)
        self.assertEqual(today['average_first_response_seconds'], 600.0)
        self.assertEqual(today['median_first_response_seconds'], 600.0)
        self.assertFalse(today['conversation_metrics_complete'])
        self.assertEqual((today['active_subscribers_1d'], today['active_subscribers_7d'], today['active_subscribers_30d']), (1, 2, 2))
        week = await db.get_channel_statistics(a, period='7d', now=now)
        self.assertEqual((week['subscriber_messages'], week['conversation_count'], week['answered_conversation_count']), (3, 2, 1))
        self.assertEqual(week['answered_conversation_share'], 50.0)
        month = await db.get_channel_statistics(a, period='30d', now=now)
        all_time = await db.get_channel_statistics(a, period='all', now=now)
        self.assertEqual(month['subscriber_messages'], all_time['subscriber_messages'])
        self.assertEqual(all_time['average_messages_per_subscriber'], 1.5)
        self.assertEqual((await db.get_channel_statistics(b, period='all', now=now))['subscriber_messages'], 1)
        # Deleting the current topic mapping cannot remove journal-backed metrics.
        await db.create_topic_mapping(channel_id=a, user_id=10, privacy_mode='identified', group_id=-1, topic_id=100)
        await db.delete_topic_mapping(channel_id=a, user_id=10, privacy_mode='identified')
        self.assertEqual((await db.get_channel_statistics(a, period='all', now=now))['conversation_count'], 2)
        empty = await db.get_channel_statistics(b, period='today', now=now+timedelta(days=90))
        self.assertEqual((empty['subscriber_messages'], empty['conversation_count'], empty['answered_conversation_share']), (0, 0, 0.0))
        self.assertIsNone(empty['average_first_response_seconds'])
        await db.close()


    async def test_channel_statistics_a21_media_time_and_privacy_safe_top(self):
        db = Database(self.path)
        await db.init()
        _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='Asia/Tashkent')
        _, second = await db.register_channel(owner_id=1, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        a, b = int(first['channel_id']), int(second['channel_id'])
        now = datetime(2026, 5, 10, 22, 0, tzinfo=timezone.utc)
        for user_id in range(10, 16):
            await db.upsert_user(user_id=user_id, first_name=f'User{user_id}', last_name='Safe', username=f'user{user_id}')
            await db.attach_subscriber(channel_id=a, user_id=user_id)
        await db.attach_subscriber(channel_id=b, user_id=10)
        anonymous_tag = await db.set_privacy_mode(channel_id=a, user_id=10, privacy_mode='anonymous')
        self.assertIsNotNone(anonymous_tag)

        next_id = 1
        async def event(user_id, message_type, when, *, group=None, channel_id=a, privacy='identified'):
            nonlocal next_id
            message_id = next_id; next_id += 1
            await db.record_message_event(channel_id=channel_id, user_id=user_id, privacy_mode=privacy, direction='subscriber_to_admin', message_type=message_type, occurred_at=when, source_chat_id=user_id, source_message_id=message_id, media_group_id=group)
            return message_id

        # Local Tashkent hours: UTC 19:xx -> 00, UTC 20:xx -> 01, UTC 21:xx -> 02.
        await event(11, 'text', now-timedelta(hours=3), privacy='identified')
        await event(11, 'text', now-timedelta(hours=2, minutes=55), privacy='identified')
        await event(11, 'text', now-timedelta(hours=2, minutes=50), privacy='identified')
        await event(10, 'photo', now-timedelta(hours=2), group='album-one', privacy='anonymous')
        duplicate_id = await event(10, 'photo', now-timedelta(hours=2, minutes=1), group='album-one', privacy='anonymous')
        await db.record_message_event(channel_id=a, user_id=10, privacy_mode='anonymous', direction='subscriber_to_admin', message_type='photo', occurred_at=now, source_chat_id=10, source_message_id=duplicate_id, media_group_id='album-one')
        await event(10, 'photo', now-timedelta(hours=1, minutes=50), privacy='anonymous')
        await event(12, 'video', now-timedelta(hours=1), group='album-two')
        await event(12, 'document', now-timedelta(minutes=59), group='album-two')
        await event(12, 'voice', now-timedelta(minutes=58))
        await event(13, 'audio', now-timedelta(minutes=57))
        await event(14, 'sticker', now-timedelta(minutes=56))
        await event(15, 'animation', now-timedelta(minutes=55))
        # Events outside 7d and 30d prove the common period filter is used.
        await event(13, 'photo', now-timedelta(days=8))
        await event(14, 'text', now-timedelta(days=31))
        # Same real user, other channel and identified presentation: never leaks into A.
        await event(10, 'video', now-timedelta(minutes=5), channel_id=b, privacy='identified')

        today = await db.get_channel_statistics(a, period='today', now=now)
        self.assertEqual(today['media'], {'text': 3, 'photo': 3, 'video': 1, 'document': 1, 'voice': 1, 'audio': 1, 'sticker': 1, 'other': 1})
        self.assertEqual((today['album_count'], today['media_items_count']), (2, 9))
        self.assertEqual(today['messages_by_hour'][0], 4)
        self.assertEqual(today['messages_by_hour'][1], 2)
        self.assertEqual(today['messages_by_hour'][2], 6)
        self.assertEqual(today['messages_by_weekday'][0], 12)  # Monday in local channel timezone.
        self.assertEqual(today['most_active_hour'], 2)
        self.assertEqual(today['most_active_weekday'], 0)
        self.assertEqual(today['top_subscribers'][0]['display_name'], 'User11 Safe')
        anonymous = next(row for row in today['top_subscribers'] if row['privacy_mode'] == 'anonymous')
        self.assertEqual(anonymous['anonymous_tag'], anonymous_tag)
        self.assertEqual(anonymous['display_name'], anonymous_tag)
        self.assertNotIn('user_id', anonymous)
        self.assertNotIn('username', anonymous)
        self.assertNotIn('first_name', anonymous)
        self.assertLessEqual(len(today['top_subscribers']), 5)

        week = await db.get_channel_statistics(a, period='7d', now=now)
        month = await db.get_channel_statistics(a, period='30d', now=now)
        all_time = await db.get_channel_statistics(a, period='all', now=now)
        self.assertEqual(week['media']['photo'], 3)
        self.assertEqual(month['media']['photo'], 4)
        self.assertEqual(all_time['media']['text'], 4)
        self.assertEqual((await db.get_channel_statistics(b, period='today', now=now))['media']['video'], 1)
        # Deleted topic mappings do not alter journal-based A2.1 figures.
        await db.create_topic_mapping(channel_id=a, user_id=12, privacy_mode='identified', group_id=-1, topic_id=12)
        await db.delete_topic_mapping(channel_id=a, user_id=12, privacy_mode='identified')
        self.assertEqual((await db.get_channel_statistics(a, period='today', now=now))['album_count'], 2)
        empty = await db.get_channel_statistics(a, period='today', now=now+timedelta(days=90))
        self.assertEqual(empty['media'], {'text': 0, 'photo': 0, 'video': 0, 'document': 0, 'voice': 0, 'audio': 0, 'sticker': 0, 'other': 0})
        self.assertEqual((empty['album_count'], empty['media_items_count'], empty['most_active_hour'], empty['most_active_weekday'], empty['top_subscribers']), (0, 0, None, None, []))
        await db.close()

    async def test_channel_statistics_a21_uses_lowest_bucket_for_ties(self):
        db = Database(self.path)
        await db.init()
        _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='UTC', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        channel_id = int(channel['channel_id'])
        await db.upsert_user(user_id=1, first_name='One', last_name=None, username=None)
        await db.attach_subscriber(channel_id=channel_id, user_id=1)
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)  # Sunday
        for message_id, stamp in ((1, now-timedelta(hours=1)), (2, now-timedelta(days=1, hours=1))):
            await db.record_message_event(channel_id=channel_id, user_id=1, privacy_mode='identified', direction='subscriber_to_admin', message_type='text', occurred_at=stamp, source_chat_id=1, source_message_id=message_id)
        stats = await db.get_channel_statistics(channel_id, period='7d', now=now)
        self.assertEqual(stats['messages_by_hour'][11], 2)
        self.assertEqual(stats['most_active_hour'], 11)
        # Saturday (5) and Sunday (6) are tied: fixed weekday order picks Saturday.
        self.assertEqual(stats['most_active_weekday'], 5)
        await db.close()


    def test_statistics_ui_uses_precalculated_data_safe_callbacks_and_legacy_warning(self):
        stats = {
            'period': '7d', 'conversation_metrics_complete': False,
            'unique_subscribers': 3, 'active_subscribers_1d': 1, 'active_subscribers_7d': 2, 'active_subscribers_30d': 3,
            'new_subscribers': 1, 'subscriber_messages': 12, 'admin_replies': 4, 'average_messages_per_subscriber': 4.0,
            'conversation_count': 3, 'answered_conversation_count': 2, 'answered_conversation_share': 66.7,
            'average_first_response_seconds': 252, 'median_first_response_seconds': 35,
            'media': {'text': 1, 'photo': 2, 'video': 3, 'document': 4, 'voice': 5, 'audio': 6, 'sticker': 7, 'other': 8},
            'album_count': 2, 'media_items_count': 27,
            'messages_by_hour': {hour: (3 if hour == 9 else 0) for hour in range(24)},
            'messages_by_weekday': {day: (4 if day == 0 else 0) for day in range(7)},
            'most_active_hour': 9, 'most_active_weekday': 0,
            'top_subscribers': [
                {'privacy_mode': 'anonymous', 'anonymous_tag': 'Анон-1', 'display_name': 'Анон-1', 'message_count': 9},
                {'privacy_mode': 'identified', 'display_name': 'Мария', 'message_count': 8},
            ],
        }
        overview = statistics_text(stats, 'overview')
        messages = statistics_text(stats, 'messages')
        responses = statistics_text(stats, 'responses')
        activity = statistics_text(stats, 'activity')
        top = statistics_text(stats, 'top')
        self.assertIn('Фото: 2', messages)
        self.assertIn('4 мин 12 сек', responses)
        self.assertIn('09:00', activity)
        self.assertIn('Анон-1', top)
        self.assertNotIn('user_id', top)
        self.assertNotIn('username', top)
        stats['conversation_metrics_complete'] = True
        self.assertNotIn('Для части старых обращений детальная статистика ответов недоступна.', statistics_text(stats, 'overview'))
        self.assertEqual((statistics_duration(None), statistics_duration(35), statistics_duration(252), statistics_duration(4080)), ('—', '35 сек', '4 мин 12 сек', '1 ч 8 мин'))
        keyboard = statistics_keyboard(source='stats', page='activity', period='30d')
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn('stats:top:30d', callbacks)
        self.assertIn('stats:activity:7d', callbacks)
        self.assertTrue(all(len(str(data)) <= 64 for data in callbacks))
        self.assertTrue(all('Анон-1' not in str(data) and 'Мария' not in str(data) for data in callbacks))
        panel_callbacks = [button.callback_data for row in statistics_keyboard(source='panel', page='top', period='today').inline_keyboard for button in row]
        self.assertIn('panel:stats:overview:today', panel_callbacks)
        self.assertIn('panel:home', panel_callbacks)

    def test_statistics_handlers_use_one_api_and_reauthorize_callbacks(self):
        source = Path('handlers.py').read_text(encoding='utf-8')
        command = source[source.index('async def stats_handler'):source.index('async def statistics_callback')]
        callback = source[source.index('async def statistics_callback'):source.index('    @router.message(Command("panel"))')]
        panel = source[source.index('async def panel_callback'):source.index('        elif data == "panel:notices"')]
        self.assertIn('db.get_channel_statistics', command)
        self.assertNotIn('db.channel_statistics', command)
        self.assertIn('_statistics_callback_channel', callback)
        self.assertIn('db.get_channel_statistics', callback)
        self.assertIn('_panel_callback_channel', panel)
        self.assertIn('db.get_channel_statistics', panel)
        self.assertNotIn('db.channel_statistics', panel)


    def test_search_command_uses_existing_fsm_and_safe_topic_links(self):
        source = Path('handlers.py').read_text(encoding='utf-8')
        command = source[source.index('async def search_command'):source.index('    # --------------------------------------------------------------\n    # Panel callbacks')]
        self.assertIn('@router.message(Command("search")', source)
        self.assertIn('SearchFlow.query', command)
        self.assertIn('get_active_admin_channel', command)
        panel = source[source.index('async def panel_handler'):source.index('async def set_period_handler')]
        self.assertIn('await state.clear()', panel)
        self.assertIn('async def panel_callback(callback: CallbackQuery, state: FSMContext)', source)
        self.assertIn('def forum_topic_url', source)
        self.assertIn('raw.startswith("-100")', source)
        self.assertIn('InlineKeyboardButton(text="Открыть", url=topic_url)', source)

    async def test_search_is_channel_scoped_privacy_safe_and_escapes_like(self):
        db = Database(self.path); await db.init()
        try:
            _, first = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
            _, second = await db.register_channel(owner_id=1, group_id=-2, group_title='B', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
            a, b = int(first['channel_id']), int(second['channel_id'])
            await db.upsert_user(user_id=10, first_name='Alice', last_name='Example', username='alice')
            await db.upsert_user(user_id=11, first_name='Secret', last_name='Person', username='secret')
            await db.attach_subscriber(channel_id=a, user_id=10); await db.attach_subscriber(channel_id=b, user_id=10)
            await db.create_topic_mapping(channel_id=a, user_id=10, group_id=-100000000001, topic_id=17)
            await db.attach_subscriber(channel_id=a, user_id=11)
            await db.set_privacy_mode(channel_id=a, user_id=11, privacy_mode='anonymous')
            await db.conn.execute("UPDATE anonymous_tags SET tag='Аноним-7' WHERE channel_id=? AND user_id=?", (a, 11))
            await db.conn.execute("UPDATE channel_anonymous_counters SET cycle_key='2031-01-01T00:00:00+00:00', next_number=9 WHERE channel_id=?", (a,))
            await db.conn.execute("INSERT INTO anonymous_tags(channel_id,user_id,cycle_key,number,tag,assigned_at) VALUES(?,?,?,?,?,?)", (a, 11, '2031-01-01T00:00:00+00:00', 8, 'Аноним-8', '2031-01-01T00:00:00+00:00'))
            await db.conn.commit()
            rows, total = await db.search_subscribers(channel_id=a, query='  @ALIC  ')
            self.assertEqual((total, rows[0]['display_name']), (1, 'Alice Example'))
            by_id, total = await db.search_subscribers(channel_id=a, query='10')
            self.assertEqual((total, by_id[0]['user_id'], by_id[0]['display_name']), (1, 10, 'Alice Example'))
            anonymous, total = await db.search_subscribers(channel_id=a, query='Аноним-7')
            self.assertEqual((total, anonymous[0]['display_name'], anonymous[0]['privacy_mode']), (1, 'Аноним-8', 'anonymous'))
            self.assertFalse({'first_name', 'last_name', 'username'} & set(anonymous[0]))
            self.assertEqual((await db.search_subscribers(channel_id=a, query='11'))[1], 0)
            conversations, conversation_total = await db.search_subscribers(channel_id=a, query='Alice', conversations_only=True)
            self.assertEqual((conversation_total, conversations[0]['topic_id'], conversations[0]['group_id']), (1, 17, -100000000001))
            self.assertEqual((await db.search_subscribers(channel_id=b, query='Alice'))[1], 1)
            self.assertEqual((await db.search_subscribers(channel_id=b, query='Аноним-8'))[1], 0)
            self.assertEqual((await db.search_subscribers(channel_id=a, query='%'))[1], 0)
            self.assertEqual((await db.search_subscribers(channel_id=a, query='_'))[1], 0)
        finally:
            await db.close()
    async def test_admin_statistics_is_channel_scoped_response_aware_and_deduplicated(self):
        db=Database(self.path); await db.init()
        _,a=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        _,b=await db.register_channel(owner_id=1,group_id=-2,group_title='B',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        a,b=int(a['channel_id']),int(b['channel_id']); now=utc_now()
        for uid,name in ((10,'Sub'),(701,'Anna'),(702,'Bella')): await db.upsert_user(user_id=uid,first_name=name,last_name=None,username=None)
        await db.attach_subscriber(channel_id=a,user_id=10); await db.attach_subscriber(channel_id=b,user_id=10)
        async def e(channel,direction,at,mid,cid,admin=None): await db.record_message_event(channel_id=channel,user_id=10,privacy_mode='identified',direction=direction,message_type='text',occurred_at=at,source_chat_id=channel if admin else 10,source_message_id=mid,conversation_id=cid,admin_id=admin)
        await e(a,'subscriber_to_admin',now-timedelta(minutes=10),1,1)
        await e(a,'admin_to_subscriber',now-timedelta(minutes=5),2,1,701)
        await e(a,'admin_to_subscriber',now-timedelta(minutes=4),3,1,702)
        await e(a,'subscriber_to_admin',now-timedelta(minutes=3),4,None)
        await e(b,'admin_to_subscriber',now-timedelta(minutes=1),5,9,702)
        await db.record_moderation_action(channel_id=a,user_id=10,admin_id=701,action='warning')
        await db.record_moderation_action(channel_id=a,user_id=10,admin_id=702,action='mark_spam')
        stats=await db.get_channel_admin_statistics(a,period='today',now=now)
        anna=next(row for row in stats['admins'] if row['display_name']=='Anna'); bella=next(row for row in stats['admins'] if row['display_name']=='Bella')
        self.assertEqual((anna['reply_count'],anna['first_response_count'],anna['handled_conversations'],anna['average_first_response_seconds'],anna['warnings']),(1,1,1,300.0,1))
        self.assertEqual((bella['reply_count'],bella['first_response_count'],bella['handled_conversations'],bella['spam_marks']),(1,0,0,1))
        self.assertEqual((stats['admin_replies'],stats['active_admin_count'],stats['tracked_conversation_count'],stats['handled_conversation_count'],stats['unanswered_conversation_count'],stats['conversation_metrics_complete']),(2,2,1,1,0,False))
        self.assertEqual((await db.get_channel_admin_statistics(b,period='today',now=now))['admin_replies'],1)
        await db.close()

    async def test_admin_statistics_counts_unanswered_conversations_without_assigning_blame(self):
        db=Database(self.path); await db.init()
        _,channel=await db.register_channel(owner_id=1,group_id=-1,group_title='A',default_reset_days=30,default_notice_text='n',default_timezone='UTC')
        cid=int(channel['channel_id']); now=utc_now()
        for uid,name in ((10,'Sub'),(701,'Anna')): await db.upsert_user(user_id=uid,first_name=name,last_name=None,username=None)
        await db.attach_subscriber(channel_id=cid,user_id=10)
        async def e(direction,at,mid,conversation_id,admin=None):
            await db.record_message_event(channel_id=cid,user_id=10,privacy_mode='identified',direction=direction,message_type='text',occurred_at=at,source_chat_id=cid if admin else 10,source_message_id=mid,conversation_id=conversation_id,admin_id=admin)
        await e('subscriber_to_admin',now-timedelta(minutes=30),1,100)
        await e('admin_to_subscriber',now-timedelta(minutes=25),2,100,701)
        await e('subscriber_to_admin',now-timedelta(minutes=20),3,200)
        stats=await db.get_channel_admin_statistics(cid,period='today',now=now)
        anna=stats['admins'][0]
        self.assertEqual((anna['handled_conversations'],stats['tracked_conversation_count'],stats['handled_conversation_count'],stats['unanswered_conversation_count']),(1,2,1,1))
        self.assertNotIn('unanswered_conversations',anna)
        await db.close()

    async def test_export_snapshot_uses_channel_scoped_statistics_apis(self):
        db = Database(self.path); await db.init()
        _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='Export', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
        cid = int(channel['channel_id'])
        snapshot = await db.get_channel_export_snapshot(cid, period='all')
        self.assertEqual((snapshot['channel_id'], snapshot['period']), (cid, 'all'))
        self.assertIn('media', snapshot['statistics'])
        self.assertIn('admins', snapshot['administrators'])
        self.assertNotIn('message_text', repr(snapshot))
        await db.close()

    def test_admin_statistics_ui_uses_service_api(self):
        source=Path('handlers.py').read_text(encoding='utf-8')
        self.assertIn('get_channel_admin_statistics',source)
        self.assertIn('admin_statistics_text',source)
        self.assertIn('"admins"',source)

    async def test_anonymous_prefix_cycle_is_independent_from_schedule_and_resets_atomically(self):
        db = Database(self.path); await db.init()
        try:
            _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
            cid = int(channel['channel_id'])
            for user_id in (10, 11, 12):
                await db.upsert_user(user_id=user_id, first_name=f'U{user_id}', last_name=None, username=None)
                await db.attach_subscriber(channel_id=cid, user_id=user_id)
            first = await db.set_privacy_mode(channel_id=cid, user_id=10, privacy_mode='anonymous')
            self.assertEqual(first, 'Анон-1')
            await db.set_channel_anonymous_prefix(channel_id=cid, prefix='  Гость  ')
            self.assertEqual(await db.ensure_anonymous_tag(channel_id=cid, user_id=10), 'Анон-1')
            second = await db.set_privacy_mode(channel_id=cid, user_id=11, privacy_mode='anonymous')
            self.assertEqual(second, 'Гость-2')

            before = await db.get_anonymous_counter_state(cid)
            await db.set_channel_period(cid, 45)
            await db.enable_auto_cleanup(channel_id=cid, days=7)
            after_schedule_change = await db.get_anonymous_counter_state(cid)
            self.assertEqual(after_schedule_change['cycle_key'], before['cycle_key'])
            self.assertEqual(await db.ensure_anonymous_tag(channel_id=cid, user_id=10), 'Анон-1')

            await db.reset_anonymous_cycle(cid)
            reset = await db.get_anonymous_counter_state(cid)
            self.assertEqual(reset['next_number'], 1)
            self.assertNotEqual(reset['cycle_key'], before['cycle_key'])
            self.assertEqual(await db.ensure_anonymous_tag(channel_id=cid, user_id=10), 'Гость-1')
            self.assertEqual(await db.ensure_anonymous_tag(channel_id=cid, user_id=12), 'Гость-2')
        finally:
            await db.close()

    async def test_anonymous_concurrent_assignments_are_unique_per_channel(self):
        db = Database(self.path); await db.init()
        try:
            _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
            cid = int(channel['channel_id'])
            users = list(range(100, 120))
            for user_id in users:
                await db.upsert_user(user_id=user_id, first_name='U', last_name=None, username=None)
                await db.attach_subscriber(channel_id=cid, user_id=user_id)
            tags = await asyncio.gather(*(db.set_privacy_mode(channel_id=cid, user_id=user_id, privacy_mode='anonymous') for user_id in users))
            self.assertEqual(len(tags), len(set(tags)))
            self.assertEqual({int(tag.rsplit('-', 1)[1]) for tag in tags}, set(range(1, 21)))
        finally:
            await db.close()

    async def test_completed_cleanup_cycle_restarts_anonymous_numbering(self):
        db = Database(self.path); await db.init()
        try:
            _, channel = await db.register_channel(owner_id=1, group_id=-1, group_title='A', default_reset_days=30, default_notice_text='n', default_timezone='UTC')
            cid = int(channel['channel_id'])
            await db.upsert_user(user_id=10, first_name='U', last_name=None, username=None)
            await db.attach_subscriber(channel_id=cid, user_id=10)
            self.assertEqual(await db.set_privacy_mode(channel_id=cid, user_id=10, privacy_mode='anonymous'), 'Анон-1')
            next_reset = datetime.now(timezone.utc) + timedelta(days=30)
            await db.advance_channel_reset(channel_id=cid, next_reset_at=next_reset)
            state = await db.get_anonymous_counter_state(cid)
            self.assertEqual(state['next_number'], 1)
            self.assertTrue(str(state['cycle_key']).startswith('auto:'))
            self.assertEqual((await db.get_channel_by_id(cid))['next_reset_at'], next_reset.isoformat(timespec='seconds'))
            self.assertEqual(await db.ensure_anonymous_tag(channel_id=cid, user_id=10), 'Анон-1')
        finally:
            await db.close()


if __name__ == '__main__': unittest.main()

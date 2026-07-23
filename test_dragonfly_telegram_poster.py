import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path

SCRIPT = Path(__file__).with_name('dragonfly_telegram_poster.py')
spec = importlib.util.spec_from_file_location('poster', SCRIPT)
poster = importlib.util.module_from_spec(spec)
sys.modules['poster'] = poster
spec.loader.exec_module(poster)


def cfg():
    return poster.Config(
        dragonfly_token='df',
        dragonfly_user_id='2723',
        telegram_token=None,
        telegram_chat_id='@channel',
        db_path=Path(':memory:'),
        dry_run=True,
        feed_type='all',
        request_delay=0,
        send_delay=0,
        text_delay=0,
        photo_delay=0,
        album_delay=0,
        animation_delay=0,
        mixed_media_delay=0,
        media_item_delay=0,
        poll_interval=15,
        limit=20,
        max_attempts=poster.DEFAULT_MAX_ATTEMPTS,
        keep_sent=poster.DEFAULT_KEEP_SENT,
        log_file=None,
        upload_media=False,
        alert_chat_id=None,
        discussion_chat_id=None,
        best_chat_id=None,
        cookie_file=None,
    )


def post(**overrides):
    base = {
        'post_id': 123,
        'author_name': 'Alice <A>',
        'author_link': 'alice_profile',
        'created_at': '2026-07-20T08:00:00',
        'description': 'hello',
        'photos': [],
        'audios': [],
        'is_repost': False,
    }
    base.update(overrides)
    return base


class CaptureTelegram:
    def __enter__(self):
        self.calls = []
        self.orig_tg_request = poster.tg_request
        self.orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            self.calls.append((method, payload))
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        return self.calls
    def __exit__(self, *exc):
        poster.tg_request = self.orig_tg_request
        poster.time.sleep = self.orig_sleep


class DragonflyPosterTests(unittest.TestCase):
    def test_feed_cache_stores_and_reads_posts_by_offset_window(self):
        con = poster.init_db(Path(':memory:'))
        posts = [post(post_id=100 - i, description=f'post {i}') for i in range(5)]

        poster.upsert_feed_cache(con, feed_type='all', offset=20, posts=posts)
        cached = poster.read_feed_cache(con, feed_type='all', count=3, offset=21)

        self.assertEqual([p['post_id'] for p in cached], [99, 98, 97])
        self.assertEqual(cached[0]['description'], 'post 1')

    def test_cmd_refresh_feed_cache_fetches_and_stores_requested_window(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.limit = 2
        calls = []
        orig_fetch_page = poster.fetch_feed_page
        def fake_fetch_page(cfg, offset):
            calls.append(offset)
            return [post(post_id=1000 - offset - i) for i in range(2)]
        poster.fetch_feed_page = fake_fetch_page
        try:
            rc = poster.cmd_refresh_feed_cache(c, con, poster.argparse.Namespace(count=5, offset=10))
        finally:
            poster.fetch_feed_page = orig_fetch_page

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [10, 12, 14])
        cached = poster.read_feed_cache(con, feed_type='all', count=5, offset=10)
        self.assertEqual([p['post_id'] for p in cached], [990, 989, 988, 987, 986])

    def test_sync_stats_can_use_feed_cache_without_fetching_network_feed(self):
        con = poster.init_db(Path(':memory:'))
        cached_posts = [post(post_id=700), post(post_id=699)]
        poster.upsert_feed_cache(con, feed_type='all', offset=0, posts=cached_posts)
        c = cfg()
        synced = []
        orig_fetch = poster.fetch_recent_posts
        orig_sync_stats = poster.sync_post_stats
        orig_sync_best = poster.sync_best_post
        poster.fetch_recent_posts = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network feed should not be fetched'))
        poster.sync_post_stats = lambda cfg, con, p: synced.append(p['post_id']) or True
        poster.sync_best_post = lambda *a, **k: None
        try:
            rc = poster.cmd_sync_stats(c, con, poster.argparse.Namespace(count=2, offset=0, use_feed_cache=True))
        finally:
            poster.fetch_recent_posts = orig_fetch
            poster.sync_post_stats = orig_sync_stats
            poster.sync_best_post = orig_sync_best

        self.assertEqual(rc, 0)
        self.assertEqual(synced, [700, 699])

    def test_sync_comments_can_use_feed_cache_without_fetching_network_feed(self):
        con = poster.init_db(Path(':memory:'))
        cached_posts = [post(post_id=800, comments_count=1)]
        poster.upsert_feed_cache(con, feed_type='all', offset=17, posts=cached_posts)
        c = cfg()
        called = []
        orig_fetch = poster.fetch_recent_posts
        orig_should = poster.should_fetch_post_comments
        orig_sync_comments = poster.sync_post_comments
        poster.fetch_recent_posts = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network feed should not be fetched'))
        poster.should_fetch_post_comments = lambda *a, **k: True
        poster.sync_post_comments = lambda cfg, con, p, send_existing=False: called.append((p['post_id'], send_existing)) or (0, 0)
        try:
            rc = poster.cmd_sync_comments(c, con, poster.argparse.Namespace(count=1, offset=17, hot_count=20, send_existing=False, use_feed_cache=True))
        finally:
            poster.fetch_recent_posts = orig_fetch
            poster.should_fetch_post_comments = orig_should
            poster.sync_post_comments = orig_sync_comments

        self.assertEqual(rc, 0)
        self.assertEqual(called, [(800, False)])

    def test_admin_bot_set_rejects_unauthorized_and_accepts_admin(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        unauthorized = {
            'update_id': 1,
            'message': {'message_id': 10, 'chat': {'id': 111}, 'from': {'id': 999}, 'text': '/set request_delay 3'},
        }
        authorized = {
            'update_id': 2,
            'message': {'message_id': 11, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/set request_delay 3'},
        }
        with CaptureTelegram() as calls:
            self.assertEqual(poster.process_admin_update(c, con, unauthorized), False)
            self.assertIsNone(poster.kv_get(con, 'runtime_setting.request_delay'))
            self.assertEqual(poster.process_admin_update(c, con, authorized), True)
        self.assertEqual(poster.kv_get(con, 'runtime_setting.request_delay'), '3.0')
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertIn('нет доступа', calls[0][1]['text'].lower())
        self.assertIn('request_delay = 3.0', calls[1][1]['text'])

    def test_admin_panel_opens_parameter_details_with_explanation_and_fixed_values(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        panel = {
            'update_id': 3,
            'message': {'message_id': 12, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/panel'},
        }
        detail = {
            'update_id': 4,
            'callback_query': {
                'id': 'cb1',
                'from': {'id': 498975827},
                'message': {'message_id': 12, 'chat': {'id': 222}},
                'data': 'admin:setting:request_delay',
            },
        }
        fixed = {
            'update_id': 5,
            'callback_query': {
                'id': 'cb2',
                'from': {'id': 498975827},
                'message': {'message_id': 12, 'chat': {'id': 222}},
                'data': 'admin:set:request_delay:4',
            },
        }
        with CaptureTelegram() as calls:
            self.assertTrue(poster.process_admin_update(c, con, panel))
            self.assertTrue(poster.process_admin_update(c, con, detail))
            self.assertTrue(poster.process_admin_update(c, con, fixed))
        self.assertEqual(calls[0][0], 'sendMessage')
        keyboard_text = json.dumps(calls[0][1]['reply_markup'], ensure_ascii=False)
        self.assertIn('🌐 API-задержка', keyboard_text)
        self.assertIn('↩️ Сбросить всё к default', keyboard_text)
        edited_texts = [payload['text'] for method, payload in calls if method == 'editMessageText']
        self.assertTrue(any('Что регулирует' in text and 'Текущее значение' in text for text in edited_texts))
        self.assertEqual(poster.kv_get(con, 'runtime_setting.request_delay'), '4.0')

    def test_admin_panel_custom_value_flow_and_reset_defaults(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        custom = {
            'update_id': 6,
            'callback_query': {
                'id': 'cb3',
                'from': {'id': 498975827},
                'message': {'message_id': 12, 'chat': {'id': 222}},
                'data': 'admin:custom:comments_interval',
            },
        }
        typed = {
            'update_id': 7,
            'message': {'message_id': 13, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '45'},
        }
        reset = {
            'update_id': 8,
            'callback_query': {
                'id': 'cb4',
                'from': {'id': 498975827},
                'message': {'message_id': 12, 'chat': {'id': 222}},
                'data': 'admin:reset_defaults',
            },
        }
        with CaptureTelegram() as calls:
            self.assertTrue(poster.process_admin_update(c, con, custom))
            self.assertEqual(poster.kv_get(con, 'admin_pending_setting.498975827'), 'comments_interval')
            self.assertTrue(poster.process_admin_update(c, con, typed))
            self.assertEqual(poster.kv_get(con, 'runtime_setting.comments_interval'), '45.0')
            self.assertIsNone(poster.kv_get(con, 'admin_pending_setting.498975827'))
            self.assertTrue(poster.process_admin_update(c, con, reset))
        self.assertIsNone(poster.kv_get(con, 'runtime_setting.comments_interval'))
        methods = [m for m, _ in calls]
        self.assertIn('answerCallbackQuery', methods)

    def test_admin_menu_alias_and_cached_updates_are_processed_once(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        cached_menu = {
            'update_id': 42,
            'message': {'message_id': 15, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/menu'},
        }
        poster.store_telegram_updates(con, [cached_menu])
        poster.kv_set(con, 'telegram_updates_offset', '43')
        with CaptureTelegram() as calls:
            handled = poster.process_pending_admin_updates(c, con)
            handled_again = poster.process_pending_admin_updates(c, con)
        self.assertEqual(handled, 1)
        self.assertEqual(handled_again, 0)
        self.assertEqual([m for m, _ in calls], ['sendMessage'])
        self.assertIn('reply_markup', calls[0][1])
        self.assertEqual(poster.kv_get(con, 'telegram_admin_updates_offset'), '43')

    def test_pending_admin_updates_skip_broken_update_and_continue(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        updates = [
            {'update_id': 50, 'message': {'message_id': 1, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/status'}},
            {'update_id': 51, 'message': {'message_id': 2, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/panel'}},
        ]
        poster.store_telegram_updates(con, updates)
        calls = []
        orig = poster.tg_request
        def flaky_tg_request(_cfg, method, payload):
            calls.append((method, payload))
            if method == 'sendMessage' and len([m for m, _p in calls if m == 'sendMessage']) == 1:
                raise RuntimeError('stale update failed')
            return {'ok': True, 'result': {'message_id': 1}}
        poster.tg_request = flaky_tg_request
        try:
            handled = poster.process_pending_admin_updates(c, con)
        finally:
            poster.tg_request = orig
        self.assertEqual(handled, 1)
        self.assertEqual(poster.kv_get(con, 'telegram_admin_updates_offset'), '52')
        self.assertTrue(any(method == 'sendMessage' and 'reply_markup' in payload for method, payload in calls))

    def test_admin_watch_polls_telegram_and_processes_cached_admin_updates(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        c.dry_run = False
        args = type('Args', (), {'interval': 0, 'once': True, 'timeout': 0})()
        calls = []
        orig_tg = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(_cfg, method, payload):
            calls.append((method, payload))
            if method == 'getUpdates':
                return {'ok': True, 'result': [{
                    'update_id': 60,
                    'message': {'message_id': 3, 'chat': {'id': 222}, 'from': {'id': 498975827}, 'text': '/panel'},
                }]}
            return {'ok': True, 'result': {'message_id': 4}}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            rc = poster.cmd_admin_watch(c, con, args)
        finally:
            poster.tg_request = orig_tg
            poster.time.sleep = orig_sleep
        self.assertEqual(rc, 0)
        self.assertIn('getUpdates', [m for m, _ in calls])
        self.assertTrue(any(method == 'sendMessage' and 'reply_markup' in payload for method, payload in calls))
        self.assertEqual(poster.kv_get(con, 'telegram_admin_updates_offset'), '61')

    def test_admin_watch_timeout_does_not_send_dm_alert(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.telegram_token = 'tg'
        c.telegram_admin_user_id = '498975827'
        c.alert_chat_id = '498975827'
        c.dry_run = False
        args = type('Args', (), {'interval': 0, 'once': True, 'timeout': 0})()
        calls = []
        orig_tg = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(_cfg, method, payload):
            calls.append((method, payload))
            if method == 'getUpdates':
                raise TimeoutError('The read operation timed out')
            return {'ok': True, 'result': {'message_id': 4}}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            rc = poster.cmd_admin_watch(c, con, args)
        finally:
            poster.tg_request = orig_tg
            poster.time.sleep = orig_sleep
        self.assertEqual(rc, 0)
        self.assertEqual([method for method, _payload in calls], ['getUpdates'])

    def test_public_community_repost_uses_parent_content_and_media(self):
        rp = post(
            post_id=29861,
            author_name='laplace',
            author_link='laplace',
            description='',
            photos=[],
            is_repost=True,
            parent_post={
                'post_id': 29854,
                'author_name': 'литературный клуб',
                'author_link': '?community=myeyes',
                'created_at': '2026-07-22T21:27:01.449448',
                'description': 'Текст из паблика',
                'photos': [{'url': '/photousers/x.png'}],
                'audios': [],
                'is_community_post': True,
                'poll': None,
            },
        )
        self.assertTrue(poster.is_publishable(rp))
        self.assertEqual(poster.photo_urls(rp), ['https://dragonfly-flash.ru/photousers/x.png'])
        html = poster.format_html(rp)
        self.assertIn('🔄 Репост', html)
        self.assertIn('литературный клуб', html)
        self.assertIn('Текст из паблика', html)
        self.assertIn('https://dragonfly-flash.ru/?community=myeyes', html)
        self.assertNotIn('🔄 репост', html)

    def test_poll_only_post_is_publishable_and_renders_results(self):
        poll_post = post(
            description='',
            photos=[],
            poll={
                'question': 'Объявлять ли сбор?',
                'total_votes': 412,
                'options': [
                    {'text': 'Да', 'votes': 396, 'percent': 96},
                    {'text': 'Нет', 'votes': 16, 'percent': 4},
                ],
            },
        )
        self.assertTrue(poster.is_publishable(poll_post))
        html = poster.format_html(poll_post)
        self.assertIn('📊 <b>Опрос:</b> Объявлять ли сбор?', html)
        self.assertIn('1. Да — 96% (396)', html)
        self.assertIn('Всего голосов: 412', html)

    def test_community_author_link_keeps_query_url(self):
        community_post = post(author_link='?community=myeyes')
        self.assertEqual(poster.profile_url(community_post), 'https://dragonfly-flash.ru/?community=myeyes')

    def test_best_likes_threshold_is_runtime_int_setting_in_panel(self):
        con = poster.init_db(Path(':memory:'))
        text, markup = poster.build_admin_panel(con)
        keyboard_text = json.dumps(markup, ensure_ascii=False)
        self.assertIn('⭐ Лучшее от лайков', keyboard_text)
        detail, detail_markup = poster.build_setting_detail(con, 'best_likes_threshold')
        self.assertIn('Текущее значение: 7 лайков (default)', detail)
        self.assertIn('минимум лайков', detail)
        self.assertIn('10 лайков', json.dumps(detail_markup, ensure_ascii=False))

    def test_best_likes_threshold_runtime_setting_applies_to_config(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.best_likes_threshold = 7
        value = poster.set_runtime_setting(con, 'best_likes_threshold', '10')
        poster.apply_runtime_settings(c, con)
        self.assertEqual(value, 10)
        self.assertEqual(c.best_likes_threshold, 10)

    def test_sync_stats_uses_runtime_best_likes_threshold(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.best_chat_id = '-100best'
        c.best_likes_threshold = 7
        poster.record_telegram_message(con, post(post_id=779, likes_count=6, comments_count=0), chat_id='@channel', message_id=111, message_kind='text', base_html='base', role='main')
        poster.set_runtime_setting(con, 'best_likes_threshold', '10')
        calls = []
        orig_fetch = poster.fetch_recent_posts
        orig_tg = poster.tg_request
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=779, likes_count=8, comments_count=0)]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 211}}
        try:
            poster.apply_runtime_settings(c, con)
            rc = poster.cmd_sync_stats(c, con, type('Args', (), {'count': 1, 'offset': 0})())
        finally:
            poster.fetch_recent_posts = orig_fetch
            poster.tg_request = orig_tg
        self.assertEqual(rc, 0)
        self.assertEqual([m for m, _ in calls], ['editMessageText'])
        self.assertFalse(poster.is_best_post_sent(con, 779))

    def test_runtime_settings_apply_to_config_without_restart(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.request_delay = 1
        c.poll_interval = 15
        poster.set_runtime_setting(con, 'request_delay', '2.5')
        poster.set_runtime_setting(con, 'poll_interval', '7')
        poster.apply_runtime_settings(c, con)
        self.assertEqual(c.request_delay, 2.5)
        self.assertEqual(c.poll_interval, 7.0)

    def test_export_and_import_state_archive_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_db = root / 'source.sqlite3'
            source_accounts = root / 'accounts.json'
            backup = root / 'backup.tar.gz'
            con = poster.init_db(source_db)
            con.execute("INSERT INTO sent_posts(post_id, sent_at, status) VALUES (?, ?, ?)", (123, 'now', 'sent'))
            con.commit()
            con.close()
            source_accounts.write_text(json.dumps({
                'active': 'main',
                'accounts': [{'name': 'main', 'access_token': 'secret-token', 'enabled': True}],
            }), encoding='utf-8')
            c = cfg()
            c.db_path = source_db
            c.accounts_file = str(source_accounts)

            manifest = poster.export_state_archive(c, backup)

            self.assertTrue(backup.exists())
            self.assertEqual(manifest['version'], 1)
            with tarfile.open(backup, 'r:gz') as tf:
                names = set(tf.getnames())
                manifest_data = json.loads(tf.extractfile('manifest.json').read().decode('utf-8'))
            self.assertIn('state/dragonfly_telegram_poster.sqlite3', names)
            self.assertIn('secrets/dragonfly_accounts.json', names)
            self.assertNotIn('dragonfly.env', names)
            self.assertNotIn('secret-token', json.dumps(manifest_data))

            target_db = root / 'restored.sqlite3'
            target_accounts = root / 'restored_accounts.json'
            restored = poster.import_state_archive(backup, db_path=target_db, accounts_file=target_accounts)

            self.assertEqual(restored['version'], 1)
            restored_con = poster.sqlite3.connect(target_db)
            self.assertEqual(restored_con.execute('SELECT post_id FROM sent_posts').fetchone()[0], 123)
            self.assertEqual(json.loads(target_accounts.read_text())['accounts'][0]['access_token'], 'secret-token')

    def test_state_archive_cli_commands_print_safe_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_db = root / 'source.sqlite3'
            source_accounts = root / 'accounts.json'
            backup = root / 'backup.tar.gz'
            con = poster.init_db(source_db)
            con.execute("INSERT INTO sent_posts(post_id, sent_at, status) VALUES (?, ?, ?)", (456, 'now', 'sent'))
            con.commit()
            con.close()
            source_accounts.write_text(json.dumps({'active': 'main', 'accounts': [{'name': 'main', 'access_token': 'secret-token', 'enabled': True}]}), encoding='utf-8')
            c = cfg()
            c.db_path = source_db
            c.accounts_file = str(source_accounts)
            buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                self.assertEqual(poster.cmd_export_state(c, None, poster.argparse.Namespace(output=str(backup))), 0)
            finally:
                sys.stdout = old_stdout
            self.assertIn('exported state archive', buf.getvalue())
            self.assertNotIn('secret-token', buf.getvalue())

            target_db = root / 'target.sqlite3'
            target_accounts = root / 'target_accounts.json'
            import_buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = import_buf
            try:
                self.assertEqual(poster.cmd_import_state(None, None, poster.argparse.Namespace(archive=str(backup), db=str(target_db), accounts_file=str(target_accounts))), 0)
            finally:
                sys.stdout = old_stdout
            self.assertIn('imported state archive', import_buf.getvalue())
            self.assertNotIn('secret-token', import_buf.getvalue())
            self.assertTrue(target_db.exists())
            self.assertTrue(target_accounts.exists())

    def test_configure_active_account_can_pin_named_account_without_rewriting_active(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'accounts.json'
            data = {
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'tok-main', 'enabled': True},
                    {'name': 'backup_2', 'access_token': 'tok-b2', 'enabled': True},
                ],
            }
            path.write_text(json.dumps(data), encoding='utf-8')
            c = cfg()
            c.accounts_file = str(path)
            c.account_name = 'backup_2'
            c.cookie_file = '/tmp/cookies.txt'

            poster.configure_active_account(c)

            self.assertEqual(c.dragonfly_token, 'tok-b2')
            self.assertEqual(c.account_name, 'backup_2')
            self.assertTrue(c.account_pinned)
            self.assertIsNone(c.cookie_file)
            self.assertEqual(json.loads(path.read_text())['active'], 'main')
            self.assertFalse(poster.switch_dragonfly_account(c, '401'))
            self.assertEqual(json.loads(path.read_text())['active'], 'main')

    def test_gap_catchup_skip_warning_is_deduped_for_same_range(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        logs = []
        orig_log = poster.log
        poster.log = lambda message, level=poster.logging.INFO: logs.append((message, level))
        try:
            poster.catch_up_missing_ids(c, con, min_id=1, max_id=1000, known_ids=set(), max_gap_scan=10)
            poster.catch_up_missing_ids(c, con, min_id=1, max_id=1000, known_ids=set(), max_gap_scan=10)
        finally:
            poster.log = orig_log

        warnings = [m for m, level in logs if 'gap catch-up skipped' in m]
        self.assertEqual(warnings, ['gap catch-up skipped: span=1000 exceeds max_gap_scan=10'])

    def test_doctor_checks_local_migration_prerequisites_without_leaking_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts = root / 'accounts.json'
            accounts.write_text(json.dumps({
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'tok-main', 'enabled': True},
                    {'name': 'backup_1', 'access_token': 'tok-b1', 'enabled': True},
                    {'name': 'backup_2', 'access_token': 'tok-b2', 'enabled': True},
                    {'name': 'backup_3', 'access_token': 'tok-b3', 'enabled': True},
                    {'name': 'backup_4', 'access_token': 'tok-b4', 'enabled': True},
                ],
            }), encoding='utf-8')
            unit_dir = root / 'deploy' / 'systemd' / 'user'
            unit_dir.mkdir(parents=True)
            for name in poster.DOCTOR_EXPECTED_SYSTEMD_UNITS:
                (unit_dir / name).write_text('[Unit]\nDescription=test\n', encoding='utf-8')
            c = cfg()
            c.telegram_token = 'tg-secret-token'
            c.telegram_chat_id = '@channel'
            c.alert_chat_id = '123'
            c.discussion_chat_id = '-100222'
            c.best_chat_id = '-100333'
            c.accounts_file = str(accounts)
            c.db_path = root / 'state.sqlite3'
            c.log_file = str(root / 'bridge.log')

            result = poster.run_doctor(c, project_dir=root, check_network=False)

        rendered = '\n'.join(result['lines'])
        self.assertTrue(result['ok'], rendered)
        self.assertIn('OK accounts file has expected accounts', rendered)
        self.assertIn('OK sqlite path writable', rendered)
        self.assertIn('OK log path writable', rendered)
        self.assertNotIn('tok-main', rendered)
        self.assertNotIn('tg-secret-token', rendered)

    def test_doctor_network_checks_telegram_chats(self):
        c = cfg()
        c.telegram_token = 'tg-secret-token'
        c.telegram_chat_id = '@channel'
        c.discussion_chat_id = '-100222'
        c.best_chat_id = '-100333'
        c.accounts_file = None
        calls = []
        orig_tg = poster.tg_request
        orig_fetch = poster.fetch_recent_posts
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'id': payload.get('chat_id', 1)}}
        poster.fetch_recent_posts = lambda cfg, count: [post(post_id=1)]
        try:
            result = poster.run_doctor(c, project_dir=Path(__file__).parent, check_network=True)
        finally:
            poster.tg_request = orig_tg
            poster.fetch_recent_posts = orig_fetch

        self.assertTrue(result['ok'], '\n'.join(result['lines']))
        self.assertIn(('getMe', {}), calls)
        self.assertIn(('getChat', {'chat_id': '@channel'}), calls)
        self.assertIn(('getChat', {'chat_id': '-100222'}), calls)
        self.assertIn(('getChat', {'chat_id': '-100333'}), calls)

    def test_fetch_recent_posts_honors_start_offset(self):
        c = cfg()
        c.limit = 10
        seen_offsets = []
        orig = poster.fetch_feed_page
        def fake_page(cfg, offset):
            seen_offsets.append(offset)
            return [post(post_id=1000 - offset - i) for i in range(10)]
        poster.fetch_feed_page = fake_page
        try:
            rows = poster.fetch_recent_posts(c, 12, start_offset=20)
        finally:
            poster.fetch_feed_page = orig
        self.assertEqual(seen_offsets, [20, 30])
        self.assertEqual(len(rows), 12)

    def test_api_get_json_retries_transient_urlerror(self):
        calls = {'n': 0}
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.URLError('ssl eof')
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda *_: None
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(calls['n'], 2)

    def test_api_get_json_retries_http_429_with_longer_sleep(self):
        calls = {'n': 0}
        sleeps = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(
                    req.full_url,
                    429,
                    'Too Many Requests',
                    {},
                    io.BytesIO(b'too many'),
                )
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(calls['n'], 2)
        self.assertEqual(sleeps[0], poster.DEFAULT_API_429_BASE_SLEEP)

    def test_api_get_json_uses_adaptive_429_backoff(self):
        calls = {'n': 0}
        sleeps = []
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] <= 3:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=3, alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(sleeps, [30.0, 60.0, 120.0])
        self.assertIn('жду 30 сек', alerts[0])
        self.assertIn('жду 1 мин', alerts[1])
        self.assertIn('жду 2 мин', alerts[2])

    def test_api_get_json_sends_friendly_429_alert_callback(self):
        calls = {'n': 0}
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda *_: None
        try:
            poster.api_get_json('https://example.test/api/feed?type=all&limit=20&offset=120', {}, retries=1, alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(len(alerts), 1)
        self.assertIn('429', alerts[0])
        self.assertIn('жду 30 сек', alerts[0])
        self.assertIn('попытка 1/1', alerts[0])

    def test_dragonfly_headers_use_bearer_without_cookie_file(self):
        c = cfg()
        c.dragonfly_token = 'jwt-token'
        c.cookie_file = None

        headers = poster.dragonfly_headers(c)

        self.assertEqual(headers['Authorization'], 'Bearer jwt-token')
        self.assertIn('User-Agent', headers)

    def test_dragonfly_headers_omit_bearer_when_cookie_file_exists(self):
        c = cfg()
        c.dragonfly_token = 'jwt-token'
        c.cookie_file = '/tmp/dragonfly-cookies.txt'

        headers = poster.dragonfly_headers(c)

        self.assertNotIn('Authorization', headers)
        self.assertEqual(headers['Accept'], 'application/json')

    def test_api_get_json_sends_friendly_401_alert_before_raising(self):
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{"detail":"expired"}'))

        poster.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError):
                poster.api_get_json('https://example.test/api/feed', {}, retries=0, auth_alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen

        self.assertEqual(len(alerts), 1)
        self.assertIn('Авторизация Dragonfly истекла', alerts[0])
        self.assertIn('401', alerts[0])

    def test_accounts_file_switches_to_next_account_on_401(self):
        c = cfg()
        c.dragonfly_token = None
        c.cookie_file = None
        with tempfile.TemporaryDirectory() as td:
            accounts_path = Path(td) / 'accounts.json'
            accounts_path.write_text(json.dumps({
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'bad-token', 'enabled': True},
                    {'name': 'backup', 'access_token': 'good-token', 'enabled': True},
                ],
            }), encoding='utf-8')
            c.accounts_file = str(accounts_path)
            poster.configure_active_account(c)
            seen_auth = []
            orig_urlopen = poster.urllib.request.urlopen

            class Resp:
                def __enter__(self): return self
                def __exit__(self, *exc): return False
                def read(self): return b'{"feed": [{"post_id": 1}]}'

            def fake_urlopen(req, timeout):
                seen_auth.append(req.get_header('Authorization'))
                if 'bad-token' in (seen_auth[-1] or ''):
                    raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'expired'))
                return Resp()

            poster.urllib.request.urlopen = fake_urlopen
            try:
                posts = poster.fetch_feed_page(c, 0)
            finally:
                poster.urllib.request.urlopen = orig_urlopen

            self.assertEqual([p['post_id'] for p in posts], [1])
            self.assertEqual(c.account_name, 'backup')
            self.assertIn('bad-token', seen_auth[0])
            self.assertIn('good-token', seen_auth[1])
            saved = json.loads(accounts_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['active'], 'backup')

    def test_accounts_file_does_not_switch_on_429(self):
        c = cfg()
        with tempfile.TemporaryDirectory() as td:
            accounts_path = Path(td) / 'accounts.json'
            accounts_path.write_text(json.dumps({
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'main-token', 'enabled': True},
                    {'name': 'backup', 'access_token': 'backup-token', 'enabled': True},
                ],
            }), encoding='utf-8')
            c.accounts_file = str(accounts_path)
            c.cookie_file = None
            poster.configure_active_account(c)
            orig_urlopen = poster.urllib.request.urlopen
            orig_sleep = poster.time.sleep

            def fake_urlopen(req, timeout):
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))

            poster.urllib.request.urlopen = fake_urlopen
            poster.time.sleep = lambda *_: None
            try:
                with self.assertRaises(RuntimeError):
                    poster.fetch_feed_page(c, 0)
            finally:
                poster.urllib.request.urlopen = orig_urlopen
                poster.time.sleep = orig_sleep

            self.assertEqual(c.account_name, 'main')
            saved = json.loads(accounts_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['active'], 'main')

    def test_cmd_auth_check_returns_zero_when_feed_page_loads(self):
        c = cfg()
        calls = []
        orig = poster.fetch_feed_page
        poster.fetch_feed_page = lambda cfg, offset: calls.append(offset) or [post(post_id=1)]
        try:
            rc = poster.cmd_auth_check(c)
        finally:
            poster.fetch_feed_page = orig

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [0])

    def test_send_alert_uses_alert_chat_id_and_html(self):
        c = cfg()
        c.telegram_token = 'bot-token'
        c.alert_chat_id = '123456789'
        calls = []
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True}
        try:
            poster.send_alert(c, '⚠️ Тест', 'получили <429> & ждём')
        finally:
            poster.tg_request = orig

        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertEqual(calls[0][1]['chat_id'], '123456789')
        self.assertIn('⚠️ Тест', calls[0][1]['text'])
        self.assertIn('&lt;429&gt; &amp;', calls[0][1]['text'])

    def test_download_media_retries_transient_urlerror(self):
        calls = {'n': 0}
        sleeps = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            headers = {'content-type': 'image/jpeg', 'content-length': '4'}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, n=-1):
                return b'JPEG'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.URLError('ssl eof')
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            filename, data, ctype = poster.download_media_bytes('https://dragonfly-flash.ru/photousers/x.jpg', retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(filename, 'x.jpg')
        self.assertEqual(data, b'JPEG')
        self.assertEqual(ctype, 'image/jpeg')
        self.assertEqual(calls['n'], 2)
        self.assertTrue(sleeps)

    def test_header_has_linked_author_and_datetime_with_light_emoji(self):
        chunks = poster.format_html_chunks(post(description='hello'), limit=poster.MAX_TG_MESSAGE)

        self.assertIn('👤 <a href="https://dragonfly-flash.ru/?id=alice_profile">Alice &lt;A&gt;</a>', chunks[0])
        self.assertIn('🕒 <i>20.07.2026 12:00</i>', chunks[0])

    def test_text_keeps_apostrophes_and_combining_symbols_but_escapes_html(self):
        chunks = poster.format_html_chunks(post(description="Брад (ง'̀-'́)ง <script>&"), limit=poster.MAX_TG_MESSAGE)
        text = chunks[0]

        self.assertIn("(ง'̀-'́)ง", text)
        self.assertNotIn('&#x27;', text)
        self.assertIn('&lt;script&gt;&amp;', text)

    def test_text_decodes_existing_html_entities_before_safe_escape(self):
        chunks = poster.format_html_chunks(post(description="Брад (ง&#x27;̀-&#x27;́)ง &lt;b&gt;"), limit=poster.MAX_TG_MESSAGE)
        text = chunks[0]

        self.assertIn("(ง'̀-'́)ง", text)
        self.assertNotIn('&#x27;', text)
        self.assertIn('&lt;b&gt;', text)

    def test_log_file_receives_timestamped_lines(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        poster.setup_logging(path)
        try:
            poster.log('hello log file')
            text = Path(path).read_text(encoding='utf-8')
        finally:
            poster.setup_logging(None)
        self.assertIn('hello log file', text)
        self.assertIn('INFO', text)

    def test_log_file_uses_rotation_limits(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        poster.setup_logging(path)
        try:
            handlers = [h for h in poster.LOGGER.handlers if getattr(h, 'baseFilename', None) == path]
        finally:
            poster.setup_logging(None)
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].maxBytes, poster.DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(handlers[0].backupCount, poster.DEFAULT_LOG_BACKUP_COUNT)

    def test_empty_post_without_text_or_media_is_not_publishable(self):
        self.assertFalse(poster.is_publishable(post(description='   ', photos=[])))
        self.assertTrue(poster.is_publishable(post(description='text', photos=[])))
        self.assertTrue(poster.is_publishable(post(description='   ', photos=[{'url': '/x.jpg'}])))

    def test_audio_only_post_is_publishable_as_track_info(self):
        p = post(description='', photos=[], audios=[{'artist': 'Импакт', 'title': 'Социопат', 'file_path': 'audio.mp3'}])

        self.assertTrue(poster.is_publishable(p))
        chunks = poster.format_html_chunks(p, limit=poster.MAX_TG_MESSAGE)
        self.assertIn('🎵 <b>Трек:</b> Импакт — Социопат', '\n'.join(chunks))

    def test_large_uploaded_photo_is_sent_as_document_instead_of_retrying_photo(self):
        c = cfg()
        c.upload_media = True
        calls = []
        orig_download = poster.download_media_bytes
        orig_multipart = poster.tg_multipart_request
        poster.download_media_bytes = lambda _url: ('large.jpg', b'x' * (poster.MAX_TG_PHOTO_BYTES + 1), 'image/jpeg')
        def fake_multipart(_cfg, method, fields, files):
            calls.append((method, fields, files))
            return {'ok': True, 'result': {'message_id': 801}}
        poster.tg_multipart_request = fake_multipart
        try:
            resp = poster.send_one_media(c, 'https://dragonfly-flash.ru/photousers/large.jpg', caption='caption')
        finally:
            poster.download_media_bytes = orig_download
            poster.tg_multipart_request = orig_multipart

        self.assertEqual(resp['result']['message_id'], 801)
        self.assertEqual(calls[0][0], 'sendDocument')
        self.assertIn('document', calls[0][2])
        self.assertEqual(calls[0][1]['caption'], 'caption')

    def test_remote_photo_size_rejection_retries_as_document_url(self):
        c = cfg()
        c.upload_media = False
        calls = []
        orig = poster.tg_request
        def fake_tg_request(_cfg, method, payload):
            calls.append((method, payload))
            if method == 'sendPhoto':
                raise RuntimeError('Telegram HTTP 400: {"description":"Bad Request: file of size 15735769 bytes is too big for a photo; the maximum size is 10485760 bytes"}')
            return {'ok': True, 'result': {'message_id': 802}}
        poster.tg_request = fake_tg_request
        try:
            resp = poster.send_one_media(c, 'https://dragonfly-flash.ru/photousers/large.jpg', caption='caption')
        finally:
            poster.tg_request = orig

        self.assertEqual(resp['result']['message_id'], 802)
        self.assertEqual([m for m, _ in calls], ['sendPhoto', 'sendDocument'])
        self.assertEqual(calls[1][1]['document'], 'https://dragonfly-flash.ru/photousers/large.jpg')
        self.assertEqual(calls[1][1]['caption'], 'caption')

    def test_long_text_post_is_split_into_bounded_messages_with_continuation(self):
        long_text = 'абвгд ' * 900
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description=long_text, photos=[]))

        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(method == 'sendMessage' for method, _ in calls))
        self.assertTrue(all(len(payload['text']) <= poster.MAX_TG_MESSAGE for _, payload in calls))
        self.assertIn('(1/', calls[0][1]['text'])
        self.assertIn('(2/', calls[1][1]['text'])
        self.assertNotIn('продолжение поста', calls[0][1]['text'])
        self.assertTrue(calls[1][1]['text'].startswith('продолжение поста #123\n\n'))
        self.assertNotIn('👤', calls[1][1]['text'].split('\n\n', 1)[0])
        self.assertNotIn('🕒', calls[1][1]['text'].split('\n\n', 1)[0])
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_long_media_caption_is_split_caption_then_followup_messages(self):
        long_text = 'длинный текст ' * 240
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description=long_text, photos=[{'url': '/p.jpg'}]))

        self.assertEqual(calls[0][0], 'sendPhoto')
        self.assertLessEqual(len(calls[0][1]['caption']), poster.MAX_TG_CAPTION)
        self.assertIn('(1/', calls[0][1]['caption'])
        self.assertGreaterEqual(len(calls), 2)
        for _method, payload in calls:
            body = payload.get('text', payload.get('caption', ''))
            limit = poster.MAX_TG_CAPTION if 'caption' in payload else poster.MAX_TG_MESSAGE
            self.assertLessEqual(len(body), limit)
        self.assertEqual(calls[-1][0], 'sendMessage')
        self.assertTrue(calls[-1][1]['text'].startswith('продолжение поста #123\n\n'))
        self.assertNotIn('👤', calls[-1][1]['text'].split('\n\n', 1)[0])
        self.assertNotIn('🕒', calls[-1][1]['text'].split('\n\n', 1)[0])
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_photo_count_is_not_rendered_in_caption(self):
        text = poster.format_html_chunks(post(description='hello', photos=[{'url': '/p.jpg'}]), limit=poster.MAX_TG_CAPTION)[0]

        self.assertNotIn('📷 1', text)

    def test_audio_info_is_rendered_and_counted_inside_caption_limit(self):
        p = post(
            description='hello ' * 220,
            photos=[{'url': '/p.jpg'}],
            audios=[{
                'artist': 'Massive Attack',
                'title': 'Angel',
                'duration': 379,
                'file_path': 'music/massive_attack_angel.mp3',
            }],
        )

        chunks = poster.format_html_chunks(p, limit=poster.MAX_TG_CAPTION)

        self.assertTrue(all(len(c) <= poster.MAX_TG_CAPTION for c in chunks))
        joined = '\n'.join(chunks)
        self.assertIn('🎵', joined)
        self.assertIn('Massive Attack — Angel', joined)
        self.assertIn('6:19', joined)
        self.assertNotIn('massive_attack_angel.mp3', joined)
        self.assertNotIn('audio_', joined)

    def test_dry_run_does_not_mark_posts_sent(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = True
        with CaptureTelegram():
            processed = poster.send_new_posts(c, con, [post(post_id=555, description='hello')])

        self.assertEqual(processed, 1)
        self.assertFalse(poster.is_sent(con, 555))

    def test_post_delay_depends_on_post_media_type(self):
        c = cfg()
        c.text_delay = 1
        c.photo_delay = 2
        c.album_delay = 3
        c.animation_delay = 4
        c.mixed_media_delay = 5

        self.assertEqual(poster.delay_for_post(c, post(description='text', photos=[])), 1)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.jpg'}])), 2)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/1.jpg'}, {'url': '/2.jpg'}])), 3)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.gif'}])), 4)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.gif'}, {'url': '/y.jpg'}])), 5)

    def test_send_new_posts_uses_type_specific_delay(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.text_delay = 1
        c.photo_delay = 2
        c.animation_delay = 4
        sleeps = []
        sent = []
        orig_send = poster.send_post
        orig_sleep = poster.time.sleep

        def fake_send(cfg, p, **kwargs):
            sent.append(int(p['post_id']))

        poster.send_post = fake_send
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            processed = poster.send_new_posts(c, con, [
                post(post_id=3, description='gif', photos=[{'url': '/x.gif'}]),
                post(post_id=2, description='photo', photos=[{'url': '/x.jpg'}]),
                post(post_id=1, description='text', photos=[]),
            ])
        finally:
            poster.send_post = orig_send
            poster.time.sleep = orig_sleep

        self.assertEqual(processed, 3)
        self.assertEqual(sent, [1, 2, 3])
        self.assertEqual(sleeps, [1, 2, 4])

    def test_catch_up_gaps_fetches_missing_id_and_sends_existing_post(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        seen_fetches = []
        sent = []
        orig_fetch = poster.fetch_post_by_id
        orig_send = poster.send_post
        orig_sleep = poster.time.sleep

        def fake_fetch(cfg, pid):
            seen_fetches.append(pid)
            return post(post_id=pid, description=f'missing {pid}') if pid == 102 else None

        def fake_send(cfg, p, **kwargs):
            sent.append(int(p['post_id']))

        poster.fetch_post_by_id = fake_fetch
        poster.send_post = fake_send
        poster.time.sleep = lambda *_: None
        try:
            processed = poster.catch_up_missing_ids(c, con, min_id=100, max_id=104, known_ids={100, 101, 103, 104})
        finally:
            poster.fetch_post_by_id = orig_fetch
            poster.send_post = orig_send
            poster.time.sleep = orig_sleep

        self.assertEqual(seen_fetches, [102])
        self.assertEqual(sent, [102])
        self.assertEqual(processed, 1)
        self.assertTrue(poster.is_sent(con, 102))

    def test_media_failure_falls_back_to_text_with_original_link(self):
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            poster.send_post(cfg(), post(description='text', photos=[{'url': '/bad.jpg'}]))
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(calls[-1][0], 'sendMessage')
        self.assertIn('⚠️ Медиа не отправилось', calls[-1][1]['text'])
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_media_fallback_does_not_dump_rendered_html(self):
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        rendered = '<div class="post-header"><img src="/x.jpg"><div class="post-footer">Мне нравится 1</div></div>'
        try:
            poster.send_post(cfg(), post(post_id=24974, description=rendered, photos=[{'url': '/bad.jpg'}]))
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        body = calls[-1][1]['text']
        self.assertIn('⚠️ Медиа не отправилось', body)
        self.assertIn('>#24974</a>', body)
        self.assertNotIn('post-header', body)
        self.assertNotIn('&lt;div', body)

    def test_send_new_posts_retries_media_before_text_fallback(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.max_attempts = 3
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True, 'result': {'message_id': 700}}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            for _ in range(2):
                poster.send_new_posts(c, con, [post(post_id=903, description='text', photos=[{'url': '/bad.jpg'}])])
            self.assertFalse(poster.is_sent(con, 903))
            self.assertEqual(poster.failed_attempts(con, 903), 2)
            poster.send_new_posts(c, con, [post(post_id=903, description='text', photos=[{'url': '/bad.jpg'}])])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertTrue(poster.is_sent(con, 903))
        self.assertTrue(any(method == 'sendMessage' and '⚠️ Медиа не отправилось' in payload.get('text', '') for method, payload in calls))

    def test_send_new_posts_records_telegram_message_mapping(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep

        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            return {'ok': True, 'result': {'message_id': 4242}}

        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            n = poster.send_new_posts(c, con, [post(post_id=888, description='hello', likes_count=2, comments_count=1)])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(n, 1)
        row = con.execute("SELECT message_id, message_kind, last_likes, last_comments FROM telegram_messages WHERE post_id=888 AND role='main'").fetchone()
        self.assertEqual(row, (4242, 'text', 2, 1))
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertIn('❤️ 2', calls[0][1]['text'])
        self.assertIn('💬 1', calls[0][1]['text'])
        stored = con.execute("SELECT base_html FROM telegram_messages WHERE post_id=888 AND role='main'").fetchone()[0]
        self.assertNotIn('❤️ 2', stored)

    def test_initial_zero_stats_footer_is_sent(self):
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description='hello', likes_count=0, comments_count=0))

        self.assertIn('❤️ 0', calls[0][1]['text'])
        self.assertIn('💬 0', calls[0][1]['text'])

    def test_discussion_mapping_detects_automatic_forward_update(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.discussion_chat_id = '-100222'
        updates = [{
            'update_id': 10,
            'message': {
                'message_id': 777,
                'chat': {'id': -100222},
                'is_automatic_forward': True,
                'forward_from_chat': {'id': -100111},
                'forward_from_message_id': 555,
            },
        }]
        poster.store_telegram_updates(con, updates)
        did = poster.try_capture_discussion_mapping(c, con, post_id=901, role='last', channel_message_id=555, wait_seconds=0)

        self.assertEqual(did, 777)
        row = con.execute("SELECT discussion_chat_id, discussion_message_id FROM telegram_discussion_messages WHERE post_id=901 AND role='last'").fetchone()
        self.assertEqual(row, ('-100222', 777))

    def test_repair_discussion_mapping_recovers_missing_last_mapping_from_cache(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.discussion_chat_id = '-100222'
        poster.record_telegram_message(
            con,
            post(post_id=909),
            chat_id='@channel',
            message_id=556,
            message_kind='text',
            base_html='base',
            role='last',
        )
        poster.store_telegram_updates(con, [{
            'update_id': 12,
            'message': {
                'message_id': 778,
                'chat': {'id': -100222},
                'is_automatic_forward': True,
                'forward_origin': {'message_id': 556, 'chat': {'id': -100111}},
            },
        }])
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: (_ for _ in ()).throw(AssertionError('repair must not call getUpdates'))
        try:
            rc = poster.cmd_repair_discussion_mapping(c, con, type('Args', (), {'count': 10, 'wait_seconds': 0, 'update_timeout': 0})())
        finally:
            poster.tg_request = orig

        self.assertEqual(rc, 0)
        row = con.execute("SELECT discussion_message_id FROM telegram_discussion_messages WHERE post_id=909 AND role='last'").fetchone()
        self.assertEqual(row, (778,))

    def test_repair_discussion_mapping_uses_one_cached_updates_snapshot_for_many_rows(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.discussion_chat_id = '-100222'
        for pid, mid in [(911, 601), (912, 602)]:
            poster.record_telegram_message(
                con,
                post(post_id=pid),
                chat_id='@channel',
                message_id=mid,
                message_kind='text',
                base_html='base',
                role='last',
            )
        updates = [
            {'update_id': 20, 'message': {'message_id': 801, 'chat': {'id': -100222}, 'is_automatic_forward': True, 'forward_from_message_id': 601}},
            {'update_id': 21, 'message': {'message_id': 802, 'chat': {'id': -100222}, 'is_automatic_forward': True, 'forward_from_message_id': 602}},
        ]
        poster.store_telegram_updates(con, updates)
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: (_ for _ in ()).throw(AssertionError('repair must not call getUpdates'))
        try:
            rc = poster.cmd_repair_discussion_mapping(c, con, type('Args', (), {'count': 10, 'wait_seconds': 0, 'update_timeout': 0})())
        finally:
            poster.tg_request = orig

        self.assertEqual(rc, 0)
        rows = con.execute("SELECT post_id, discussion_message_id FROM telegram_discussion_messages ORDER BY post_id").fetchall()
        self.assertEqual(rows, [(911, 801), (912, 802)])

    def test_sync_comments_repairs_cached_discussion_mapping_before_fetching_comments(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.discussion_chat_id = '-100222'
        poster.record_telegram_message(
            con,
            post(post_id=913),
            chat_id='@channel',
            message_id=603,
            message_kind='text',
            base_html='base',
            role='last',
        )
        poster.store_telegram_updates(con, [{
            'update_id': 30,
            'message': {
                'message_id': 803,
                'chat': {'id': -100222},
                'is_automatic_forward': True,
                'forward_from_message_id': 603,
            },
        }])
        calls = []
        orig_posts = poster.get_recent_posts_for_command
        orig_fetch = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.get_recent_posts_for_command = lambda cfg, con, args: [post(post_id=913, comments_count=1)]
        poster.fetch_post_comments = lambda cfg, pid: [{'id': 42, 'username': 'Bob', 'text': 'new', 'created_at': '2026-07-21T10:01:00', 'likes_count': 0}]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 904}}
        try:
            rc = poster.cmd_sync_comments(c, con, type('Args', (), {'count': 1, 'offset': 0, 'hot_count': 20, 'send_existing': True, 'use_feed_cache': True})())
        finally:
            poster.get_recent_posts_for_command = orig_posts
            poster.fetch_post_comments = orig_fetch
            poster.tg_request = orig_tg

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertEqual(calls[0][1]['reply_to_message_id'], 803)
        row = con.execute("SELECT discussion_message_id FROM telegram_discussion_messages WHERE post_id=913 AND role='last'").fetchone()
        self.assertEqual(row, (803,))

    def test_sync_comments_seeds_existing_then_sends_new_comment(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=902,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        comments_round_1 = [{'id': 1, 'username': 'Alice', 'text': 'old', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}]
        comments_round_2 = comments_round_1 + [{'id': 2, 'username': 'Bob', 'text': '<new>', 'created_at': '2026-07-21T10:01:00', 'likes_count': 3}]
        calls = []
        orig_fetch = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_post_comments = lambda cfg, pid: comments_round_1
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 900}}
        try:
            sent, marked = poster.sync_post_comments(c, con, post(post_id=902), send_existing=False)
            poster.fetch_post_comments = lambda cfg, pid: comments_round_2
            sent2, marked2 = poster.sync_post_comments(c, con, post(post_id=902), send_existing=False)
        finally:
            poster.fetch_post_comments = orig_fetch
            poster.tg_request = orig_tg

        self.assertEqual((sent, marked), (0, 1))
        self.assertEqual((sent2, marked2), (1, 0))
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertEqual(calls[0][1]['chat_id'], '-100222')
        self.assertEqual(calls[0][1]['reply_to_message_id'], 777)
        self.assertIn('&lt;new&gt;', calls[0][1]['text'])
        rows = con.execute('SELECT comment_id, telegram_message_id FROM dragonfly_comments WHERE post_id=902 ORDER BY comment_id').fetchall()
        self.assertEqual(rows, [(1, None), (2, 900)])

    def test_sync_comments_send_existing_posts_seeded_rows(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=904,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        comment = {'id': 10, 'username': 'Alice', 'text': 'seeded', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}
        poster.mark_comment_sent(con, post_id=904, comment=comment, telegram_chat_id=None, telegram_message_id=None)
        calls = []
        orig_fetch = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_post_comments = lambda cfg, pid: [comment]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 901}}
        try:
            sent, marked = poster.sync_post_comments(c, con, post(post_id=904), send_existing=True)
        finally:
            poster.fetch_post_comments = orig_fetch
            poster.tg_request = orig_tg

        self.assertEqual((sent, marked), (1, 0))
        self.assertEqual(calls[0][1]['reply_to_message_id'], 777)
        row = con.execute('SELECT telegram_message_id FROM dragonfly_comments WHERE post_id=904 AND comment_id=10').fetchone()
        self.assertEqual(row, (901,))

    def test_reserve_comment_for_send_allows_only_one_in_flight_sender(self):
        con = poster.init_db(Path(':memory:'))
        comment = {'id': 30, 'username': 'Alice', 'text': 'race', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}

        first = poster.reserve_comment_for_send(con, post_id=908, comment=comment)
        second = poster.reserve_comment_for_send(con, post_id=908, comment=comment)

        self.assertTrue(first)
        self.assertFalse(second)
        rows = con.execute('SELECT comment_id, telegram_message_id FROM dragonfly_comments WHERE post_id=908').fetchall()
        self.assertEqual(rows, [(30, poster.COMMENT_SEND_RESERVED_MESSAGE_ID)])

    def test_release_stale_comment_reservations_only_clears_old_inflight_rows(self):
        con = poster.init_db(Path(':memory:'))
        old_comment = {'id': 31, 'username': 'Alice', 'text': 'old race', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}
        fresh_comment = {'id': 32, 'username': 'Bob', 'text': 'fresh race', 'created_at': '2026-07-21T10:01:00', 'likes_count': 0}
        poster.reserve_comment_for_send(con, post_id=908, comment=old_comment)
        poster.reserve_comment_for_send(con, post_id=908, comment=fresh_comment)
        con.execute(
            "UPDATE dragonfly_comments SET sent_at = '2026-07-21T10:00:00+00:00' WHERE comment_id = 31"
        )
        con.commit()

        released = poster.release_stale_comment_reservations(con, max_age_seconds=60, now='2026-07-21T10:10:00+00:00')

        self.assertEqual(released, 1)
        rows = con.execute('SELECT comment_id, telegram_message_id FROM dragonfly_comments WHERE post_id=908 ORDER BY comment_id').fetchall()
        self.assertEqual(rows, [(31, None), (32, poster.COMMENT_SEND_RESERVED_MESSAGE_ID)])

    def test_sync_comments_skips_old_post_when_feed_comment_count_unchanged(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=906,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        poster.record_telegram_message(
            con,
            post(post_id=906, likes_count=1, comments_count=2),
            chat_id='@channel',
            message_id=500,
            message_kind='text',
            base_html='base',
            role='main',
        )
        for cid, msg_id in [(1, 901), (2, 902)]:
            poster.mark_comment_sent(con, post_id=906, comment={'id': cid, 'username': 'u', 'text': str(cid)}, telegram_chat_id='-100', telegram_message_id=msg_id)
        fetch_calls = []
        orig_fetch_posts = poster.fetch_recent_posts
        orig_fetch_comments = poster.fetch_post_comments
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=906, likes_count=1, comments_count=2)]
        poster.fetch_post_comments = lambda cfg, pid: fetch_calls.append(pid) or []
        try:
            rc = poster.cmd_sync_comments(c, con, type('Args', (), {'count': 1, 'offset': 20, 'send_existing': True, 'hot_count': 10})())
        finally:
            poster.fetch_recent_posts = orig_fetch_posts
            poster.fetch_post_comments = orig_fetch_comments

        self.assertEqual(rc, 0)
        self.assertEqual(fetch_calls, [])

    def test_sync_comments_fetches_old_post_when_seeded_comment_unsent(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=907,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        poster.record_telegram_message(
            con,
            post(post_id=907, likes_count=1, comments_count=1),
            chat_id='@channel',
            message_id=500,
            message_kind='text',
            base_html='base',
            role='main',
        )
        comment = {'id': 21, 'username': 'Alice', 'text': 'seeded', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}
        poster.mark_comment_sent(con, post_id=907, comment=comment, telegram_chat_id=None, telegram_message_id=None)
        fetch_calls = []
        tg_calls = []
        orig_fetch_posts = poster.fetch_recent_posts
        orig_fetch_comments = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=907, likes_count=1, comments_count=1)]
        poster.fetch_post_comments = lambda cfg, pid: fetch_calls.append(pid) or [comment]
        poster.tg_request = lambda cfg, method, payload: tg_calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 903}}
        try:
            rc = poster.cmd_sync_comments(c, con, type('Args', (), {'count': 1, 'offset': 20, 'send_existing': True, 'hot_count': 10})())
        finally:
            poster.fetch_recent_posts = orig_fetch_posts
            poster.fetch_post_comments = orig_fetch_comments
            poster.tg_request = orig_tg

        self.assertEqual(rc, 0)
        self.assertEqual(fetch_calls, [907])
        self.assertEqual(tg_calls[0][0], 'sendMessage')

    def test_sync_comments_updates_stats_footer_after_sending_comment(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=905,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        poster.record_telegram_message(
            con,
            post(post_id=905, likes_count=2, comments_count=0),
            chat_id='@channel',
            message_id=500,
            message_kind='text',
            base_html='base',
            role='main',
        )
        calls = []
        orig_fetch_posts = poster.fetch_recent_posts
        orig_fetch_comments = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=905, likes_count=2, comments_count=0)]
        poster.fetch_post_comments = lambda cfg, pid: [{'id': 20, 'username': 'Alice', 'text': 'new', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 902}}
        try:
            rc = poster.cmd_sync_comments(c, con, type('Args', (), {'count': 1, 'offset': 0, 'send_existing': True})())
        finally:
            poster.fetch_recent_posts = orig_fetch_posts
            poster.fetch_post_comments = orig_fetch_comments
            poster.tg_request = orig_tg

        self.assertEqual(rc, 0)
        self.assertEqual([m for m, _ in calls], ['sendMessage', 'editMessageText'])
        self.assertIn('💬 1', calls[1][1]['text'])
        row = con.execute("SELECT last_comments FROM telegram_messages WHERE post_id=905 AND role='main'").fetchone()
        self.assertEqual(row, (1,))

    def test_sync_post_stats_edits_text_when_counts_change(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        poster.mark_sent(con, post(post_id=889))
        poster.record_telegram_message(
            con,
            post(post_id=889, likes_count=2, comments_count=1),
            chat_id='@channel',
            message_id=500,
            message_kind='text',
            base_html='base <b>html</b>',
        )
        calls = []
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 500}}
        try:
            changed = poster.sync_post_stats(c, con, post(post_id=889, likes_count=3, comments_count=4))
        finally:
            poster.tg_request = orig

        self.assertTrue(changed)
        self.assertEqual(calls[0][0], 'editMessageText')
        self.assertEqual(calls[0][1]['message_id'], 500)
        self.assertIn('❤️ 3', calls[0][1]['text'])
        self.assertIn('💬 4', calls[0][1]['text'])
        row = con.execute("SELECT last_likes, last_comments FROM telegram_messages WHERE post_id=889").fetchone()
        self.assertEqual(row, (3, 4))

    def test_sync_post_stats_never_shows_fewer_comments_than_mirrored(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        poster.record_telegram_message(
            con,
            post(post_id=891, likes_count=1, comments_count=2),
            chat_id='@channel',
            message_id=501,
            message_kind='text',
            base_html='base',
        )
        for cid, msg_id in [(1, 901), (2, 902), (3, 903)]:
            poster.mark_comment_sent(con, post_id=891, comment={'id': cid, 'username': 'u', 'text': str(cid)}, telegram_chat_id='-100', telegram_message_id=msg_id)
        calls = []
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 501}}
        try:
            changed = poster.sync_post_stats(c, con, post(post_id=891, likes_count=1, comments_count=2))
        finally:
            poster.tg_request = orig

        self.assertTrue(changed)
        self.assertIn('💬 3', calls[0][1]['text'])
        row = con.execute("SELECT last_comments FROM telegram_messages WHERE post_id=891").fetchone()
        self.assertEqual(row, (3,))

    def test_sync_post_stats_suppresses_alert_for_transient_edit_network_error(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        poster.record_telegram_message(
            con,
            post(post_id=892, likes_count=1, comments_count=1),
            chat_id='@channel',
            message_id=502,
            message_kind='text',
            base_html='base',
        )
        alerts = []
        orig_tg = poster.tg_request
        orig_alert = poster.send_alert
        poster.tg_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError('handshake operation timed out'))
        poster.send_alert = lambda *_args, **_kwargs: alerts.append((_args, _kwargs))
        try:
            changed = poster.sync_post_stats(c, con, post(post_id=892, likes_count=2, comments_count=1))
        finally:
            poster.tg_request = orig_tg
            poster.send_alert = orig_alert

        self.assertFalse(changed)
        self.assertEqual(alerts, [])
        row = con.execute("SELECT last_likes, last_error FROM telegram_messages WHERE post_id=892").fetchone()
        self.assertEqual(row[0], 1)
        self.assertIn('handshake operation timed out', row[1])

    def test_sync_stats_forwards_best_posts_when_configured(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.best_chat_id = '-100best'
        c.best_likes_threshold = 7
        poster.record_telegram_message(con, post(post_id=778, likes_count=6, comments_count=0), chat_id='@channel', message_id=110, message_kind='text', base_html='base', role='main')
        calls = []
        orig_fetch = poster.fetch_recent_posts
        orig_tg = poster.tg_request
        orig_sleep = poster.time.sleep
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=778, likes_count=8, comments_count=0)]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 210}}
        poster.time.sleep = lambda *_: None
        try:
            rc = poster.cmd_sync_stats(c, con, type('Args', (), {'count': 1, 'offset': 0})())
        finally:
            poster.fetch_recent_posts = orig_fetch
            poster.tg_request = orig_tg
            poster.time.sleep = orig_sleep

        self.assertEqual(rc, 0)
        self.assertEqual([m for m, _ in calls], ['editMessageText', 'forwardMessage'])
        self.assertTrue(poster.is_best_post_sent(con, 778))

    def test_cmd_sync_best_uses_runtime_threshold_when_present(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.best_chat_id = '-100best'
        c.telegram_token = 'tg'
        c.best_likes_threshold = 7
        poster.record_telegram_message(con, post(post_id=780, likes_count=6), chat_id='@channel', message_id=112, message_kind='text', base_html='base', role='main')
        poster.set_runtime_setting(con, 'best_likes_threshold', '10')
        calls = []
        orig_fetch = poster.fetch_recent_posts
        orig_tg = poster.tg_request
        poster.fetch_recent_posts = lambda cfg, count, start_offset=0: [post(post_id=780, likes_count=8)]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 212}}
        try:
            rc = poster.cmd_sync_best(c, con, type('Args', (), {'count': 1, 'offset': 0, 'threshold': 7})())
        finally:
            poster.fetch_recent_posts = orig_fetch
            poster.tg_request = orig_tg
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertFalse(poster.is_best_post_sent(con, 780))

    def test_sync_best_post_forwards_once_and_records_dedupe(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.best_chat_id = '-100best'
        c.send_delay = 0
        poster.record_telegram_message(con, post(post_id=777, likes_count=7), chat_id='@channel', message_id=100, message_kind='text', base_html='main', role='main')
        poster.record_telegram_message(con, post(post_id=777, likes_count=7), chat_id='@channel', message_id=101, message_kind='text', base_html='last', role='last')
        calls = []
        seq = {'message_id': 200}
        orig = poster.tg_request
        def fake_best_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method == 'forwardMessages':
                return {'ok': True, 'result': [{'message_id': 201}, {'message_id': 202}]}
            seq['message_id'] += 1
            return {'ok': True, 'result': {'message_id': seq['message_id']}}
        poster.tg_request = fake_best_tg_request
        try:
            self.assertTrue(poster.sync_best_post(c, con, post(post_id=777, likes_count=7), threshold=7))
            self.assertFalse(poster.sync_best_post(c, con, post(post_id=777, likes_count=9), threshold=7))
        finally:
            poster.tg_request = orig

        self.assertEqual([m for m, _p in calls], ['forwardMessages'])
        self.assertEqual(calls[0][1]['chat_id'], '-100best')
        self.assertEqual(calls[0][1]['message_ids'], [100, 101])
        row = con.execute('SELECT likes_at_send, source_message_ids, best_message_ids FROM best_posts WHERE post_id=777').fetchone()
        self.assertEqual(row[0], 7)
        self.assertEqual(json.loads(row[1]), [100, 101])
        self.assertEqual(json.loads(row[2]), [201, 202])

    def test_sync_best_post_ignores_posts_below_threshold(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.best_chat_id = '-100best'
        poster.record_telegram_message(con, post(post_id=778, likes_count=6), chat_id='@channel', message_id=100, message_kind='text', base_html='main', role='main')
        with CaptureTelegram() as calls:
            self.assertFalse(poster.sync_best_post(c, con, post(post_id=778, likes_count=6), threshold=7))
        self.assertEqual(calls, [])

    def test_long_split_post_records_last_role_for_final_part(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        seq = {'message_id': 100}
        orig = poster.tg_request
        orig_sleep = poster.time.sleep

        def fake_tg_request(cfg, method, payload):
            seq['message_id'] += 1
            return {'ok': True, 'result': {'message_id': seq['message_id']}}

        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            n = poster.send_new_posts(c, con, [post(post_id=890, description='абвгд ' * 900)])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(n, 1)
        rows = dict(con.execute("SELECT role, message_id FROM telegram_messages WHERE post_id=890").fetchall())
        self.assertIn('main', rows)
        self.assertIn('last', rows)
        self.assertLess(rows['main'], rows['last'])

    def test_failed_posts_stop_after_attempt_limit(self):
        con = poster.init_db(Path(':memory:'))
        p = post(post_id=777, description='text')
        for _ in range(poster.DEFAULT_MAX_ATTEMPTS):
            poster.mark_failed(con, p, 'boom')

        self.assertTrue(poster.is_exhausted(con, 777, poster.DEFAULT_MAX_ATTEMPTS))

    def test_cleanup_keeps_recent_sent_and_preserves_failed(self):
        con = poster.init_db(Path(':memory:'))
        for pid in range(1, 8):
            poster.mark_sent(con, post(post_id=pid))
        poster.mark_failed(con, post(post_id=99), 'boom')

        deleted = poster.cleanup_sent(con, keep_sent=3)

        self.assertEqual(deleted, 4)
        remaining_sent = [r[0] for r in con.execute("select post_id from sent_posts where status='sent' order by post_id").fetchall()]
        self.assertEqual(remaining_sent, [5, 6, 7])
        failed = con.execute("select post_id from sent_posts where status='failed'").fetchall()
        self.assertEqual(failed, [(99,)])

    def test_more_than_ten_photos_are_sent_as_multiple_media_groups(self):
        photos = [{'url': f'/p{i}.jpg'} for i in range(23)]
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description='album', photos=photos))

        groups = [payload for method, payload in calls if method == 'sendMediaGroup']
        self.assertEqual([len(g['media']) for g in groups], [10, 10, 3])
        self.assertIn('caption', groups[0]['media'][0])
        for g in groups[1:]:
            for item in g['media']:
                self.assertNotIn('caption', item)


if __name__ == '__main__':
    unittest.main(verbosity=2)

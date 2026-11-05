"""OAuth regression tests. Provider HTTP is mocked; no live Feishu credentials needed."""
import base64
import hashlib
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .feishu import COOKIE_NAME, TOKEN_URL, USER_INFO_URL
from django.contrib.auth.models import User
from projects.models import Project, ProjectMember
from .models import FeishuIdentity, FeishuLoginAttempt


@override_settings(
    FEISHU_LOGIN_ENABLED=True, FEISHU_APP_ID='cli_test', FEISHU_APP_SECRET='test-only-secret',
    FEISHU_REDIRECT_URI='https://testhub.example/auth/feishu/callback',
    FEISHU_ALLOWED_TENANT_KEYS=['tenant_allowed'], FEISHU_AUTO_CREATE_USER=True,
)
class FeishuLoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.profile = {'open_id': 'ou_employee', 'tenant_key': 'tenant_allowed', 'name': '测试成员'}
        self.post_mock = patch('accounts.feishu.requests.post').start()
        self.get_mock = patch('accounts.feishu.requests.get').start()
        self.addCleanup(patch.stopall)
        self.post_mock.return_value = Mock(status_code=200, json=Mock(return_value={
            'code': 0, 'access_token': 'provider-token-must-stay-server-side',
        }))
        self.get_mock.return_value = Mock(status_code=200, json=Mock(return_value={
            'code': 0, 'data': self.profile,
        }))

    def start(self):
        response = self.client.post('/api/accounts/feishu/authorize/', HTTP_ORIGIN='https://testhub.example')
        self.assertEqual(response.status_code, 200, response.data)
        query = parse_qs(urlsplit(response.data['authorize_url']).query)
        return query['state'][0], response, query

    def finish(self, state, **kwargs):
        return self.client.post('/api/accounts/feishu/callback/',
                                {'state': state, 'code': 'provider-code', **kwargs}, format='json')

    def test_config_never_exposes_credentials(self):
        response = self.client.get('/api/accounts/feishu/config/')
        self.assertEqual(response.data, {'enabled': True, 'message': ''})
        self.assertIn('no-store', response['Cache-Control'])

    def test_missing_configuration_fails_closed(self):
        for name, value in [('FEISHU_LOGIN_ENABLED', False), ('FEISHU_APP_ID', ''),
                            ('FEISHU_APP_SECRET', ''), ('FEISHU_ALLOWED_TENANT_KEYS', []),
                            ('FEISHU_REDIRECT_URI', '')]:
            with self.subTest(name=name), override_settings(**{name: value}):
                self.assertFalse(self.client.get('/api/accounts/feishu/config/').data['enabled'])
                self.assertEqual(self.client.post('/api/accounts/feishu/authorize/').status_code, 503)
                self.assertEqual(self.finish('state').status_code, 503)
        self.post_mock.assert_not_called()

    def test_invalid_or_insecure_callback_config_is_disabled(self):
        for uri in ['http://testhub.example/auth/feishu/callback', 'https://bad[',
                    'https://testhub.example/login', 'https://testhub.example/auth/feishu/callback?next=evil']:
            with self.subTest(uri=uri), override_settings(DEBUG=False, FEISHU_REDIRECT_URI=uri):
                self.assertFalse(self.client.get('/api/accounts/feishu/config/').data['enabled'])
        with override_settings(DEBUG=True, FEISHU_REDIRECT_URI='http://127.0.0.1:3001/auth/feishu/callback'):
            self.assertTrue(self.client.get('/api/accounts/feishu/config/').data['enabled'])

    def test_authorization_is_bound_to_browser_and_pkce(self):
        state, response, query = self.start()
        attempt = FeishuLoginAttempt.objects.get()
        self.assertEqual(attempt.state_hash, hashlib.sha256(state.encode()).hexdigest())
        expected = base64.urlsafe_b64encode(hashlib.sha256(attempt.code_verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(query['code_challenge'], [expected])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['prompt'], ['consent'])
        self.assertNotIn('client_secret', query)
        self.assertNotIn('code_verifier', query)
        self.assertTrue(response.cookies[COOKIE_NAME]['httponly'])
        self.assertTrue(response.cookies[COOKIE_NAME]['secure'])
        self.assertEqual(response.cookies[COOKIE_NAME]['samesite'], 'Lax')

    def test_wrong_origin_cannot_start(self):
        response = self.client.post('/api/accounts/feishu/authorize/', HTTP_ORIGIN='https://attacker.example')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FeishuLoginAttempt.objects.exists())

    def test_success_creates_unprivileged_passwordless_user_and_jwt(self):
        state, _, _ = self.start()
        verifier = FeishuLoginAttempt.objects.get().code_verifier
        response = self.finish(state)
        self.assertEqual(response.status_code, 200, response.data)
        user = User.objects.get()
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.first_name, '测试成员')
        self.assertIsNotNone(user.last_login)
        self.assertFalse(user.user_permissions.exists())
        self.assertEqual(FeishuIdentity.objects.get().user, user)
        self.assertNotIn('provider-token', str(response.data))
        self.assertNotIn('test-only-secret', str(response.data))
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(TOKEN_URL, 'https://open.feishu.cn/open-apis/authen/v2/oauth/token')
        self.post_mock.assert_called_once_with(TOKEN_URL, json={
            'grant_type': 'authorization_code', 'client_id': 'cli_test',
            'client_secret': 'test-only-secret', 'code': 'provider-code',
            'redirect_uri': 'https://testhub.example/auth/feishu/callback',
            'code_verifier': verifier,
        }, timeout=(3, 10), allow_redirects=False)
        self.get_mock.assert_called_once_with(USER_INFO_URL,
            headers={'Authorization': 'Bearer provider-token-must-stay-server-side'},
            timeout=(3, 10), allow_redirects=False)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + response.data['access'])
        self.assertEqual(self.client.get('/api/accounts/me/').data['id'], user.id)

    def test_new_tester_can_work_only_after_joining_a_project(self):
        project = Project.objects.create(name='Team project')
        state, _, _ = self.start()
        login = self.finish(state)
        self.assertEqual(login.data['user']['groups'], ['普通测试成员'])
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + login.data['access'])
        user = User.objects.get(pk=login.data['user']['id'])
        self.assertFalse(user.has_perm('auth.change_user'))
        self.assertFalse(user.has_perm('testcases.delete_testcase'))
        self.assertTrue(user.has_perm('testcases.add_testexecution'))
        path = f'/api/projects/{project.pk}/testcase-modules/'
        self.assertEqual(self.client.get('/api/projects/').status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 403)
        ProjectMember.objects.create(user=user, project=project, role='member')
        module = self.client.post(path, {'name': 'Smoke'}, format='json')
        self.assertEqual(module.status_code, 201, module.data)
        case = self.client.post(f'/api/projects/{project.pk}/testcases/', {
            'name': 'Smoke case', 'module_id': module.data['id'], 'steps': [],
        }, format='json')
        self.assertEqual(case.status_code, 201, case.data)

    def test_repeat_login_respects_group_and_membership_edits(self):
        state, _, _ = self.start()
        user = User.objects.get(pk=self.finish(state).data['user']['id'])
        group = user.groups.get()
        group.permissions.clear()
        user.groups.remove(group)
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).data['user']['groups'], [])
        self.profile['open_id'] = 'ou_second'
        state, _, _ = self.start()
        second = User.objects.get(pk=self.finish(state).data['user']['id'])
        self.assertTrue(second.groups.filter(pk=group.pk).exists())
        self.assertFalse(second.has_perm('testcases.add_testcase'))

    def test_state_is_single_use_even_with_a_different_code(self):
        state, _, _ = self.start()
        cookie = self.client.cookies[COOKIE_NAME].value
        self.assertEqual(self.finish(state).status_code, 200)
        self.client.cookies[COOKIE_NAME] = cookie
        self.assertEqual(self.finish(state, code='another-code').status_code, 400)
        self.assertEqual(self.post_mock.call_count, 1)

    def test_missing_or_wrong_browser_and_state_are_rejected(self):
        state, _, _ = self.start()
        cookie = self.client.cookies[COOKIE_NAME].value
        for browser in ['', 'other-browser']:
            self.client.cookies[COOKIE_NAME] = browser
            self.assertEqual(self.finish(state).status_code, 400)
        self.client.cookies[COOKIE_NAME] = cookie
        self.assertEqual(self.finish('different-state').status_code, 400)
        self.post_mock.assert_not_called()
        self.assertEqual(self.finish(state).status_code, 200)

    def test_expired_state_is_rejected(self):
        state, _, _ = self.start()
        FeishuLoginAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.finish(state).status_code, 400)
        self.post_mock.assert_not_called()

    def test_cancelled_or_missing_code_does_not_call_provider(self):
        for params in [{'error': 'access_denied'}, {'code': ''}]:
            state, _, _ = self.start()
            self.assertEqual(self.finish(state, **params).status_code, 400)
        self.post_mock.assert_not_called()

    def test_foreign_tenant_or_missing_identity_cannot_create_user(self):
        for changes in [{'tenant_key': 'tenant_other'}, {'tenant_key': ''}, {'open_id': None}]:
            self.profile.update({'tenant_key': 'tenant_allowed', 'open_id': 'ou_employee', **changes})
            state, _, _ = self.start()
            self.assertEqual(self.finish(state).status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_repeat_login_reuses_identity_not_email_or_name(self):
        existing = User.objects.create_user(username='colleague', email='same@example.test', first_name='测试成员')
        self.profile['email'] = existing.email
        state, _, _ = self.start()
        first = self.finish(state)
        self.assertNotEqual(first.data['user']['id'], existing.id)
        state, _, _ = self.start()
        second = self.finish(state)
        self.assertEqual(first.data['user']['id'], second.data['user']['id'])
        self.assertEqual(User.objects.count(), 2)

    def test_feishu_name_and_avatar_are_returned_and_refreshed_for_existing_users(self):
        self.profile['avatar_thumb'] = 'https://example.test/avatar/first.png'
        state, _, _ = self.start()
        first = self.finish(state)
        username = first.data['user']['username']
        self.assertEqual(first.data['user']['display_name'], '测试成员')
        self.assertEqual(first.data['user']['avatar_url'], self.profile['avatar_thumb'])
        self.profile.update(name='更新后的飞书名称', avatar_thumb='https://example.test/avatar/new.png')
        state, _, _ = self.start()
        second = self.finish(state)
        self.assertEqual(second.data['user']['username'], username)
        self.assertEqual(second.data['user']['display_name'], '更新后的飞书名称')
        self.assertEqual(second.data['user']['avatar_url'], self.profile['avatar_thumb'])
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + second.data['access'])
        profile = self.client.get('/api/accounts/me/').data
        self.assertEqual(profile['display_name'], '更新后的飞书名称')
        self.assertEqual(profile['avatar_url'], self.profile['avatar_thumb'])
        self.assertEqual(User.objects.count(), 1)

    def test_provider_profile_does_not_overwrite_linked_local_profile(self):
        user = User.objects.create_user(username='local-name', first_name='Local')
        FeishuIdentity.objects.create(user=user, app_id='cli_test', open_id='ou_employee', tenant_key='tenant_allowed')
        self.profile['avatar_url'] = 'https://example.test/avatar/feishu.png'
        state, _, _ = self.start()
        response = self.finish(state)
        self.assertEqual(response.data['user']['display_name'], '测试成员')
        self.assertEqual(response.data['user']['avatar_url'], self.profile['avatar_url'])
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Local')

    def test_invalid_avatar_urls_are_not_used_and_removed_avatar_is_cleared(self):
        for value in ['javascript:alert(1)', 'data:image/png;base64,AAA', 'http://example.test/a.png',
                      'https://name:password@example.test/a.png', 'https://bad[', None, '']:
            self.profile['avatar_url'] = value
            state, _, _ = self.start()
            response = self.finish(state)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['user']['avatar_url'], '')
        self.profile['avatar_url'] = 'https://example.test/avatar.png'
        state, _, _ = self.start()
        self.assertTrue(self.finish(state).data['user']['avatar_url'])
        self.profile.pop('avatar_url')
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).data['user']['avatar_url'], '')

    def test_disabled_or_mismatched_linked_user_is_rejected(self):
        user = User.objects.create_user(username='disabled', is_active=False)
        identity = FeishuIdentity.objects.create(user=user, app_id='cli_test',
            open_id='ou_employee', tenant_key='tenant_allowed')
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).status_code, 403)
        user.is_active = True
        user.save()
        identity.tenant_key = 'wrong-tenant'
        identity.save()
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).status_code, 403)

    @override_settings(FEISHU_AUTO_CREATE_USER=False)
    def test_manual_link_mode(self):
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).status_code, 403)
        user = User.objects.create_user(username='existing')
        FeishuIdentity.objects.create(user=user, app_id='cli_test', open_id='ou_employee', tenant_key='tenant_allowed')
        state, _, _ = self.start()
        response = self.finish(state)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], user.pk)

    def test_token_failure_or_bad_json_cannot_create_user(self):
        for payload in [{'code': 20003, 'error_description': 'sensitive-provider-details'}, [], {'code': 0}]:
            self.post_mock.return_value.json.return_value = payload
            state, _, _ = self.start()
            response = self.finish(state)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('sensitive-provider-details', str(response.data))
        self.assertFalse(User.objects.exists())
        self.get_mock.assert_not_called()

    def test_network_or_json_errors_are_sanitized(self):
        for failure in [requests.Timeout('secret'), ValueError('secret')]:
            self.post_mock.return_value.json.side_effect = failure
            state, _, _ = self.start()
            response = self.finish(state)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('secret', str(response.data))
        self.assertFalse(User.objects.exists())

    def test_token_errors_explain_provider_reason_without_exposing_payload(self):
        for code, expected in [(20002, '应用凭证无效'), (20004, '已过期'),
                               (20010, '使用权限'), (20049, 'PKCE'),
                               (20071, '回调地址'), (29999, '令牌交换失败')]:
            with self.subTest(code=code):
                self.post_mock.return_value.status_code = 400
                self.post_mock.return_value.json.return_value = {
                    'code': code, 'error_description': 'secret-provider-code-token',
                    'access_token': 'secret-provider-code-token',
                }
                state, _, _ = self.start()
                with self.assertLogs('accounts.feishu', level='WARNING') as logs:
                    response = self.finish(state)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.data['error'])
                self.assertIn(str(code), response.data['error'])
                self.assertIn(f'provider_code={code}', str(logs.output))
                self.assertNotIn('secret-provider-code-token', str(response.data) + str(logs.output))
        self.assertFalse(User.objects.exists())
        self.get_mock.assert_not_called()

    def test_malformed_provider_code_is_not_echoed_or_logged(self):
        for code in ['secret-in-code', {'secret': 'private'}, ['private'], True]:
            self.post_mock.return_value.json.return_value = {'code': code}
            state, _, _ = self.start()
            with self.assertLogs('accounts.feishu', level='WARNING') as logs:
                response = self.finish(state)
            self.assertEqual(response.status_code, 400)
            self.assertIn('provider_code=None', str(logs.output))
            self.assertNotIn('secret', str(response.data) + str(logs.output))
            self.assertNotIn('private', str(response.data) + str(logs.output))

    def test_user_info_failure_cannot_create_user(self):
        self.get_mock.return_value.json.return_value = {'code': 20021, 'msg': 'resigned'}
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_bad_old_jwt_does_not_block_oauth(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer expired.invalid.jwt')
        state, _, _ = self.start()
        self.assertEqual(self.finish(state).status_code, 200)

    def test_configuration_change_invalidates_pending_login(self):
        state, _, _ = self.start()
        with override_settings(FEISHU_APP_ID='other-app'):
            self.assertEqual(self.finish(state).status_code, 400)
        self.post_mock.assert_not_called()

    def test_password_login_still_works(self):
        User.objects.create_user(username='password-user', password='local-password-123')
        response = self.client.post('/api/token/', {
            'username': 'password-user', 'password': 'local-password-123',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)

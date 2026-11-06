from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import Permission, User
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import FeishuIdentity
from projects.models import Project, ProjectMember
from .models import RequirementDocument


@override_settings(FEISHU_LOGIN_ENABLED=True, FEISHU_APP_ID='test-app', FEISHU_APP_SECRET='test-secret',
                   FEISHU_REDIRECT_URI='https://platform.example/auth/feishu/callback',
                   FEISHU_ALLOWED_TENANT_KEYS=['example'])
class FeishuImportTests(APITestCase):
    endpoint = '/api/requirements/documents/'

    def setUp(self):
        self.user = User.objects.create_user('reader')
        self.user.user_permissions.add(Permission.objects.get(codename='add_requirementdocument'))
        self.project = Project.objects.create(name='Example')
        ProjectMember.objects.create(project=self.project, user=self.user)
        self.client.force_authenticate(self.user)
        self.payload = {'title': 'Checkout', 'project': str(self.project.pk), 'document_type': 'txt',
                        'feishu_url': 'https://example.feishu.cn/wiki/WikiToken123'}
        FeishuIdentity.objects.create(user=self.user, open_id='reader', app_id='test-app', tenant_key='example')
        self.exchange = patch('accounts.feishu.requests.post').start()
        self.get = patch('accounts.feishu.requests.get').start()
        self.addCleanup(patch.stopall)
        self.exchange.return_value = Mock(status_code=200, json=lambda: {'code': 0, 'access_token': 'test-token'})

    def start(self):
        response = self.client.post(self.endpoint + 'feishu-authorize/', self.payload,
                                    HTTP_ORIGIN='https://platform.example')
        self.assertEqual(response.status_code, 200, response.data)
        params = parse_qs(urlsplit(response.data['authorize_url']).query)
        self.payload.update(feishu_code='once', feishu_state=params['state'][0])
        return params

    def provider(self, **data):
        return Mock(status_code=200, json=lambda: {'code': 0, 'data': data})

    def test_authorized_wiki_creates_text_document_once(self):
        clean_url = self.payload['feishu_url']
        self.payload['feishu_url'] += '?from=share&example=private#selection'
        params = self.start()
        self.assertEqual(set(params['scope'][0].split()), {'docx:document:readonly', 'wiki:node:read'})
        self.get.side_effect = [self.provider(tenant_key='example', open_id='reader'),
                                self.provider(node={'obj_type': 'docx', 'obj_token': 'ResolvedDoc123'}),
                                self.provider(content='第一条需求\n第二条需求')]
        response = self.client.post(self.endpoint, self.payload)
        self.assertEqual(response.status_code, 201, response.data)
        doc = RequirementDocument.objects.get()
        self.assertEqual((doc.content, doc.uploader, doc.project), ('第一条需求\n第二条需求', self.user, self.project))
        self.assertTrue(self.get.call_args.args[0].endswith('/ResolvedDoc123/raw_content'))
        self.assertEqual(self.client.post(self.endpoint, self.payload).status_code, 400)
        self.exchange.assert_called_once()
        self.user.user_permissions.add(Permission.objects.get(codename='view_requirementdocument'))
        self.client.force_authenticate(User.objects.get(pk=self.user.pk))
        detail = self.client.get(f'{self.endpoint}{doc.pk}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['feishu_url'], clean_url)
        self.assertTrue(detail.data['uploaded_at'])

    def test_tampered_document_and_foreign_feishu_account(self):
        self.payload['feishu_url'] = 'https://example.feishu.cn/docx/Document123'
        self.assertEqual(self.start()['scope'], ['docx:document:readonly'])
        response = self.client.post(self.endpoint, {**self.payload, 'feishu_url': 'https://example.feishu.cn/docx/OtherDoc123'})
        self.assertEqual(response.status_code, 400)
        self.exchange.assert_not_called()
        self.get.return_value = self.provider(tenant_key='example', open_id='someone-else')
        self.assertEqual(self.client.post(self.endpoint, self.payload).status_code, 400)
        self.get.assert_called_once()
        self.assertFalse(RequirementDocument.objects.exists())

    def test_invalid_link_or_foreign_project_cannot_start(self):
        path = self.endpoint + 'feishu-authorize/'
        self.assertEqual(self.client.post(path, {**self.payload, 'feishu_url': 'https://outside.example/docx/Document123'}).status_code, 400)
        # Browsers interpret a backslash in the authority as the start of the path.
        misleading_host = r'https://outside.example\example.feishu.cn/docx/Document123'
        self.assertEqual(self.client.post(path, {**self.payload, 'feishu_url': misleading_host}).status_code, 400)
        other = Project.objects.create(name='Private')
        response = self.client.post(f'{path}?project={self.project.pk}', {**self.payload, 'project': other.pk})
        self.assertEqual(response.status_code, 403)
        self.exchange.assert_not_called()

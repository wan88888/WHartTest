from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from projects.models import Project


class ProjectCredentialUpdateTests(APITestCase):
    def setUp(self):
        admin = User.objects.create_superuser(username='owner', email='owner@example.com')
        self.client.force_authenticate(admin)
        self.project = Project.objects.create(name='credential-test', creator=admin)
        self.cred = self.project.credentials.create(username='tester', password='initial')
        self.url = f'/api/projects/{self.project.pk}/'

    def update(self, **fields):
        return self.client.patch(self.url, fields, format='json')

    def test_saving_description_and_legacy_blank_password_preserve_secret_and_id(self):
        self.assertEqual(self.update(description='edited').status_code, 200)
        result = self.update(credentials=[{'id': self.cred.pk, 'password': '', 'user_role': 'tester'}])
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('password', result.data['credentials'][0])
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.password, 'initial')
        self.assertEqual(self.cred.user_role, 'tester')

    def test_explicit_updates_preserve_concurrent_additions_and_only_delete_named_rows(self):
        obsolete = self.project.credentials.create(username='obsolete')
        concurrent = self.project.credentials.create(username='concurrent')
        result = self.update(
            credentials=[{'id': self.cred.pk, 'password': ' new '}, {'username': 'extra'}],
            removed_credential_ids=[obsolete.pk],
        )
        self.assertEqual(result.status_code, 200)
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.password, ' new ')
        self.assertTrue(self.project.credentials.filter(pk=concurrent.pk).exists())
        self.assertFalse(self.project.credentials.filter(pk=obsolete.pk).exists())
        self.assertTrue(self.project.credentials.filter(username='extra').exists())

    def test_foreign_ids_and_conflicting_rows_roll_back_project_edit(self):
        foreign = Project.objects.create(name='other').credentials.create(password='private')
        for fields in (
            {'credentials': [{'id': foreign.pk, 'password': 'incorrect'}]},
            {'removed_credential_ids': [foreign.pk]},
            {'credentials': [{'id': self.cred.pk}, {'id': self.cred.pk}]},
            {'credentials': [{'id': self.cred.pk}], 'removed_credential_ids': [self.cred.pk]},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.update(description='incorrect', **fields).status_code, 400)
        self.project.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.project.description, '')
        self.assertEqual(foreign.password, 'private')
        self.assertTrue(self.project.credentials.filter(pk=self.cred.pk).exists())

    def test_legacy_empty_collection_removes_all_credentials(self):
        self.assertEqual(self.update(credentials=[]).status_code, 200)
        self.assertFalse(self.project.credentials.exists())

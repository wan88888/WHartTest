import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from projects.models import Project, ProjectMember
from .artifacts import claim_artifacts
from .consumers import SocketUserManager, UiAutomationConsumer
from .models import (UiAuthState, UiBatchExecutionRecord, UiCaseStepsDetailed, UiEnvironmentConfig,
                     UiExecutionArtifact, UiExecutionRecord, UiModule, UiPage, UiPageSteps, UiTestCase)
from .socket_models import QueueModel, SocketDataModel, UiSocketEnum


class ProjectBoundaryTests(APITestCase):
    base = '/api/ui-automation/'

    def setUp(self):
        self.user = User.objects.create_user('web_member')
        self.owner = User.objects.create_user('other')
        self.a, self.b = (Project.objects.create(name=name, creator=self.owner) for name in ('A', 'B'))
        ProjectMember.objects.create(project=self.a, user=self.user)
        self.env, self.foreign_env = (UiEnvironmentConfig.objects.create(project=p, name='env') for p in (self.a, self.b))
        self.state, self.foreign_state = (UiAuthState.objects.create(env_config=e, name='login', state_json={
            'cookies': [{'name': 'session', 'value': 'fixture-secret', 'expires': 1800000000}],
        }) for e in (self.env, self.foreign_env))
        self.case, self.foreign = (UiTestCase.objects.create(
            project=p, name='case', module=UiModule.objects.create(project=p, name='module')) for p in (self.a, self.b))
        page = UiPage.objects.create(name='page', project=self.a, module=self.case.module)
        self.group = UiPageSteps.objects.create(name='step', page=page, module=page.module, project=self.a, auth_state=self.state)
        self.step = UiCaseStepsDetailed.objects.create(test_case=self.case, page_step=self.group, case_data={'input': 'v'}, status=2)
        self.client.force_authenticate(self.user)

    def grant(self, *codes):
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='ui_automation', codename__in=codes))
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_authenticate(self.user)

    def test_auth_state_metadata_and_writes(self):
        self.grant('view_uiauthstate', 'change_uiauthstate', 'add_uiauthstate', 'delete_uiauthstate')
        url = self.base + 'auth-states/'
        for response in (self.client.get(url), self.client.get(url + f'{self.state.pk}/')):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('fixture-secret', str(response.data))
            self.assertNotIn('state_json', str(response.data))
        self.assertEqual(self.client.get(url + f'{self.state.pk}/').data['credential_summary']['cookies'], 1)
        self.assertEqual(self.client.get(url + f'{self.foreign_state.pk}/').status_code, 404)
        self.assertEqual(self.client.patch(url + f'{self.foreign_state.pk}/', {'is_active': False}).status_code, 404)
        self.assertEqual(self.client.delete(url + f'{self.foreign_state.pk}/').status_code, 404)
        self.assertIn(self.client.post(url, {'name': 'x', 'env_config': self.foreign_env.pk}).status_code, (400, 403))

    def test_invalid_storage_state_rejected_and_historical_entry_readable(self):
        self.grant('view_uiauthstate', 'add_uiauthstate', 'change_uiauthstate', 'add_uiexecutionrecord')
        url = self.base + 'auth-states/'
        for state in ({'cookies': 42}, {'origins': True}, {'session_storage': ['x']},
                      {'cookies': [{'name': 'a', 'value': 'v', 'expires': 'bad'}]},
                      {'origins': [{'origin': 'https://example.test', 'localStorage': 1}]}):
            response = self.client.post(url, {'env_config': self.env.pk, 'name': 'invalid', 'state_json': state}, format='json')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.client.patch(url + f'{self.state.pk}/', {'state_json': state}, format='json').status_code, 400)
        self.assertFalse(UiAuthState.objects.filter(name='invalid').exists())
        self.state.refresh_from_db()
        self.assertEqual(self.state.state_json['cookies'][0]['value'], 'fixture-secret')
        UiAuthState.objects.filter(pk=self.state.pk).update(state_json={'cookies': 42})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(self.client.get(url + f'{self.state.pk}/').data['credential_summary']['invalid'])
        self.assertEqual(self.client.post(self.base + 'trigger-batch/', {'case_ids': [self.case.pk]}, format='json').status_code, 400)
        self.assertFalse(UiBatchExecutionRecord.objects.exists())

    def test_rest_batch_routes_to_current_project_member_by_numeric_owner(self):
        self.grant('add_uiexecutionrecord')
        actuator = UiAutomationConsumer()
        actuator.is_actuator = True
        actuator.user_id = 'test-actuator'
        actuator.send_json = AsyncMock()
        actuator.channel_layer = AsyncMock()
        with (patch('ui_automation.actuator_registry.resolve_and_select', return_value=({}, {'id': actuator.user_id}, None)),
              patch('ui_automation.actuator_registry.get_raw_consumer', return_value=actuator),
              patch('ui_automation.actuator_registry.reserve_slots', return_value=(True, ''))):
            self.assertEqual(self.client.post(self.base + 'trigger-batch/', {'case_ids': [self.case.pk]}, format='json').status_code, 200)
        task = actuator.send_json.await_args.args[0]
        args = {'task_token': task.data.func_args['task_token'], 'case_id': self.case.pk, 'status': 'success',
                'steps': [{'message': 'private result'}]}
        async_to_sync(actuator.route_message)(SocketDataModel(user=task.user, data=QueueModel(func_name=UiSocketEnum.CASE_RESULT, func_args=args)))
        group, event = actuator.channel_layer.group_send.await_args.args
        self.assertEqual(group, f'ui.user.{self.user.pk}')
        self.assertTrue(UiExecutionRecord.objects.filter(test_case=self.case, executor=self.user).exists())
        outside = self.socket()
        outside.scope['user'] = User.objects.create_user('system')
        async_to_sync(outside.broadcast_result)(event)
        outside.send_json.assert_not_awaited()
        own = self.socket()
        async_to_sync(own.broadcast_result)(event)
        self.assertIn('private result', str(own.send_json.await_args))
        ProjectMember.objects.filter(project=self.a, user=self.user).delete()
        own.send_json.reset_mock()
        async_to_sync(own.broadcast_result)(event)
        own.send_json.assert_not_awaited()

    def test_dispatch_authorization_precedes_side_effects_and_keeps_snapshot_private(self):
        url = self.base + 'trigger-batch/'
        with patch('ui_automation.actuator_registry.resolve_and_select') as select:
            self.assertEqual(self.client.post(url, {'case_ids': [self.case.pk]}, format='json').status_code, 403)
            self.grant('add_uiexecutionrecord')
            for payload in ({'case_ids': [self.foreign.pk]}, {'case_ids': [self.case.pk, self.foreign.pk]},
                            {'case_ids': [self.case.pk], 'env_config_id': self.foreign_env.pk},
                            {'case_ids': [self.case.pk], 'scheduled_task_id': 123}):
                self.assertIn(self.client.post(url, payload, format='json').status_code, (400, 403))
            select.assert_not_called()
        self.assertFalse(UiBatchExecutionRecord.objects.exists())
        actuator = UiAutomationConsumer()
        actuator.send_json = AsyncMock()
        with (patch('ui_automation.actuator_registry.resolve_and_select', return_value=({}, {'id': 'dummy'}, None)),
              patch('ui_automation.actuator_registry.get_raw_consumer', return_value=actuator),
              patch('ui_automation.actuator_registry.reserve_slots', return_value=(True, ''))):
            result = self.client.post(url, {'case_ids': [self.case.pk]}, format='json')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('fixture-secret', str(result.data))
        self.assertIn('fixture-secret', str(actuator.send_json.await_args.args[0].data.func_args['auth_snapshots']))
        self.assertEqual(UiBatchExecutionRecord.objects.get().project_id, self.a.pk)
        self.assertEqual(self.client.post(self.base + 'actuators/config/', {}, format='json').status_code, 403)

    def socket(self):
        web = UiAutomationConsumer()
        web.scope = {'user': self.user}
        web.user_id = self.user.username
        web.send_json = AsyncMock()
        return web

    def test_socket_identity_scope_recorder_and_stopping(self):
        self.grant('add_uiexecutionrecord')
        web = self.socket()
        web.handle_execute_test_case = AsyncMock()
        def send(kind, args):
            async_to_sync(web.route_message)(SocketDataModel(user='other', data=QueueModel(func_name=kind, func_args=args)))
        send(UiSocketEnum.TEST_CASE, {'case_id': self.foreign.pk})
        web.handle_execute_test_case.assert_not_awaited()
        send(UiSocketEnum.TEST_CASE, {'case_id': self.case.pk, 'executor_id': self.owner.pk, 'batch_id': 99})
        safe, name = web.handle_execute_test_case.await_args.args
        self.assertEqual((safe['executor_id'], name), (self.user.pk, self.user.username))
        self.assertNotIn('batch_id', safe)
        actuator = UiAutomationConsumer()
        actuator.send_json = AsyncMock()
        args = {'case_id': self.case.pk, 'project_id': self.a.pk, 'executor_id': self.owner.pk}
        actuator.register_actuator_task(actuator, args, self.owner.username)
        with patch.object(SocketUserManager, 'get_all_actuators', return_value=[actuator]):
            send(UiSocketEnum.STOP_EXECUTION, {})
            send(UiSocketEnum.STOP_EXECUTION, {'task_id': self.case.pk, 'actuator_id': 'dummy'})
            actuator.send_json.assert_not_awaited()
            own = {**safe}
            actuator.register_actuator_task(actuator, own, name)
            send(UiSocketEnum.STOP_EXECUTION, {'task_id': self.case.pk})
            payload = actuator.send_json.await_args.args[0].data.func_args
            self.assertEqual(payload['run_token'], own['task_token'])
            self.assertNotEqual(payload['run_token'], args['task_token'])
        with patch('ui_automation.recorder.session_manager.recorder_manager') as manager:
            manager.get_meta.return_value = Mock(owner_id=self.owner.pk, project_id=self.a.pk)
            send(UiSocketEnum.RECORDER_START, {'session_id': 'known'})
            self.assertIsNone(web._recorder_session_id)
        web.is_actuator = False
        web.send_json.reset_mock()
        async_to_sync(web.broadcast_result)({'data': {'user': 'other', 'func_name': 'u_case_result'}})
        web.send_json.assert_not_awaited()

    def test_reorder_is_exact_and_preserves_rows(self):
        self.grant('change_uicasestepsdetailed')
        other = UiCaseStepsDetailed.objects.create(test_case=self.case, page_step=self.group, case_sort=1)
        url = self.base + 'case-steps/reorder/'
        for ids in ([other.pk], [other.pk, other.pk], [999999, self.step.pk]):
            self.assertEqual(self.client.post(url, {'test_case': self.case.pk, 'ids': ids}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'test_case': self.case.pk, 'ids': [other.pk, self.step.pk]}, format='json').status_code, 200)
        self.step.refresh_from_db()
        self.assertEqual((self.step.case_sort, self.step.case_data, self.step.status), (1, {'input': 'v'}, 2))
        self.assertEqual(self.client.post(url, {'test_case': self.foreign.pk, 'ids': []}, format='json').status_code, 404)

    def test_batch_permissions_cannot_bypass_report_permissions(self):
        self.grant('view_uibatchexecutionrecord', 'delete_uibatchexecutionrecord')
        batch = UiBatchExecutionRecord.objects.create(project=self.a, name='batch')
        record = UiExecutionRecord.objects.create(test_case=self.case, batch=batch, error_message='report-marker')
        url = self.base + f'batch-records/{batch.pk}/'
        self.assertNotIn('execution_records', self.client.get(url).data)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertTrue(UiExecutionRecord.objects.filter(pk=record.pk).exists())
        self.grant('view_uiexecutionrecord', 'delete_uiexecutionrecord')
        self.assertEqual(self.client.get(url).data['execution_records'][0]['error_message'], 'report-marker')
        self.assertIn(self.client.delete(url).status_code, (200, 204))
        self.assertFalse(UiExecutionRecord.objects.filter(pk=record.pk).exists())

    def test_artifacts_validate_own_and_delete_only_registered_files(self):
        self.grant('add_uiexecutionrecord', 'view_uiexecutionrecord', 'delete_uiexecutionrecord')
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            def upload(endpoint, data, case):
                return self.client.post(self.base + endpoint + '/upload/', {
                    'test_case': case.pk, 'file': SimpleUploadedFile('browser.html', data, 'text/html'),
                })
            self.assertEqual(upload('screenshots', b'<script>alert(1)</script>', self.case).status_code, 400)
            self.assertEqual(upload('traces', b'<html>', self.case).status_code, 400)
            self.assertEqual(upload('screenshots', b'x', self.foreign).status_code, 404)
            buffer = io.BytesIO()
            Image.new('RGB', (2, 2)).save(buffer, 'PNG')
            marker = b'<script>fixture</script>'
            shot = upload('screenshots', buffer.getvalue() + marker, self.case)
            self.assertEqual(shot.status_code, 201)
            png = self.client.get(shot.data['url'])
            self.assertEqual(png['Content-Type'], 'image/png')
            self.assertNotIn(marker, b''.join(png.streaming_content))
            trace = io.BytesIO()
            with zipfile.ZipFile(trace, 'w') as archive:
                archive.writestr('trace.trace', '{}')
            archive = upload('traces', trace.getvalue(), self.case)
            self.assertEqual(archive.status_code, 201)
            self.assertEqual(APIClient().get(archive.data['url']).status_code, 401)
            foreign_user = User.objects.create_user('outside')
            foreign_user.user_permissions.add(Permission.objects.get(codename='view_uiexecutionrecord'))
            outsider = APIClient()
            outsider.force_authenticate(foreign_user)
            self.assertEqual(outsider.get(archive.data['url']).status_code, 404)
            response = self.client.get(archive.data['url'])
            self.assertIn('attachment', response['Content-Disposition'])
            self.assertEqual(b''.join(response.streaming_content), trace.getvalue())
            victim = Path(root) / 'other.txt'
            victim.write_text('other resource')
            record = UiExecutionRecord.objects.create(test_case=self.case, trace_path=archive.data['path'],
                screenshots=[shot.data['url'], '../other.txt'], video_path=str(victim))
            claim_artifacts(record, record.trace_path, record.screenshots)
            detail = self.client.get(self.base + f'execution-records/{record.pk}/').data
            self.assertEqual(detail['screenshots'], [shot.data['url']])
            self.assertEqual(detail['trace_path'], archive.data['url'])
            self.assertEqual(self.client.post(self.base + 'execution-records/', {'video_path': str(victim)}).status_code, 405)
            with self.captureOnCommitCallbacks(execute=True):
                self.assertIn(self.client.delete(self.base + f'execution-records/{record.pk}/').status_code, (200, 204))
            self.assertTrue(victim.is_file())
            self.assertFalse((Path(root) / archive.data['path']).exists())
            self.assertFalse(UiExecutionArtifact.objects.exists())

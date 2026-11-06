"""UI 自动化 WebSocket 认证（第一层中间件 + 第二层 consumer 校验）测试。

覆盖：
- 中间件：web JWT 校验、执行器共享密钥校验、grace/strict 模式、错误凭据拒绝
- consumer：执行器 ID 防抢注、结果回传来源校验（严格/兼容模式）

注意：WebSocket 用例必须用 TransactionTestCase——中间件在独立线程/事件循环里
同步查库（sync_to_async），PostgreSQL 下看不到 TestCase 未提交事务的数据，
会以 SynchronousOnlyOperation 报错。
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from ui_automation import consumers as cons
from ui_automation.routing import websocket_urlpatterns
from wharttest_django.websocket_auth import (
    WS_CLOSE_UNAUTHENTICATED,
    UiWebSocketAuthMiddleware,
)


def _middleware_app():
    return UiWebSocketAuthMiddleware(URLRouter(websocket_urlpatterns))


class MiddlewareWebAuthTests(TransactionTestCase):
    """web 端点 JWT 认证。"""

    def setUp(self):
        self.user = User.objects.create_user(username='wsuser', password='x')
        from rest_framework_simplejwt.tokens import AccessToken
        self.token = str(AccessToken.for_user(self.user))

    async def _connect_and_first_message(self, path):
        communicator = WebsocketCommunicator(_middleware_app(), path)
        try:
            accepted = await communicator.connect()
            first = None
            if accepted:
                first = await communicator.receive_output(timeout=2)
            return accepted, first, communicator
        except Exception:
            await communicator.disconnect()
            raise

    async def test_valid_jwt_connects_as_authenticated_user(self):
        accepted, first, communicator = await self._connect_and_first_message(
            f'/ws/ui/web/?token={self.token}'
        )
        try:
            self.assertTrue(accepted)
            self.assertEqual(first['type'], 'websocket.send')
            self.assertIn('"user":"wsuser"', first['text'])
        finally:
            await communicator.disconnect()

    async def test_garbage_token_rejected_even_in_grace_mode(self):
        communicator = WebsocketCommunicator(_middleware_app(), '/ws/ui/web/?token=not-a-jwt')
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['type'], 'websocket.close')
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()

    async def test_no_token_rejected_always(self):
        """WS 认证为硬性要求：匿名连接无条件拒绝（无兼容模式）。"""
        communicator = WebsocketCommunicator(_middleware_app(), '/ws/ui/web/')
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['type'], 'websocket.close')
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()

    async def test_no_token_rejected_in_strict_mode(self):
        communicator = WebsocketCommunicator(_middleware_app(), '/ws/ui/web/')
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['type'], 'websocket.close')
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()


class MiddlewareActuatorAuthTests(TransactionTestCase):
    """执行器端点共享密钥认证。"""

    SECRET = 'test-actuator-secret'

    async def _connect(self, path):
        communicator = WebsocketCommunicator(_middleware_app(), path)
        try:
            accepted = await communicator.connect()
            first = None
            if accepted:
                first = await communicator.receive_output(timeout=2)
            return accepted, first, communicator
        except Exception:
            await communicator.disconnect()
            raise

    @override_settings(UI_WS_ACTUATOR_TOKEN=SECRET)
    async def test_correct_secret_connects(self):
        accepted, first, communicator = await self._connect(
            f'/ws/ui/actuator/?id=act-1&token={self.SECRET}'
        )
        try:
            self.assertTrue(accepted)
            self.assertEqual(first['type'], 'websocket.send')
            self.assertIn('"user":"act-1"', first['text'])
        finally:
            await communicator.disconnect()

    @override_settings(UI_WS_ACTUATOR_TOKEN=SECRET)
    async def test_wrong_secret_rejected(self):
        communicator = WebsocketCommunicator(
            _middleware_app(), '/ws/ui/actuator/?id=act-1&token=wrong'
        )
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()

    @override_settings(UI_WS_ACTUATOR_TOKEN=SECRET)
    async def test_missing_secret_rejected_in_strict_mode(self):
        communicator = WebsocketCommunicator(_middleware_app(), '/ws/ui/actuator/?id=act-1')
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()

    @override_settings(UI_WS_ACTUATOR_TOKEN=SECRET)
    async def test_missing_secret_rejected_always(self):
        """无凭据执行器连接无条件拒绝（WS 认证硬性要求）。"""
        communicator = WebsocketCommunicator(_middleware_app(), '/ws/ui/actuator/?id=act-grace')
        try:
            self.assertTrue(await communicator.connect())
            close = await communicator.receive_output(timeout=2)
            self.assertEqual(close['code'], WS_CLOSE_UNAUTHENTICATED)
        finally:
            await communicator.disconnect()


class ActuatorHijackTests(TestCase):
    """执行器 ID 防抢注。"""

    def test_duplicate_actuator_id_rejected(self):
        first = cons.UiAutomationConsumer()
        second = cons.UiAutomationConsumer()
        self.assertTrue(cons.SocketUserManager.add_actuator('dup-1', first))
        self.assertFalse(cons.SocketUserManager.add_actuator('dup-1', second))
        # 原连接不被顶掉
        self.assertIs(cons.SocketUserManager.get_actuator_by_id('dup-1'), first)
        cons.SocketUserManager.remove_actuator('dup-1')

    def test_reconnect_same_instance_allowed(self):
        consumer = cons.UiAutomationConsumer()
        try:
            self.assertTrue(cons.SocketUserManager.add_actuator('dup-2', consumer))
            self.assertTrue(cons.SocketUserManager.add_actuator('dup-2', consumer))
        finally:
            cons.SocketUserManager.remove_actuator('dup-2')


class ResultSourceVerificationTests(TestCase):
    """执行器结果回传来源校验。"""

    def _actuator_consumer(self) -> cons.UiAutomationConsumer:
        consumer = cons.UiAutomationConsumer()
        consumer.is_actuator = True
        consumer.user_id = 'act-test'
        return consumer

    def test_registered_task_requires_its_token_and_original_user(self):
        actuator = self._actuator_consumer()
        args = {'case_id': 1, 'executor_id': 5}
        actuator.register_actuator_task(actuator, args, 'web_9')
        token = args['task_token']
        for user, supplied in [('web_9', 'forged'), ('web_victim', token), ('web_9', None)]:
            self.assertEqual(actuator._take_result_source(user, {'task_token': supplied}), (None, None))
        for _ in range(2):
            payload = {'task_token': token}
            _, run = actuator._take_result_source('web_9', payload)
            self.assertEqual((run['owner'], run['cases']), (5, {1}))
            self.assertNotIn('task_token', payload)

    def test_expired_task_cannot_report_or_stop(self):
        actuator = self._actuator_consumer()
        with patch('ui_automation.consumers.time.monotonic', return_value=100):
            args = {'case_id': 1}
            actuator.register_actuator_task(actuator, args, 'web_9')
        token = args['task_token']
        with patch('ui_automation.consumers.time.monotonic', return_value=101 + actuator._TASK_TOKEN_TTL_SECONDS):
            self.assertEqual(actuator._take_result_source('web_9', {'task_token': token}), (None, None))
            actuator.register_actuator_task(actuator, {}, 'web_9')
            self.assertNotIn(token, actuator.actuator_runs)

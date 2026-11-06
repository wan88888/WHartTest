from django.test import TestCase
from django.contrib.auth.models import User
from projects.models import Project
from testcases.models import TestSuite, TestCase as TestCaseModel, TestExecution, TestCaseModule
from testcases.serializers import TestSuiteSerializer, TestExecutionCreateSerializer
from rest_framework.exceptions import ValidationError

class TestSuiteExecutionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.project = Project.objects.create(name='Test Project', description='Test Description', creator=self.user)
        self.module = TestCaseModule.objects.create(project=self.project, name='Test Module', creator=self.user)
        
        self.testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name='Test Case 1',
            creator=self.user
        )
        
        self.suite = TestSuite.objects.create(
            project=self.project,
            name='Test Suite 1',
            creator=self.user
        )

    def test_suite_validation_with_testcase(self):
        """测试包含用例的套件验证"""
        self.suite.testcases.add(self.testcase)
        
        serializer = TestExecutionCreateSerializer(data={'suite_id': self.suite.id})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        
    def test_suite_validation_empty(self):
        """测试空套件验证"""
        serializer = TestExecutionCreateSerializer(data={'suite_id': self.suite.id})
        self.assertFalse(serializer.is_valid())
        self.assertIn('suite_id', serializer.errors)
        self.assertEqual(str(serializer.errors['suite_id'][0]), "测试套件中没有测试用例")

    def test_suite_serializer_partial_update(self):
        """测试套件序列化器的部分更新"""
        self.suite.testcases.add(self.testcase)
        
        data = {'name': 'Updated Suite Name'}
        serializer = TestSuiteSerializer(instance=self.suite, data=data, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_suite = serializer.save()
        self.assertEqual(updated_suite.name, 'Updated Suite Name')
        self.assertTrue(updated_suite.testcases.exists())

    def test_suite_serializer_validation_empty_create(self):
        """测试创建空套件时的验证"""
        data = {
            'name': 'Empty Suite',
            'project': self.project.id,
            'testcases': [],
        }
        # 需要提供 context
        context = {'project_id': self.project.id, 'request': None}
        serializer = TestSuiteSerializer(data=data, context=context)
        self.assertFalse(serializer.is_valid())
        self.assertIn('non_field_errors', serializer.errors)

    def test_cancel_reconciles_rows_and_blocks_late_results_and_duplicate_delivery(self):
        from django.utils import timezone
        from testcases.models import TestExecution, TestCaseResult
        from testcases.tasks import _finish_result, cancel_test_execution, execute_test_suite

        execution = TestExecution.objects.create(suite=self.suite, executor=self.user, status='running')
        children = []
        for i, status in enumerate(('running', 'pending', 'pass', 'fail', 'error', 'skip')):
            case = TestCaseModel.objects.create(project=self.project, module=self.module, name=f'case {i}')
            children.append(TestCaseResult.objects.create(execution=execution, testcase=case, status=status))
        late = children[0]
        self.assertTrue(cancel_test_execution(execution.pk)['success'])
        execution.refresh_from_db()
        finished = execution.completed_at
        late.status = 'pass'
        late.started_at = timezone.now()
        _finish_result(late)
        self.assertTrue(cancel_test_execution(execution.pk)['success'])
        self.assertEqual(execute_test_suite.run(execution.pk)['status'], 'cancelled')
        execution.refresh_from_db()
        self.assertEqual((execution.status, execution.completed_at), ('cancelled', finished))
        self.assertEqual((execution.total_count, execution.skipped_count, execution.passed_count,
                          execution.failed_count, execution.error_count), (6, 3, 1, 1, 1))
        self.assertEqual(list(execution.results.order_by('id').values_list('status', flat=True)),
                         ['skip', 'skip', 'pass', 'fail', 'error', 'skip'])

    def test_cancel_before_worker_starts_and_exactly_once_result(self):
        from django.utils import timezone
        from testcases.models import TestExecution, TestCaseResult
        from testcases.tasks import _finish_result, cancel_test_execution, execute_test_suite

        self.suite.testcases.add(self.testcase)
        execution = TestExecution.objects.create(suite=self.suite, executor=self.user)
        self.assertTrue(cancel_test_execution(execution.pk)['success'])
        self.assertEqual(execute_test_suite.run(execution.pk)['status'], 'cancelled')
        execution.refresh_from_db()
        self.assertEqual((execution.total_count, execution.skipped_count, execution.results.get().status), (1, 1, 'skip'))
        running = TestExecution.objects.create(suite=self.suite, executor=self.user, status='running')
        result = TestCaseResult.objects.create(execution=running, testcase=self.testcase, status='running', started_at=timezone.now())
        result.status = 'pass'
        _finish_result(result)
        _finish_result(result)
        running.refresh_from_db()
        self.assertEqual((running.passed_count, running.results.get().status), (1, 'pass'))

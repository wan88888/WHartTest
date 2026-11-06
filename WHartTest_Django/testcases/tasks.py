"""
测试用例执行的Celery异步任务
"""
import logging
import asyncio
import re
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Count
from datetime import datetime
from typing import Dict, Any
from django.conf import settings
import os
import json
import uuid
import httpx
from rest_framework_simplejwt.tokens import RefreshToken

from .models import TestExecution, TestSuite, TestCaseResult, TestCase
from prompts.models import UserPrompt, PromptType
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


def sync_scheduled_task_result(execution) -> None:
    """套件执行完结时把测试结果回写到任务中心执行记录（归一成败语义）。

    套件执行（TestExecution）由任务中心定时/手动触发时携带 scheduled_task 关联；
    完结后按归一规则更新对应 TaskExecution：completed 且无失败 → success；
    completed 但有用例失败、或套件执行本身 failed → failed；cancelled 不动。
    """
    try:
        scheduled_task_id = getattr(execution, 'scheduled_task_id', None)
        if not scheduled_task_id:
            return
        from task_center.models import TaskExecution

        if execution.status == 'completed':
            result_status = (
                TaskExecution.ExecutionStatus.SUCCESS
                if execution.failed_count == 0 and execution.error_count == 0
                else TaskExecution.ExecutionStatus.FAILED
            )
        elif execution.status == 'failed':
            result_status = TaskExecution.ExecutionStatus.FAILED
        else:
            # cancelled / running / pending：不回写
            return

        task_execution = (
            TaskExecution.objects
            .filter(
                task_id=scheduled_task_id,
                suite_execution_id=execution.id,
                status=TaskExecution.ExecutionStatus.RUNNING,
            )
            .order_by('-started_at')
            .first()
        )
        if task_execution is None:
            return
        task_execution.status = result_status
        task_execution.test_status = result_status
        task_execution.finished_at = timezone.now()
        task_execution.save(update_fields=['status', 'test_status', 'finished_at'])
        logger.info(
            f"[任务中心] 套件执行 {execution.id} 测试结果已回写执行记录 {task_execution.execution_id}: {result_status}"
        )
    except Exception:
        logger.error(f"[任务中心] 套件执行 {getattr(execution, 'id', '?')} 测试结果回写失败", exc_info=True)


@shared_task(bind=True, name='testcases.execute_test_suite')
def execute_test_suite(self, execution_id):
    """
    执行测试套件的异步任务
    
    Args:
        execution_id: TestExecution实例的ID
        
    Returns:
        dict: 执行结果摘要
    """
    try:
        with transaction.atomic():
            execution = TestExecution.objects.select_for_update().get(pk=execution_id)
            if execution.status != 'pending':
                return {'execution_id': execution.pk, 'status': execution.status}
            suite = execution.suite
            _prepare_suite_results(execution)
            all_tasks = list(execution.results.select_related('testcase').order_by('testcase__level', 'testcase_id'))
            execution.status = 'running'
            execution.started_at = timezone.now()
            execution.celery_task_id = self.request.id
            execution.total_count = len(all_tasks)
            execution.save(update_fields=['status', 'started_at', 'celery_task_id', 'total_count', 'updated_at'])

        # 获取并发配置
        max_concurrent = suite.max_concurrent_tasks
        logger.info(f"并发配置: {max_concurrent} 个任务同时执行")
        
        # 使用asyncio执行并发测试
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _execute_tasks_concurrently(execution, all_tasks, max_concurrent)
            )
        finally:
            loop.close()
        
        # 更新执行记录为已完成
        now = timezone.now()
        TestExecution.objects.filter(pk=execution_id, status='running').update(
            status='completed', completed_at=now, updated_at=now)
        execution.refresh_from_db()
        sync_scheduled_task_result(execution)
        
        logger.info(f"测试套件执行完成: {suite.name}, "
                   f"通过: {execution.passed_count}, "
                   f"失败: {execution.failed_count}, "
                   f"错误: {execution.error_count}, "
                   f"跳过: {execution.skipped_count}")
        
        return {
            'execution_id': execution.id,
            'suite_name': suite.name,
            'status': execution.status,
            'total': execution.total_count,
            'passed': execution.passed_count,
            'failed': execution.failed_count,
            'skipped': execution.skipped_count,
            'error': execution.error_count,
            'pass_rate': execution.pass_rate,
            'duration': execution.duration
        }
        
    except TestExecution.DoesNotExist:
        error_msg = f"测试执行记录不存在: {execution_id}"
        logger.error(error_msg)
        return {'error': error_msg}
        
    except Exception as e:
        error_msg = f"执行测试套件时发生错误: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        # 尝试更新执行状态为失败
        try:
            now = timezone.now()
            if TestExecution.objects.filter(pk=execution_id, status__in=['pending', 'running']).update(
                    status='failed', completed_at=now, updated_at=now):
                sync_scheduled_task_result(TestExecution.objects.get(pk=execution_id))
        except:
            pass
            
        return {'error': error_msg}


def _prepare_suite_results(execution):
    # The parent row is locked by the caller. A pending execution can be cancelled before pickup.
    if not execution.results.exists():
        case_ids = execution.suite.testcases.values_list('id', flat=True)
        TestCaseResult.objects.bulk_create([
            TestCaseResult(execution=execution, testcase_id=case_id) for case_id in case_ids
        ])


_RESULT_COUNTS = {'pass': 'passed_count', 'fail': 'failed_count', 'skip': 'skipped_count', 'error': 'error_count'}


@transaction.atomic
def _finish_result(result):
    execution = TestExecution.objects.select_for_update().get(pk=result.execution_id)
    if execution.status != 'running':
        return
    now = timezone.now()
    saved = execution.results.filter(pk=result.pk, status='running').update(
        status=result.status, error_message=result.error_message, execution_log=result.execution_log,
        screenshots=result.screenshots, completed_at=now, updated_at=now,
        execution_time=(now - result.started_at).total_seconds(),
    )
    if saved:
        counter = _RESULT_COUNTS[result.status]
        setattr(execution, counter, getattr(execution, counter) + 1)
        execution.save(update_fields=[counter, 'updated_at'])


async def _run_result(result):
    result.started_at = timezone.now()
    pending = TestCaseResult.objects.filter(pk=result.pk, status='pending', execution__status='running')
    if not await pending.aupdate(status='running', started_at=result.started_at, updated_at=result.started_at):
        return
    result.status = 'running'
    try:
        await _execute_testcase_via_chat_api(result)
    except Exception as exc:
        result.status = 'error'
        result.error_message = str(exc)
        logger.exception('测试用例执行失败 result=%s', result.pk)
        await sync_to_async(_finish_result)(result)
        raise
    await sync_to_async(_finish_result)(result)


def execute_single_testcase(result: TestCaseResult):
    asyncio.run(_run_result(result))


async def _execute_tasks_concurrently(execution, tasks_list, max_concurrent):
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run(result):
        async with semaphore:
            await _run_result(result)

    await asyncio.gather(*(run(result) for result in tasks_list), return_exceptions=True)


@sync_to_async
def _get_testcase_steps(testcase):
    """获取测试用例步骤"""
    return list(testcase.steps.all().order_by('step_number'))

@sync_to_async
def _get_test_execution_prompt(executor):
    """获取测试用例执行提示词"""
    return UserPrompt.get_user_prompt_by_type(executor, PromptType.TEST_CASE_EXECUTION)

def _normalize_media_url(url: str) -> str:
    """
    规范化媒体URL，确保正确添加MEDIA_URL前缀
    避免双重前缀问题（如 /media//media/...）
    
    Args:
        url: 原始URL路径
        
    Returns:
        规范化后的URL
    """
    if not url:
        return url
    
    # 如果已经是完整的HTTP URL，直接返回
    if url.startswith('http://') or url.startswith('https://'):
        return url
    
    # 规范化路径分隔符（将反斜杠替换为正斜杠）
    url = url.replace('\\', '/')
    
    media_url = settings.MEDIA_URL.rstrip('/')  # 通常是 '/media'
    
    # 如果已经以 MEDIA_URL 开头，直接返回
    if url.startswith(media_url + '/') or url.startswith(media_url):
        return url
    
    # 如果以 / 开头，去掉开头的 /
    if url.startswith('/'):
        url = url[1:]
    
    return f"{media_url}/{url}"


def _reported_outcome(report, expected_steps):
    if not isinstance(report, dict) or report.get("status") not in ("pass", "fail"):
        return "error", "AI 未返回有效的结构化执行结果"
    if report["status"] == "fail":
        return "fail", str(report.get("summary") or "AI 报告执行失败")
    steps = report.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
        return "error", "AI 通过报告缺少步骤结果"
    if any(step.get("status") == "fail" for step in steps):
        return "fail", "报告中包含失败步骤"
    numbers = [step.get("step_number") for step in steps]
    if (not all(type(n) is int for n in numbers) or sorted(numbers) != sorted(expected_steps)
            or any(step.get("status") != "pass" or step.get("error") for step in steps)):
        return "error", "AI 通过报告未确认所有用例步骤执行成功"
    return "pass", ""


def _extract_test_result_json(response_text: str) -> dict | None:
    """
    从AI响应中提取测试结果JSON，支持多种格式
    
    支持的格式：
    1. 纯JSON
    2. ```json ... ``` 代码块
    3. ``` ... ``` 代码块
    4. 混合文本中的JSON对象
    
    Returns:
        解析后的JSON对象，或None
    """
    if not response_text or not response_text.strip():
        return None
    
    response_text = response_text.strip()
    
    # 预处理：处理可能的转义字符
    # 有些情况下响应中的换行符是字面量 \\n 而不是实际换行符
    normalized_text = response_text.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    # 处理转义的引号 (如 \\\" -> ")
    normalized_text = normalized_text.replace('\\"', '"')
    
    # 方法1: 尝试提取 ```json ... ``` 代码块
    json_block_pattern = r'```json\s*([\s\S]*?)\s*```'
    for text in [normalized_text, response_text]:
        matches = re.findall(json_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                result = json.loads(match.strip())
                if isinstance(result, dict) and ('status' in result or 'steps' in result):
                    logger.debug(f"从 ```json 代码块提取JSON成功")
                    return result
            except json.JSONDecodeError:
                continue
    
    # 方法2: 尝试提取普通 ``` ... ``` 代码块
    code_block_pattern = r'```\s*([\s\S]*?)\s*```'
    for text in [normalized_text, response_text]:
        matches = re.findall(code_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                result = json.loads(match.strip())
                if isinstance(result, dict) and ('status' in result or 'steps' in result):
                    logger.debug(f"从普通代码块提取JSON成功")
                    return result
            except json.JSONDecodeError:
                continue
    
    # 方法3: 尝试直接解析整个响应
    for text in [normalized_text, response_text]:
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                logger.debug(f"直接解析响应为JSON成功")
                return result
        except json.JSONDecodeError:
            pass
    
    # 方法4: 尝试从文本中提取JSON对象 (寻找 { ... } 结构)
    # 从最后一个可能的JSON对象开始查找（通常结果在末尾）
    brace_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    for text in [normalized_text, response_text]:
        matches = re.findall(brace_pattern, text)
        for match in reversed(matches):  # 从最后一个开始尝试
            try:
                result = json.loads(match)
                if isinstance(result, dict) and ('status' in result or 'steps' in result):
                    logger.debug(f"从大括号结构提取JSON成功")
                    return result
            except json.JSONDecodeError:
                continue
    
    # 方法5: 更复杂的嵌套JSON提取
    # 找到所有的 { 开始位置，然后尝试匹配到对应的 }
    for text in [normalized_text, response_text]:
        start_positions = [i for i, c in enumerate(text) if c == '{']
        for start in reversed(start_positions):  # 从最后一个开始
            brace_count = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        try:
                            candidate = text[start:i+1]
                            result = json.loads(candidate)
                            if isinstance(result, dict) and ('status' in result or 'steps' in result):
                                logger.debug(f"从嵌套结构提取JSON成功")
                                return result
                        except json.JSONDecodeError:
                            break  # 这个开始位置不行，尝试下一个
                        break
    
    return None

async def _execute_testcase_via_chat_api(result: TestCaseResult):
    """通过 Agent Loop SSE API 执行测试用例"""
    # 使用thread_sensitive=False避免死锁
    execution = await sync_to_async(lambda: result.execution, thread_sensitive=False)()
    testcase = await sync_to_async(lambda: result.testcase, thread_sensitive=False)()
    executor = await sync_to_async(lambda: execution.executor, thread_sensitive=False)()
    project = await sync_to_async(lambda: testcase.project, thread_sensitive=False)()
    # 获取是否生成脚本的配置
    generate_playwright_script = await sync_to_async(
        lambda: execution.generate_playwright_script, thread_sensitive=False
    )()

    # 调试日志：打印脚本生成配置
    logger.info(f"测试执行 ID: {execution.id}, 生成脚本配置: {generate_playwright_script}")

    if not executor or not project:
        raise Exception("无法获取执行人或项目信息")
    
    execution_log = []
    screenshots = []
    session_id = None

    try:
        # 1. 获取测试用例执行提示词
        prompt = await _get_test_execution_prompt(executor)
        if not prompt:
            raise Exception("未找到测试用例执行提示词，请先初始化系统提示词")
        
        logger.info(f"使用测试执行提示词: {prompt.name}")
        execution_log.append(f"✓ 加载测试执行提示词: {prompt.name}")
        if generate_playwright_script:
            execution_log.append("✓ 已启用脚本生成模式")
        
        # 2. 获取测试步骤
        steps = await _get_testcase_steps(testcase)
        if not steps:
            raise Exception("测试用例没有定义执行步骤")
        
        # 3. 格式化测试步骤信息
        steps_text = ""
        for step in steps:
            steps_text += f"{step.step_number}. {step.description}\n   预期结果: {step.expected_result}\n"
        
        # 4. 格式化提示词，填充测试用例信息
        from string import Template
        prompt_template = Template(prompt.content)
        formatted_prompt = prompt_template.safe_substitute(
            project_id=project.id,
            testcase_id=testcase.id,
            testcase_name=testcase.name,
            precondition=testcase.precondition or "无",
            steps=steps_text.strip()
        )
        
        logger.info(f"格式化后的提示词长度: {len(formatted_prompt)} 字符")
        execution_log.append(f"✓ 准备执行 {len(steps)} 个测试步骤")
        
        # 5. 构造 Agent Loop API 请求
        api_url = f"{settings.BASE_URL}/api/orchestrator/agent-loop/" if hasattr(settings, 'BASE_URL') else "http://localhost:8000/api/orchestrator/agent-loop/"
        
        # 生成唯一的会话ID
        session_id = f"test_exec_{execution.id}_{testcase.id}_{result.id}_{uuid.uuid4().hex[:8]}"
        
        request_data = {
            "message": formatted_prompt,
            "session_id": session_id,
            "project_id": str(project.id),
            "prompt_id": str(prompt.id),
            "use_knowledge_base": False,
            "generate_playwright_script": generate_playwright_script,
            "test_case_id": testcase.id
        }
        
        logger.info(f"调用 Agent Loop API: {api_url}")
        logger.info(f"会话ID: {session_id}")
        execution_log.append(f"✓ 开始与AI测试引擎通信...")

        # 会话标题设为可读（agent-loop 默认标题取提示词开头，不可读）；
        # 标题含"测试执行"供对话页识别为外部任务会话（打字机订阅判定）
        try:
            from langgraph_integration.models import ChatSession

            await sync_to_async(ChatSession.objects.filter(session_id=session_id).update)(
                title=f"套件执行 - {execution.execution_id} - {testcase.name}"
            )
        except Exception:
            pass
        
        # 6. 生成认证令牌并调用 Agent Loop SSE API
        def generate_token():
            refresh = RefreshToken.for_user(executor)
            return str(refresh.access_token)
        
        access_token = await sync_to_async(generate_token)()
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream'
        }
        
        # 收集 SSE 流式响应
        final_response = ""
        current_step_response = ""  # 当前步骤的响应内容
        step_count = 0
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                'POST',
                api_url,
                json=request_data,
                headers=headers
            ) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line or not line.startswith('data: '):
                        continue
                    
                    try:
                        data_str = line[6:]  # 去掉 'data: ' 前缀
                        if data_str == '[DONE]':
                            break
                        
                        data = json.loads(data_str)
                        event_type = data.get('type', '')

                        # 实时桥接到对话页 WS（打字机效果，best-effort 不影响执行）：
                        # 套件会话与正常聊天同构渲染（AI 气泡逐字 + 工具卡片）；
                        # async 消费循环内必须用 await 异步入口（async_to_sync 在
                        # 运行中的事件循环内调用会抛 RuntimeError）
                        try:
                            from orchestrator_integration.stream_bridge import publish_stream_event_async
                            await publish_stream_event_async(session_id, {
                                'type': event_type,
                                'data': data.get('data'),
                                'message': data.get('message'),
                                'summary': data.get('summary'),
                                'tool_name': data.get('name') or data.get('tool'),
                                'step': data.get('step'),
                                'content': data.get('content'),
                                'session_id': session_id,
                            })
                        except Exception:
                            pass

                        if event_type == 'step_start':
                            step_count += 1
                            current_step_response = ""  # 重置当前步骤响应
                            execution_log.append(f"\n🔄 AI执行步骤 {step_count}")
                        
                        elif event_type == 'stream':
                            # 流式响应：每个事件包含一小段文本
                            stream_data = data.get('data', '')
                            if stream_data:
                                final_response += stream_data
                                current_step_response += stream_data
                        
                        elif event_type == 'content':
                            content = data.get('content', '')
                            if content:
                                final_response += content
                        
                        elif event_type == 'message':
                            # Agent Loop 的 message 事件包含 AI 的响应（思考过程）
                            msg_data = data.get('data', '')
                            if msg_data:
                                final_response += msg_data
                                # 显示 AI 的说明（前150字符）
                                short_msg = msg_data[:150].replace('\n', ' ').strip()
                                if len(msg_data) > 150:
                                    short_msg += '...'
                                if short_msg:
                                    execution_log.append(f"   💬 {short_msg}")
                        
                        elif event_type == 'tool_call':
                            tool_name = data.get('name', data.get('tool', ''))
                            tool_args = data.get('arguments', data.get('args', ''))
                            if tool_name:
                                execution_log.append(f"   🔧 调用工具: {tool_name}")
                            if tool_args and isinstance(tool_args, str) and len(tool_args) > 0:
                                # 只显示参数的前100个字符
                                short_args = tool_args[:100] + '...' if len(tool_args) > 100 else tool_args
                                execution_log.append(f"      参数: {short_args}")
                        
                        elif event_type == 'tool_start':
                            # 工具开始执行
                            tool_name = data.get('name', data.get('tool', ''))
                            if tool_name:
                                execution_log.append(f"   🔧 调用工具: {tool_name}")
                        
                        elif event_type == 'tool_result':
                            # 工具执行结果
                            result_summary = data.get('summary', '')
                            if result_summary:
                                # 只显示结果摘要的前150字符
                                short_result = result_summary[:150].replace('\n', ' ')
                                if len(result_summary) > 150:
                                    short_result += '...'
                                execution_log.append(f"   🔧 工具结果: {short_result}")
                        
                        elif event_type == 'stream_end':
                            # 流式响应结束，输出当前步骤的响应摘要
                            if current_step_response.strip():
                                summary = current_step_response.strip()[:200].replace('\n', ' ')
                                if len(current_step_response.strip()) > 200:
                                    summary += '...'
                                execution_log.append(f"   📝 {summary}")
                        
                        elif event_type == 'step_end' or event_type == 'step_complete':
                            # 步骤完全结束信号，tool_result已显示工具结果，此处不再重复
                            pass
                        
                        elif event_type == 'final':
                            final_response = data.get('content', final_response)
                        
                        elif event_type == 'ai':
                            # AI消息事件，检查是否是最终响应
                            content = data.get('content', '')
                            agent_type = data.get('agent_type', '')
                            if agent_type == 'final' and content:
                                # 这是最终AI响应，包含测试结果JSON
                                final_response = content
                                logger.info(f"收到最终AI响应, 长度: {len(content)}")
                            elif content:
                                # 普通AI响应，累加到final_response
                                final_response += content
                        
                        elif event_type == 'error':
                            error_msg = data.get('message', '未知错误')
                            execution_log.append(f"   ❌ 错误: {error_msg}")
                            raise Exception(error_msg)
                    
                    except json.JSONDecodeError:
                        continue
        
        logger.info(f"Agent Loop 执行完成，共 {step_count} 个步骤")
        
        # 7. 尝试从最终响应中提取JSON格式的测试结果
        if final_response:
            logger.debug(f"最终响应前100字符: {final_response[:100] if len(final_response) > 100 else final_response}")
            logger.debug(f"最终响应是否包含```json: {'```json' in final_response}")
        test_result_json = _extract_test_result_json(final_response)
        result.status, result.error_message = _reported_outcome(test_result_json, [step.step_number for step in steps])
        execution_log.append(f"测试结果: {result.status}")
        if result.error_message:
            execution_log.append(result.error_message)
        if test_result_json:
            execution_log.append(str(test_result_json.get("summary") or ""))
            step_reports = test_result_json.get("steps")
            for step_report in step_reports if isinstance(step_reports, list) else []:
                if isinstance(step_report, dict):
                    execution_log.append(f"[步骤 {step_report.get('step_number')}] {step_report.get('status')} "
                                         f"{step_report.get('description', '')} {step_report.get('error') or ''}")
        elif final_response:
            execution_log.append(f"实际响应: {final_response[:1000]}")

        # 获取测试用例的截图
        try:
            testcase_screenshots = await sync_to_async(
                lambda: list(testcase.screenshots.filter(
                    step_number__isnull=False
                ).order_by('step_number').values_list('screenshot', flat=True))
            )()
            
            if testcase_screenshots:
                screenshots = [_normalize_media_url(url) for url in testcase_screenshots]
                logger.info(f"从测试用例获取到 {len(screenshots)} 个截图URL")
        except Exception as e:
            logger.warning(f"获取测试用例截图失败: {e}")
        
    except httpx.HTTPError as e:
        error_msg = f"调用 Agent Loop API 失败: {str(e)}"
        execution_log.append(f"\n✗ {error_msg}")
        logger.error(error_msg, exc_info=True)
        raise Exception(error_msg)
    
    except Exception as e:
        error_msg = f"执行过程异常: {str(e)}"
        execution_log.append(f"\n✗ {error_msg}")
        logger.error(error_msg, exc_info=True)
        raise
    
    finally:
        # Always release MCP/browser session resources, including failure paths.
        if session_id is not None:
            try:
                from mcp_tools.persistent_client import mcp_session_manager
                await mcp_session_manager.cleanup_user_session(
                    user_id=str(executor.id),
                    project_id=str(project.id),
                    session_id=session_id,
                )
                logger.info(f"MCP session cleaned: {session_id}")
                execution_log.append("✓ browser/MCP session resources cleaned")
            except Exception as e:
                logger.warning(f"MCP session cleanup failed: {e}")

        result.execution_log = "\n".join(execution_log)
        result.screenshots = screenshots


@shared_task(name='testcases.cancel_test_execution')
@transaction.atomic
def cancel_test_execution(execution_id):
    try:
        execution = TestExecution.objects.select_for_update().get(pk=execution_id)
    except TestExecution.DoesNotExist:
        return {'success': False, 'message': '测试执行记录不存在'}
    if execution.status == 'cancelled':
        return {'success': True, 'message': '测试执行已取消'}
    if execution.status not in ('pending', 'running'):
        return {'success': False, 'message': f'无法取消状态为 {execution.status} 的执行'}

    if execution.status == 'pending':
        _prepare_suite_results(execution)
    now = timezone.now()
    execution.results.filter(status__in=['pending', 'running']).update(
        status='skip', completed_at=now, updated_at=now)
    counts = dict(execution.results.order_by().values_list('status').annotate(n=Count('pk')))
    for status, field in _RESULT_COUNTS.items():
        setattr(execution, field, counts.get(status, 0))
    execution.status = 'cancelled'
    execution.completed_at = now
    execution.total_count = sum(counts.values())
    execution.save(update_fields=['status', 'completed_at', 'total_count', 'updated_at', *_RESULT_COUNTS.values()])
    return {'success': True, 'message': '测试执行已取消', 'celery_task_id': execution.celery_task_id}

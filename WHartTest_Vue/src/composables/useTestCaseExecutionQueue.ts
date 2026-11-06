/**
 * 用例执行任务队列管理器
 * 提供全局统一的用例执行状态、进度与实时日志管理
 */

import { ref, computed } from 'vue';
import { useAuthStore } from '@/store/authStore';
import { useProjectStore } from '@/store/projectStore';
import type { AiDiagnosisResult } from '@/services/testcaseService';

export interface ExecutionTaskLog {
  time: string;
  message: string;
  type?: 'info' | 'success' | 'warning' | 'error';
}

export interface ExecutionTask {
  id: string;
  testCaseId: number;
  testCaseName: string;
  moduleName?: string;
  executionMode: 'hybrid' | 'script_only' | 'ai_only';
  status: 'pending' | 'running' | 'success' | 'failed' | 'diagnosing';
  totalSteps: number;
  currentStepIndex: number;
  currentStepDesc: string;
  startTime: number;
  endTime?: number;
  duration?: number;
  logs: ExecutionTaskLog[];
  errorMessage?: string;
  aiDiagnosis?: AiDiagnosisResult | null;
  uiRecordId?: number;
}

// 单例响应式状态
const allTasks = ref<(ExecutionTask & { projectId: number; userId: number })[]>([]);

export function resetExecutionQueue() {
  allTasks.value = [];
}

export function useTestCaseExecutionQueue() {
  const projects = useProjectStore();
  const auth = useAuthStore();
  const isCurrentScope = (task: typeof allTasks.value[number]) =>
    task.userId === auth.user?.id && task.projectId === projects.currentProjectId;
  const tasks = computed(() => allTasks.value.filter(isCurrentScope));
  const runningTasksCount = computed(() => {
    return tasks.value.filter(t => t.status === 'running' || t.status === 'diagnosing' || t.status === 'pending').length;
  });

  const successTasksCount = computed(() => {
    return tasks.value.filter(t => t.status === 'success').length;
  });

  const failedTasksCount = computed(() => {
    return tasks.value.filter(t => t.status === 'failed').length;
  });

  const addTask = (taskData: Omit<ExecutionTask, 'id' | 'startTime' | 'logs' | 'currentStepIndex' | 'currentStepDesc' | 'status'> & { status?: ExecutionTask['status'] }): ExecutionTask => {
    const now = Date.now();
    const timeStr = new Date().toLocaleTimeString();
    const newTask: ExecutionTask = {
      ...taskData,
      id: `task-${now}-${Math.floor(Math.random() * 1000)}`,
      status: taskData.status || 'running',
      currentStepIndex: 0,
      currentStepDesc: '任务初始化中...',
      startTime: now,
      logs: [
        {
          time: timeStr,
          message: `执行任务已启动（${taskData.executionMode === 'hybrid' ? '智能双模' : taskData.executionMode === 'script_only' ? '原生脚本' : '纯AI探索'}）`,
          type: 'info',
        },
      ],
    };
    if (!auth.user || !projects.currentProjectId) throw new Error('执行任务需要登录并选择项目');
    allTasks.value.unshift({ ...newTask, projectId: projects.currentProjectId, userId: auth.user.id });
    return newTask;
  };

  const updateTask = (taskId: string, patch: Partial<ExecutionTask>) => {
    const task = allTasks.value.find(t => t.id === taskId);
    if (task) {
      Object.assign(task, patch);
      if (patch.status === 'success' || patch.status === 'failed') {
        if (!task.endTime) {
          task.endTime = Date.now();
          task.duration = Math.round((task.endTime - task.startTime) / 1000);
        }
      }
    }
  };

  const addStepLog = (taskId: string, message: string, type: ExecutionTaskLog['type'] = 'info') => {
    const task = allTasks.value.find(t => t.id === taskId);
    if (task) {
      const timeStr = new Date().toLocaleTimeString();
      task.logs.push({ time: timeStr, message, type });
    }
  };

  const finishTask = (
    taskId: string,
    status: 'success' | 'failed',
    errorMessage?: string,
    aiDiagnosis?: AiDiagnosisResult | null
  ) => {
    const task = allTasks.value.find(t => t.id === taskId);
    if (task) {
      const now = Date.now();
      task.status = status;
      task.endTime = now;
      task.duration = Math.round((now - task.startTime) / 1000);
      if (errorMessage) {
        task.errorMessage = errorMessage;
        addStepLog(taskId, errorMessage, 'error');
      }
      if (aiDiagnosis !== undefined) {
        task.aiDiagnosis = aiDiagnosis;
      }
      if (status === 'success') {
        task.currentStepIndex = task.totalSteps;
        addStepLog(taskId, '用例全部步骤执行通过！', 'success');
      }
    }
  };

  const removeTask = (taskId: string) => {
    const index = allTasks.value.findIndex(t => t.id === taskId);
    if (index > -1) {
      allTasks.value.splice(index, 1);
    }
  };

  const clearCompletedTasks = () => {
    allTasks.value = allTasks.value.filter(t => !isCurrentScope(t) || t.status === 'running' || t.status === 'diagnosing' || t.status === 'pending');
  };

  const getTaskByTestCaseId = (testCaseId: number) => {
    return tasks.value.find(t => t.testCaseId === testCaseId && (t.status === 'running' || t.status === 'diagnosing'));
  };

  return {
    tasks,
    runningTasksCount,
    successTasksCount,
    failedTasksCount,
    addTask,
    updateTask,
    addStepLog,
    finishTask,
    removeTask,
    clearCompletedTasks,
    getTaskByTestCaseId,
  };
}

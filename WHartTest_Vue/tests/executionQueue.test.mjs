// Run from WHartTest_Vue after npm ci: node tests/executionQueue.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';
import * as vue from 'vue';

const auth = vue.reactive({ user: { id: 1 } });
const projects = vue.reactive({ currentProjectId: 11 });
const dependencies = {
  vue,
  '@/store/authStore': { useAuthStore: () => auth },
  '@/store/projectStore': { useProjectStore: () => projects },
};
const source = readFileSync(new URL('../src/composables/useTestCaseExecutionQueue.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});
const queueModule = {};
runInNewContext(outputText, { exports: queueModule, require: name => dependencies[name] });
const queue = queueModule.useTestCaseExecutionQueue();
const task = queue.addTask({ testCaseId: 42, testCaseName: 'A', executionMode: 'script_only', totalSteps: 1 });
projects.currentProjectId = 22;
assert.equal(queue.tasks.value.length, 0);
queue.finishTask(task.id, 'failed', 'Private log');
queue.clearCompletedTasks();
projects.currentProjectId = 11;
assert.equal(queue.failedTasksCount.value, 1);
assert.equal(queue.tasks.value[0].errorMessage, 'Private log');
auth.user.id = 2;
assert.equal(queue.tasks.value.length, 0);
queue.clearCompletedTasks();
auth.user.id = 1;
assert.equal(queue.failedTasksCount.value, 1);
queueModule.resetExecutionQueue();
queue.addStepLog(task.id, 'Late event after logout');
assert.equal(queue.tasks.value.length, 0);

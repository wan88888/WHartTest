// Run after npm ci with node tests/apiTabsScope.test.mjs.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import * as pinia from 'pinia';
import * as vue from 'vue';
import ts from 'typescript';

const account = vue.reactive({ user: { id: 7 } });
const projects = vue.reactive({ currentProjectId: 10 });
const storage = new Map();
const modules = {
  pinia, vue,
  '@/store/authStore': { useAuthStore: () => account },
  '@/store/projectStore': { useProjectStore: () => projects },
};
const source = readFileSync(new URL('../src/features/api-testing/stores/apiTabsStore.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});
const api = {};
runInNewContext(outputText, {
  exports: api,
  require: name => modules[name],
  localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
});
pinia.setActivePinia(pinia.createPinia());
storage.set('api-testing-tabs', JSON.stringify({ tabs: [{ id: 'unscoped' }] }));
const tabs = api.useApiTabsStore();
assert.equal(tabs.tabs.length, 0);
const first = tabs.createTab();
tabs.updateTabRequest(first, { name: 'project 10 draft' });
tabs.updateTabResponse(first, { data: 'result' });
projects.currentProjectId = 20;
assert.equal(tabs.tabs.length, 0);
const second = tabs.createTab();
projects.currentProjectId = 10;
assert.equal(tabs.activeTabId, first);
assert.equal(tabs.tabs[0].name, 'project 10 draft');
assert.equal(tabs.tabs[0].response, undefined);
account.user.id = 8;
assert.equal(tabs.tabs.length, 0);
account.user.id = 7;
projects.currentProjectId = 20;
assert.equal(tabs.activeTabId, second);
await vue.nextTick();
pinia.setActivePinia(pinia.createPinia());
assert.equal(api.useApiTabsStore().activeTabId, second);

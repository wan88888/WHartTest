// 项目状态仓库
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { getProjectList, type Project } from '@/services/projectService';

const PROJECT_STORAGE_KEY = 'selected_project_id';

export const useProjectStore = defineStore('project', () => {
  // 当前选中的项目
  const currentProject = ref<Project | null>(null);
  
  // 项目列表
  const projectList = ref<Project[]>([]);
  
  // 加载状态
  const loading = ref(false);
  
  // 错误信息
  const error = ref<string | null>(null);
  
  // 计算属性：当前项目ID
  const currentProjectId = computed(() => currentProject.value?.id || null);
  
  // 计算属性：项目选项列表（用于下拉选择器）
  const projectOptions = computed(() => {
    return projectList.value.map(project => ({
      label: project.name,
      value: project.id
    }));
  });

  // 从localStorage获取保存的项目ID
  const getSavedProjectId = (): number | null => {
    try {
      const savedId = localStorage.getItem(PROJECT_STORAGE_KEY);
      return savedId ? parseInt(savedId, 10) : null;
    } catch (error) {
      console.warn('读取保存的项目ID失败:', error);
      return null;
    }
  };

  const setCurrentProject = (project: Project | null) => {
    currentProject.value = project;
    try {
      if (project) localStorage.setItem(PROJECT_STORAGE_KEY, String(project.id));
      else localStorage.removeItem(PROJECT_STORAGE_KEY);
    } catch (err) {
      console.warn('保存当前项目失败:', err);
    }
  };

  const applyProjects = (projects: Project[]) => {
    const preferredId = currentProjectId.value ?? getSavedProjectId();
    projectList.value = projects;
    setCurrentProject(projects.find(p => p.id === preferredId) ?? projects[0] ?? null);
  };

  let requestVersion = 0;
  const fetchProjects = async () => {
    const version = ++requestVersion;
    loading.value = true;
    error.value = null;
    try {
      const response = await getProjectList();
      if (version !== requestVersion) return;
      if (response.success && response.data) applyProjects(response.data);
      else error.value = response.error || '获取项目列表失败';
    } catch {
      if (version === requestVersion) error.value = '获取项目列表时发生错误';
    } finally {
      if (version === requestVersion) loading.value = false;
    }
  };

  const removeProject = (id: number) => {
    ++requestVersion;
    loading.value = false;
    applyProjects(projectList.value.filter(project => project.id !== id));
  };

  // 根据ID设置当前项目
  const setCurrentProjectById = (projectId: number) => {
    const project = projectList.value.find(p => p.id === projectId);
    if (project) {
      setCurrentProject(project);
    }
  };
  
  // 重置状态（用于用户退出时清空）
  const reset = () => {
    ++requestVersion;
    currentProject.value = null;
    projectList.value = [];
    error.value = null;
    loading.value = false;
    // 清除 localStorage 中保存的项目ID
    try {
      localStorage.removeItem(PROJECT_STORAGE_KEY);
    } catch (err) {
      console.warn('清除保存的项目ID失败:', err);
    }
  };
  
  return {
    currentProject,
    currentProjectId,
    projectList,
    projectOptions,
    loading,
    error,
    fetchProjects,
    removeProject,
    setCurrentProject,
    setCurrentProjectById,
    reset
  };
});

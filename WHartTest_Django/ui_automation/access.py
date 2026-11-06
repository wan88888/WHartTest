"""Project boundaries shared by the UI API, recorder and task dispatcher."""
from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied, ValidationError

from projects.models import Project, ProjectMember
from .auth_state import credential_summary
from .models import (
    UiAuthState, UiCaseStepsDetailed, UiElement, UiEnvironmentConfig, UiExecutionRecord,
    UiPageSteps, UiPageStepsDetailed, UiTestCase,
)

EXECUTE = 'ui_automation.add_uiexecutionrecord'
MANAGE_ACTUATORS = 'ui_automation.manage_actuators'
RECORD_PERMS = ('ui_automation.change_uipagesteps', 'ui_automation.add_uipagestepsdetailed',
                'ui_automation.add_uielement', 'ui_automation.change_uielement')

# The path from each resource to its owning project. Other UI resources own a direct FK.
PROJECT_PATHS = {
    UiAuthState: 'env_config__project',
    UiElement: 'page__project',
    UiCaseStepsDetailed: 'test_case__project',
    UiPageStepsDetailed: 'page_step__project',
    UiExecutionRecord: 'test_case__project',
}


def project_queryset(qs, user):
    if not user or not user.is_authenticated or not user.is_active:
        return qs.none()
    if user.is_superuser:
        return qs
    path = PROJECT_PATHS.get(qs.model, 'project')
    return qs.filter(**{path + '_id__in': ProjectMember.objects.filter(user=user).values('project_id')})


def require_project(user, project_id, *permissions):
    if not user or not user.is_authenticated or not user.is_active or not user.has_perms(permissions):
        raise PermissionDenied('缺少执行此操作的权限')
    if not user.is_superuser and not ProjectMember.objects.filter(user=user, project_id=project_id).exists():
        raise PermissionDenied('无权访问该项目')


def object_project_id(obj):
    if isinstance(obj, Project):
        return obj.pk
    path = PROJECT_PATHS.get(type(obj), 'project').split('__')
    for field in path[:-1]:
        obj = getattr(obj, field)
    return getattr(obj, path[-1] + '_id')


def validate_project_relations(serializer, data):
    model = serializer.Meta.model
    owner_field = PROJECT_PATHS.get(model, 'project').split('__')[0]
    instance = serializer.instance
    owner = data.get(owner_field) or getattr(instance, owner_field, None)
    if owner is None:
        raise ValidationError({owner_field: '请选择所属项目资源'})
    project_id = object_project_id(owner)
    if instance and object_project_id(instance) != project_id:
        raise ValidationError({owner_field: '不能更改资源所属项目'})
    request = serializer.context.get('request')
    if request:
        require_project(request.user, project_id)
    for name, value in data.items():
        if value is not None and hasattr(value, '_meta') and value._meta.app_label == 'ui_automation':
            if object_project_id(value) != project_id:
                raise ValidationError({name: '关联资源必须属于同一项目'})
    return data


def positive_ids(values):
    if not isinstance(values, list) or not values or any(type(i) is not int or i <= 0 for i in values):
        raise ValidationError('请选择有效的执行目标')
    if len(values) != len(set(values)):
        raise ValidationError('执行目标重复')
    return values


def execution_context(user, *, case_ids=None, page_step_id=None, env_id=None):
    """Authorize the complete task before reserving a slot or sending any credentials."""
    if not user or not user.is_authenticated or not user.is_active or not user.has_perm(EXECUTE):
        raise PermissionDenied('缺少 UI 自动化执行权限')
    if case_ids is not None:
        ids = positive_ids(case_ids)
        cases = list(project_queryset(UiTestCase.objects.all(), user).filter(pk__in=ids)
                     .values_list('project_id', 'env_config__project_id'))
        if len(cases) != len(ids):
            raise PermissionDenied('用例不存在或无权访问')
        project_id = cases[0][0]
        if any(p != project_id or env not in (None, project_id) for p, env in cases):
            raise ValidationError('用例和环境必须属于同一项目')
        groups = UiPageSteps.objects.filter(case_usages__test_case_id__in=ids)
    else:
        positive_ids([page_step_id])
        groups = project_queryset(UiPageSteps.objects.all(), user).filter(pk=page_step_id)
        project_id = groups.values_list('project_id', flat=True).first()
        if project_id is None:
            raise PermissionDenied('页面步骤不存在或无权访问')
    if env_id is not None:
        positive_ids([env_id])
        if not UiEnvironmentConfig.objects.filter(pk=env_id, project_id=project_id).exists():
            raise ValidationError('执行环境必须属于当前项目')
    if (groups.exclude(project_id=project_id).exists() or groups.exclude(page__project_id=project_id).exists()
            or UiPageStepsDetailed.objects.filter(page_step__in=groups, element__isnull=False)
            .exclude(element__page__project_id=project_id).exists()):
        raise ValidationError('页面步骤包含其他项目的资源')
    states = UiAuthState.objects.filter(pk__in=groups.values('auth_state_id'))
    if states.exclude(env_config__project_id=project_id).exists():
        raise ValidationError('绑定的登录态必须属于当前项目')
    if states.filter(is_active=False).exists():
        raise ValidationError('步骤绑定的登录态已停用')
    default_env = UiEnvironmentConfig.objects.filter(project_id=project_id, is_default=True).values_list('pk', flat=True).first()
    applicable_env = env_id or default_env
    if case_ids is None:
        bindings = ((applicable_env, g) for g in groups.values_list('auth_state__env_config_id', flat=True))
    else:
        bindings = ((bound or applicable_env, auth_env) for bound, auth_env in
                    UiCaseStepsDetailed.objects.filter(test_case_id__in=ids)
                    .values_list('test_case__env_config_id', 'page_step__auth_state__env_config_id'))
    if any(env and auth_env and env != auth_env for env, auth_env in bindings):
        raise ValidationError('步骤绑定登录态与执行环境不一致')
    snapshots = {str(pk): {'env_config': env, 'state_json': state}
                 for pk, env, state in states.values_list('id', 'env_config_id', 'state_json')}
    for snapshot in snapshots.values():
        credential_summary(snapshot['state_json'])
    return project_id, snapshots


def current_user(pk):
    return get_user_model().objects.filter(pk=pk, is_active=True).first()

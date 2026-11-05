"""Feishu OAuth login, with browser-bound state and PKCE."""
import base64
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.contrib.auth.models import Group, Permission, User

from .models import FeishuIdentity, FeishuLoginAttempt
from .serializers import MyTokenObtainPairSerializer, session_user_data

AUTHORIZE_URL = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
# The authorize endpoint documents v2 for PKCE; v3 currently returns 20049
# for this flow. Keep PKCE enabled and exchange each code only once.
# https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code
TOKEN_URL = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token'
USER_INFO_URL = 'https://open.feishu.cn/open-apis/authen/v1/user_info'
COOKIE_NAME = 'wharttest_feishu_browser'
ATTEMPT_TTL = 300
logger = logging.getLogger(__name__)

# Only static messages and numeric status codes may leave the provider boundary.
# Provider descriptions can contain credentials or other request data.
TOKEN_ERRORS = {
    20002: '飞书应用凭证无效，请管理员检查 App ID 和 App Secret。',
    20003: '飞书授权码无效或已使用，请返回登录页重新授权。',
    20004: '飞书授权码已过期，请返回登录页重新授权。',
    20008: '飞书用户不存在，请检查当前账号状态。',
    20009: '企业尚未安装该飞书应用，请联系应用管理员。',
    20010: '当前账号没有该飞书应用的使用权限，请联系管理员检查应用可用范围。',
    20024: '飞书授权码与当前应用不匹配，请返回登录页重新授权。',
    20027: '飞书应用缺少请求的用户身份权限，请管理员开通相应权限并发布应用版本后重新授权。',
    20048: '飞书应用不存在，请管理员检查 App ID。',
    20049: '飞书授权安全校验失败（PKCE），请返回登录页重新授权；若仍失败请联系管理员。',
    20050: '飞书授权服务异常，请稍后重新授权。',
    20065: '飞书授权码已使用，请返回登录页重新授权。',
    20066: '飞书账号状态异常，请联系企业管理员。',
    20068: '飞书授权权限范围不匹配，请联系应用管理员。',
    20069: '飞书应用尚未启用，请联系应用管理员。',
    20070: '飞书令牌请求存在重复的身份验证方式，请联系管理员。',
    20071: '飞书回调地址与发起授权时不一致，请返回登录页重新授权。',
    20072: '飞书授权服务暂不可用，请稍后重新授权。',
}


def token_error(payload, http_status):
    value = payload.get('code') if isinstance(payload, dict) else None
    provider_code = value if type(value) is int and 0 < value < 1000000000 else None
    logger.warning('Feishu token exchange rejected: http_status=%s provider_code=%s',
                   http_status, provider_code)
    message = TOKEN_ERRORS.get(provider_code, '飞书令牌交换失败，请联系管理员检查登录配置。')
    if provider_code is not None:
        message += f'（飞书错误码：{provider_code}）'
    return FeishuLoginError(message)


class FeishuLoginError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def is_configured():
    if not all((settings.FEISHU_LOGIN_ENABLED, settings.FEISHU_APP_ID,
                settings.FEISHU_APP_SECRET, settings.FEISHU_REDIRECT_URI,
                settings.FEISHU_ALLOWED_TENANT_KEYS)):
        return False
    try:
        uri = urlsplit(settings.FEISHU_REDIRECT_URI)
    except ValueError:
        return False
    local_http = settings.DEBUG and uri.scheme == 'http' and uri.hostname in ('localhost', '127.0.0.1')
    return bool((uri.scheme == 'https' or local_http) and uri.hostname
                and not uri.username and not uri.password and not uri.query and not uri.fragment
                and uri.path == '/auth/feishu/callback')


@never_cache
@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def login_config(request):
    enabled = is_configured()
    return Response({'enabled': enabled, 'message': '' if enabled else '飞书登录尚未配置，请联系管理员。'})


@never_cache
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def authorize(request):
    return start_authorization(request)


def start_authorization(request, *, binding='', scopes=()):
    if not is_configured():
        return Response({'error': '飞书登录尚未配置，请联系管理员。'}, status=503)
    # The callback must return to the same browser origin that receives the binding cookie.
    callback = urlsplit(settings.FEISHU_REDIRECT_URI)
    origin = f'{callback.scheme}://{callback.netloc}'
    request_origin = request.headers.get('Origin') or request.build_absolute_uri('/').rstrip('/')
    if request_origin != origin:
        return Response({'error': f'请从 {origin} 打开登录页，当前地址与飞书回调地址不一致。'}, status=400)
    now = timezone.now()
    FeishuLoginAttempt.objects.filter(expires_at__lte=now).delete()
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    if binding:
        state = 'doc.' + state
    FeishuLoginAttempt.objects.create(
        state_hash=_hash(binding + state), browser_hash=_hash(browser_secret), code_verifier=verifier,
        app_id=settings.FEISHU_APP_ID, redirect_uri=settings.FEISHU_REDIRECT_URI,
        expires_at=now + timedelta(seconds=ATTEMPT_TTL),
    )
    params = {
        'client_id': settings.FEISHU_APP_ID, 'redirect_uri': settings.FEISHU_REDIRECT_URI,
        'response_type': 'code', 'state': state,
        'prompt': 'consent',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    }
    if scopes:
        params['scope'] = ' '.join(scopes)
    url = AUTHORIZE_URL + '?' + urlencode(params)
    response = Response({'authorize_url': url}, headers={'Cache-Control': 'no-store, private'})
    response.set_cookie(COOKIE_NAME + ('_doc' if binding else ''), browser_secret, max_age=ATTEMPT_TTL,
                        httponly=True, secure=callback.scheme == 'https', samesite='Lax')
    return response


class CallbackSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    state = serializers.CharField(max_length=128)
    error = serializers.CharField(max_length=256, required=False, allow_blank=True)


@sensitive_variables()
def exchange_code(code, attempt):
    try:
        token_response = requests.post(TOKEN_URL, json={
            'grant_type': 'authorization_code', 'client_id': attempt.app_id,
            'client_secret': settings.FEISHU_APP_SECRET, 'code': code,
            'redirect_uri': attempt.redirect_uri, 'code_verifier': attempt.code_verifier,
        }, timeout=(3, 10), allow_redirects=False)
        token = token_response.json()
        if (token_response.status_code != 200 or not isinstance(token, dict)
                or token.get('code') != 0 or not isinstance(token.get('access_token'), str)
                or not token['access_token']):
            raise token_error(token, token_response.status_code)
        return token['access_token']
    except (requests.RequestException, ValueError):
        raise FeishuLoginError('暂时无法连接飞书，请稍后重新授权。', 502) from None


@sensitive_variables()
def fetch_user_info(access_token):
    try:
        profile_response = requests.get(USER_INFO_URL,
            headers={'Authorization': f"Bearer {access_token}"},
            timeout=(3, 10), allow_redirects=False)
        profile = profile_response.json()
        if (profile_response.status_code != 200 or not isinstance(profile, dict)
                or profile.get('code') != 0 or not isinstance(profile.get('data'), dict)):
            raise FeishuLoginError('无法验证飞书身份，请确认账号有效且具有应用使用权限。', 403)
        return profile['data']
    except (requests.RequestException, ValueError):
        # Never echo provider responses, authorization codes, secrets or access tokens.
        raise FeishuLoginError('暂时无法连接飞书，请稍后重新授权。', 502) from None


@transaction.atomic
def default_tester_group():
    group, created = Group.objects.get_or_create(name='普通测试成员')
    if created:
        models = ('testcase', 'testcasemodule', 'testcasestep', 'testcasescreenshot',
                  'testsuite', 'testexecution', 'testcaseresult')
        codenames = [f'{action}_{model}' for model in models for action in ('view', 'add', 'change')]
        permissions = Permission.objects.filter(
            Q(content_type__app_label='projects', codename='view_project')
            | Q(content_type__app_label='testcases', codename__in=codenames)
        )
        group.permissions.set(permissions)
    return group


def resolve_user(profile):
    open_id, tenant_key = profile.get('open_id'), profile.get('tenant_key')
    if not all(isinstance(value, str) and 0 < len(value) <= 128 for value in (open_id, tenant_key)):
        raise FeishuLoginError('飞书未返回有效的用户或企业标识。', 403)
    if tenant_key not in settings.FEISHU_ALLOWED_TENANT_KEYS:
        raise FeishuLoginError('当前飞书账号不属于允许登录的企业。', 403)
    key = {'app_id': settings.FEISHU_APP_ID, 'open_id': open_id}
    identity = FeishuIdentity.objects.select_related('user').filter(**key).first()
    if identity is None:
        if not settings.FEISHU_AUTO_CREATE_USER:
            raise FeishuLoginError('该飞书账号尚未关联平台用户，请联系管理员。', 403)
        name = profile.get('name') if isinstance(profile.get('name'), str) else ''
        try:
            with transaction.atomic():
                # No email/name matching: provider contact fields are not verified credentials.
                user = User.objects.create_user(username='feishu_' + uuid.uuid4().hex,
                                                password=None, first_name=name[:150])
                user.groups.add(default_tester_group())
                identity = FeishuIdentity.objects.create(user=user, tenant_key=tenant_key,
                                                        display_name=name[:150], **key)
        except IntegrityError:
            # A concurrent login can create the same identity; the losing user insert rolls back.
            identity = FeishuIdentity.objects.select_related('user').filter(**key).first()
            if identity is None:
                raise FeishuLoginError('账号创建冲突，请重新授权。', 409) from None
    if identity.tenant_key != tenant_key or not identity.user.is_active:
        raise FeishuLoginError('平台账号已停用或企业身份不匹配，请联系管理员。', 403)
    # Refresh provider presentation data on every verified login, including existing
    # identities. Keep platform usernames and locally uploaded avatars unchanged.
    name = profile.get('name')
    identity.display_name = name.strip()[:150] if isinstance(name, str) else ''
    identity.avatar_url = ''
    for field in ('avatar_thumb', 'avatar_url', 'avatar_middle', 'avatar_big'):
        value = profile.get(field)
        if not isinstance(value, str) or len(value) > 2048:
            continue
        try:
            uri = urlsplit(value)
        except ValueError:
            continue
        if uri.scheme == 'https' and uri.hostname and not uri.username and not uri.password:
            identity.avatar_url = value
            break
    identity.save(update_fields=['display_name', 'avatar_url'])
    return identity.user


def consume_attempt(request, state, *, binding=''):
    now = timezone.now()
    attempts = FeishuLoginAttempt.objects.filter(
        state_hash=_hash(binding + state), browser_hash=_hash(request.COOKIES.get(COOKIE_NAME + ('_doc' if binding else ''), '')),
        app_id=settings.FEISHU_APP_ID, redirect_uri=settings.FEISHU_REDIRECT_URI,
        expires_at__gt=now, consumed_at__isnull=True,
    )
    attempt = attempts.first()
    if attempt is None or attempts.update(consumed_at=now) != 1:
        raise FeishuLoginError('授权请求已过期、已使用或与当前浏览器不匹配，请重新授权。')
    return attempt


@never_cache
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def callback(request):
    if not is_configured():
        return Response({'error': '飞书登录尚未配置，请联系管理员。'}, status=503)
    serializer = CallbackSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        attempt = consume_attempt(request, data['state'])
    except FeishuLoginError as exc:
        return Response({'error': str(exc)}, status=exc.status)
    try:
        if data.get('error'):
            raise FeishuLoginError('你已取消飞书授权，可以重新授权或使用密码登录。')
        if not data.get('code'):
            raise FeishuLoginError('缺少飞书授权码，请重新授权。')
        profile = fetch_user_info(exchange_code(data['code'], attempt))
        user = resolve_user(profile)
        refresh = MyTokenObtainPairSerializer.get_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        response = Response({'user': session_user_data(user),
                             'access': str(refresh.access_token), 'refresh': str(refresh)})
    except FeishuLoginError as exc:
        response = Response({'error': str(exc)}, status=exc.status)
    response.delete_cookie(COOKIE_NAME, samesite='Lax')
    return response

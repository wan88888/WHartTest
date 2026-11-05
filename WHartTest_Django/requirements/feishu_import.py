import re
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import ValidationError

from accounts.feishu import FeishuLoginError, consume_attempt, exchange_code, fetch_user_info, is_configured
from accounts.models import FeishuIdentity


def parse_link(value):
    try:
        uri = urlsplit(value.strip())
        host = uri.hostname or ''
        valid_host = any(host == domain or host.endswith('.' + domain)
                         for domain in ('feishu.cn', 'larkoffice.com', 'larksuite.com'))
        match = re.fullmatch(r'/(docx|wiki)/([A-Za-z0-9]{10,128})/?', uri.path)
        if (uri.scheme == 'https' and valid_host and uri.port in (None, 443)
                and not uri.username and not uri.password and match):
            return match.groups()
    except (ValueError, AttributeError):
        pass
    raise ValidationError({'detail': '请粘贴飞书新版文档（/docx/）或知识库文档（/wiki/）的 HTTPS 链接。'})


def document_binding(request):
    kind, token = parse_link(request.data.get('feishu_url'))
    return f"import:{request.user.pk}:{request.data.get('project')}:{kind}:{token}:"


@sensitive_variables()
def get_data(access_token, path, params=None):
    try:
        response = requests.get('https://open.feishu.cn/open-apis/' + path, params=params,
                                headers={'Authorization': 'Bearer ' + access_token},
                                timeout=(3, 20), allow_redirects=False)
        body = response.json()
    except (requests.RequestException, ValueError):
        raise ValidationError({'detail': '暂时无法读取飞书文档，请稍后重试。'}) from None
    if (response.status_code != 200 or not isinstance(body, dict) or body.get('code') != 0
            or not isinstance(body.get('data'), dict)):
        raise ValidationError({'detail': '读取失败，请确认当前飞书账号可查看该文档，且飞书应用已开通对应的只读权限。'})
    return body['data']


@sensitive_variables()
def read_document(request, url, code, state):
    if not is_configured():
        raise ValidationError({'detail': '飞书授权尚未配置，请联系管理员。'})
    kind, token = parse_link(url)
    try:
        attempt = consume_attempt(request, state, binding=document_binding(request))
        access_token = exchange_code(code, attempt)
        profile = fetch_user_info(access_token)
    except FeishuLoginError as exc:
        raise ValidationError({'detail': str(exc)}) from None
    identity = FeishuIdentity.objects.filter(user=request.user, app_id=settings.FEISHU_APP_ID).first()
    if (profile.get('tenant_key') not in settings.FEISHU_ALLOWED_TENANT_KEYS or not profile.get('open_id')
            or (identity and (identity.open_id != profile['open_id'] or identity.tenant_key != profile['tenant_key']))):
        raise ValidationError({'detail': '请使用当前平台账号对应的企业飞书身份授权。'})
    if kind == 'wiki':
        node = get_data(access_token, 'wiki/v2/spaces/get_node', {'token': token}).get('node')
        if not isinstance(node, dict) or node.get('obj_type') != 'docx':
            raise ValidationError({'detail': '请选择知识库中的新版文档页面。'})
        token = node.get('obj_token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token):
            raise ValidationError({'detail': '飞书返回的文档标识无效。'})
    content = get_data(access_token, f'docx/v1/documents/{token}/raw_content').get('content')
    if not isinstance(content, str) or not content.strip():
        raise ValidationError({'detail': '文档没有可导入的正文。'})
    return content

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Run after the backend is healthy. Credentials come from the private .env file.
docker compose exec -T backend python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model
from api_keys.models import APIKey

user = get_user_model().objects.get(username=os.environ['DJANGO_ADMIN_USERNAME'])
APIKey.objects.update_or_create(
    name='Default MCP Key (Auto-generated)',
    defaults={'user': user, 'key': os.environ['WHARTTEST_API_KEY'], 'is_active': True},
)
print('Local integration API key synchronized.')
PY
docker compose exec -T backend python manage.py init_skills
docker compose exec -T backend python manage.py init_default_prompts

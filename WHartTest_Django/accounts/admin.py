from django.contrib import admin

from .models import FeishuIdentity


@admin.register(FeishuIdentity)
class FeishuIdentityAdmin(admin.ModelAdmin):
    list_display = ['user', 'display_name', 'app_id', 'open_id', 'tenant_key']
    search_fields = ['user__username', 'display_name', 'open_id', 'tenant_key']
    autocomplete_fields = ['user']
    readonly_fields = ['created_at']

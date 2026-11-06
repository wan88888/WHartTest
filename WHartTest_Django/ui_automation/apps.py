from django.apps import AppConfig


class UiAutomationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ui_automation'

    def ready(self):
        from . import artifacts  # Register cleanup for cascaded deletions.

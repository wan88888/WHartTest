from django.db import migrations
from django.db.models import Count
from django.utils import timezone


def reconcile_cancelled(apps, schema_editor):
    executions = apps.get_model('testcases', 'TestExecution').objects.using(schema_editor.connection.alias)
    results = apps.get_model('testcases', 'TestCaseResult').objects.using(schema_editor.connection.alias)
    for execution in executions.filter(status='cancelled').iterator():
        rows = results.filter(execution_id=execution.pk)
        ended = execution.completed_at or timezone.now()
        rows.filter(status__in=['pending', 'running']).update(status='skip', completed_at=ended, updated_at=ended)
        counts = dict(rows.order_by().values_list('status').annotate(n=Count('pk')))
        # Historical suite membership cannot be reconstructed if no results were recorded.
        if counts:
            executions.filter(pk=execution.pk).update(
                total_count=sum(counts.values()), passed_count=counts.get('pass', 0),
                failed_count=counts.get('fail', 0), error_count=counts.get('error', 0),
                skipped_count=counts.get('skip', 0),
            )


class Migration(migrations.Migration):
    dependencies = [('testcases', '0024_testexecution_scheduled_task')]
    operations = [migrations.RunPython(reconcile_cancelled, migrations.RunPython.noop)]

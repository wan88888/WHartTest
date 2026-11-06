import re

from django.db import migrations, models
import django.db.models.deletion


def register_history(apps, schema_editor):
    Batch = apps.get_model('ui_automation', 'UiBatchExecutionRecord')
    Record = apps.get_model('ui_automation', 'UiExecutionRecord')
    Artifact = apps.get_model('ui_automation', 'UiExecutionArtifact')
    for batch in Batch.objects.iterator():
        projects = list(Record.objects.filter(batch=batch).order_by()
                        .values_list('test_case__project_id', flat=True).distinct()[:2])
        if len(projects) == 1:
            Batch.objects.filter(pk=batch.pk).update(project_id=projects[0])
    # Old uploads had a server-generated date and filename. Do not register
    # ambiguous or client-supplied paths, or delete files based on legacy metadata.
    ambiguous = set()
    for record in Record.objects.only('pk', 'test_case_id', 'screenshots', 'trace_path', 'step_results').iterator():
        screenshots = list(record.screenshots or [])
        screenshots.extend(s.get('screenshot') for s in (record.step_results or []) if isinstance(s, dict))
        for kind, path in [('trace', record.trace_path), *(('screenshot', s) for s in screenshots)]:
            if not isinstance(path, str) or len(path) > 255:
                continue
            path = path.removeprefix('/media/')
            pattern = (r'ui_traces/\d{8}/[a-f0-9]{12}\.zip' if kind == 'trace'
                       else r'ui_screenshots/\d{8}/[a-f0-9]{12}\.(?:png|jpg|jpeg|webp)')
            if path in ambiguous or not re.fullmatch(pattern, path):
                continue
            artifact, created = Artifact.objects.get_or_create(path=path, defaults={
                'kind': kind, 'record_id': record.pk, 'test_case_id': record.test_case_id,
            })
            if not created and artifact.record_id != record.pk:
                artifact.delete()
                ambiguous.add(path)


class Migration(migrations.Migration):
    dependencies = [('ui_automation', '0025_uitestcase_env_config')]
    operations = [
        migrations.AddField(model_name='uibatchexecutionrecord', name='project',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='ui_execution_batches', to='projects.project')),
        migrations.AlterModelOptions(name='uiexecutionrecord', options={
            'ordering': ['-created_at'], 'verbose_name': 'UI 执行记录', 'verbose_name_plural': 'UI 执行记录',
            'permissions': [('manage_actuators', '管理共享 UI 执行器')]}),
        migrations.CreateModel(name='UiExecutionArtifact', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('path', models.CharField(max_length=255, unique=True)),
            ('kind', models.CharField(max_length=12, choices=[('screenshot', '截图'), ('trace', 'Trace')])),
            ('record', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                         related_name='artifacts', to='ui_automation.uiexecutionrecord')),
            ('test_case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='ui_automation.uitestcase')),
        ]),
        migrations.RunPython(register_history, migrations.RunPython.noop),
    ]

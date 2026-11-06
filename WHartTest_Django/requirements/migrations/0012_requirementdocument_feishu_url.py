from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('requirements', '0011_requirementdocument_error_message_and_more')]
    operations = [
        migrations.AddField(
            model_name='requirementdocument',
            name='feishu_url',
            field=models.URLField(blank=True, default='', max_length=2048, verbose_name='飞书原文链接'),
        ),
    ]

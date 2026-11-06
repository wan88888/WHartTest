"""Validate, register and deliver private UI execution artifacts."""
import io
import re
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.http import FileResponse, Http404
from django.urls import reverse
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from .models import UiExecutionArtifact

PRIVATE_DIR = 'ui_execution_artifacts'
LEGACY_DIRS = {'screenshot': 'ui_screenshots', 'trace': 'ui_traces'}


def artifact_path(relative, kind):
    root = Path(settings.MEDIA_ROOT).resolve()
    relative = Path(relative)
    if relative.is_absolute() or relative.parts[:1] not in [(PRIVATE_DIR,), (LEGACY_DIRS[kind],)]:
        raise Http404
    path = root / relative
    if '..' in relative.parts or path.resolve() != path:
        raise Http404
    return path


def _png(stream):
    try:
        with Image.open(stream, formats=['PNG', 'JPEG', 'WEBP']) as img:
            if img.width * img.height > 16_000_000:
                raise ValidationError('截图像素数超出限制')
            output = io.BytesIO()
            # Re-encode pixels only. Discard metadata, appended bytes and original filename.
            clean = img.convert('RGBA')
            clean.info.clear()
            clean.save(output, 'PNG')
            output.seek(0)
            return output
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ValidationError('请上传有效的 PNG、JPEG 或 WebP 截图') from None


def validate_trace(stream):
    try:
        with zipfile.ZipFile(stream) as archive:
            files = archive.infolist()
            if (len(files) > 10000 or sum(f.file_size for f in files) > 256 * 1024**2
                    or not any(f.filename.endswith('.trace') for f in files)
                    or any(f.flag_bits & 1 or Path(f.filename).is_absolute() or '..' in Path(f.filename).parts for f in files)):
                raise ValidationError('Trace 内容超出限制或格式无效')
            if archive.testzip():
                raise ValidationError('Trace 文件损坏')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError):
        raise ValidationError('请上传有效的 Playwright Trace ZIP') from None


def store_artifact(stream, kind, case_id):
    stream.seek(0, 2)
    if stream.tell() > (16 if kind == 'screenshot' else 64) * 1024**2:
        raise ValidationError('执行产物超过大小限制')
    stream.seek(0)
    if kind == 'screenshot':
        stream = _png(stream)
        extension = 'png'
    else:
        validate_trace(stream)
        stream.seek(0)
        extension = 'zip'
    relative = f'{PRIVATE_DIR}/{uuid.uuid4().hex}.{extension}'
    target = artifact_path(relative, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open('xb') as out:
            while chunk := stream.read(64 * 1024):
                out.write(chunk)
        return UiExecutionArtifact.objects.create(test_case_id=case_id, path=relative, kind=kind)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def artifact_url(artifact):
    return reverse('ui-artifact', args=[artifact.pk])


def _artifact_key(value):
    if not isinstance(value, str) or len(value) > 255:
        return None
    return value.removeprefix('/media/')


def _picture(value, pictures):
    if isinstance(value, str) and value.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')):
        return value
    return pictures.get(_artifact_key(value))


def report_representation(record, data):
    artifacts = list(record.artifacts.all())
    pictures = {key: artifact_url(a) for a in artifacts if a.kind == 'screenshot' for key in (a.path, artifact_url(a))}
    trace = next((a for a in artifacts if a.kind == 'trace'), None)
    data['trace_path'] = artifact_url(trace) if trace else None
    data['screenshots'] = [url for s in data.get('screenshots') or [] if (url := _picture(s, pictures))]
    data['step_results'] = [
        {**step, 'screenshot': _picture(step.get('screenshot'), pictures)} if isinstance(step, dict) else step
        for step in data.get('step_results') or []
    ]
    return data


def claim_artifacts(record, trace, screenshots):
    candidates = UiExecutionArtifact.objects.filter(test_case_id=record.test_case_id, record__isnull=True)
    if trace:
        candidates.filter(path=_artifact_key(trace), kind='trace').update(record=record)
    paths, ids = [], []
    for value in screenshots:
        key = _artifact_key(value)
        if key:
            match = re.fullmatch(r'/api/ui-automation/artifacts/(\d+)/', key)
            if match:
                ids.append(int(match[1]))
            else:
                paths.append(key)
    candidates.filter(kind='screenshot', path__in=paths).update(record=record)
    candidates.filter(kind='screenshot', pk__in=ids).update(record=record)


def serve_artifact(artifact):
    path = artifact_path(artifact.path, artifact.kind)
    if not path.is_file():
        raise Http404
    if artifact.kind == 'screenshot':
        if artifact.path.startswith(PRIVATE_DIR + '/'):
            stream = path.open('rb')
        else:
            with path.open('rb') as old:
                stream = _png(old)
        response = FileResponse(stream, content_type='image/png', filename='screenshot.png')
    else:
        response = FileResponse(path.open('rb'), content_type='application/zip', as_attachment=True, filename='trace.zip')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@receiver(post_delete, sender=UiExecutionArtifact)
def remove_owned_file(sender, instance, **kwargs):
    if instance.path.startswith(PRIVATE_DIR + '/'):
        try:
            path = artifact_path(instance.path, instance.kind)
        except Http404:
            return
        transaction.on_commit(lambda: path.unlink(missing_ok=True))

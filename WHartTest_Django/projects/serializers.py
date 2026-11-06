# 导入 DRF 序列化器基类。
from rest_framework import serializers

# 导入用户模型用于成员创建校验。
from django.contrib.auth.models import User
from django.db import transaction

# 导入项目相关模型。
from .models import Project, ProjectMember, ProjectCredential

# 导入用户详情序列化器用于嵌套回显。
from accounts.serializers import UserDetailSerializer


class ProjectCredentialSerializer(serializers.ModelSerializer):
    """项目凭据序列化器"""
    id = serializers.IntegerField(required=False, min_value=1)
    password = serializers.CharField(write_only=True, allow_blank=True, required=False,
                                     trim_whitespace=False, max_length=255, help_text='登录密码')

    class Meta:
        model = ProjectCredential
        fields = ['id', 'system_url', 'username', 'password', 'user_role', 'created_at']
        read_only_fields = ['created_at']
        extra_kwargs = {
            'system_url': {'help_text': '系统访问地址（如 https://test.example.com）'},
            'username': {'help_text': '登录账号'},
            'user_role': {'help_text': '如"管理员"、"普通用户"、"审核员"等'}
        }


class ProjectMemberSerializer(serializers.ModelSerializer):
    """项目成员序列化器"""
    user_detail = UserDetailSerializer(source='user', read_only=True)

    class Meta:
        model = ProjectMember
        fields = ['id', 'user', 'user_detail', 'role', 'joined_at']
        read_only_fields = ['joined_at']


class ProjectSerializer(serializers.ModelSerializer):
    """项目基本信息序列化器"""
    creator_detail = UserDetailSerializer(source='creator', read_only=True)
    credentials = ProjectCredentialSerializer(many=True, required=False)
    current_user_role = serializers.CharField(read_only=True, allow_null=True)
    removed_credential_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), write_only=True, required=False,
        help_text='显式指定删除的凭据。提供此字段时，credentials 只更新列出的条目。',
    )

    class Meta:
        model = Project
        fields = ['id', 'name', 'description', 'creator', 'creator_detail', 'created_at', 'updated_at', 'credentials', 'removed_credential_ids', 'current_user_role']
        read_only_fields = ['created_at', 'updated_at', 'creator']

    def validate_name(self, value):
        """验证项目名称唯一性"""
        # 条件：创建时名称已存在，或更新时改成了已存在名称；动作：拒绝；结果：保证项目名唯一约束前置可读报错。
        if self.instance is None and Project.objects.filter(name=value).exists():
            raise serializers.ValidationError("项目名称已存在")
        elif self.instance and self.instance.name != value and Project.objects.filter(name=value).exists():
            raise serializers.ValidationError("项目名称已存在")
        return value

    def validate(self, attrs):
        if self.instance is None and ('removed_credential_ids' in attrs or
                                      any('id' in row for row in attrs.get('credentials', []))):
            raise serializers.ValidationError({'credentials': '新项目不能引用已有凭据。'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        # 拆出嵌套凭据数据，先创建项目主记录，再逐条创建凭据。
        credentials_data = validated_data.pop('credentials', [])
        project = Project.objects.create(**validated_data)
        for credential_data in credentials_data:
            ProjectCredential.objects.create(project=project, **credential_data)
        return project

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = Project.objects.select_for_update().get(pk=instance.pk)
        credentials_data = validated_data.pop('credentials', None)
        removed_ids = validated_data.pop('removed_credential_ids', None)
        instance = super().update(instance, validated_data)
        if credentials_data is None and removed_ids is None:
            return instance

        rows = credentials_data or []
        existing_ids = set(instance.credentials.values_list('pk', flat=True))
        submitted_ids = [row['id'] for row in rows if 'id' in row]
        retained = set(submitted_ids)
        # Older clients submit the complete list, while the editor sends only changed rows.
        removed = set(removed_ids) if removed_ids is not None else existing_ids - retained
        if len(submitted_ids) != len(retained) or retained & removed:
            raise serializers.ValidationError({'credentials': '同一凭据不能重复提交或同时更新和删除。'})
        if (retained | removed) - existing_ids:
            raise serializers.ValidationError({'credentials': '凭据不存在或不属于此项目，请刷新后重试。'})

        for row in rows:
            credential_id = row.pop('id', None)
            if credential_id is None:
                instance.credentials.create(**row)
                continue
            if row.get('password') == '':
                del row['password']
            if row:
                instance.credentials.filter(pk=credential_id).update(**row)
        instance.credentials.filter(pk__in=removed).delete()

        return instance


class ProjectDetailSerializer(ProjectSerializer):
    """项目详细信息序列化器，包含成员信息"""
    members = ProjectMemberSerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ['members']


class ProjectMemberCreateSerializer(serializers.ModelSerializer):
    """创建项目成员的序列化器"""
    user_id = serializers.IntegerField(write_only=True, help_text="用户ID")

    class Meta:
        model = ProjectMember
        fields = ['user_id', 'role']

    def validate_user_id(self, value):
        """验证用户ID是否存在"""
        try:
            User.objects.get(id=value)
        except User.DoesNotExist:
            raise serializers.ValidationError("用户不存在")
        return value

    def validate(self, attrs):
        """验证用户是否已经是项目成员"""
        project = self.context['project']
        user_id = attrs['user_id']

        # 条件：用户已是成员；动作：拒绝重复添加；结果：避免违反 unique_together 并返回可读错误。
        if ProjectMember.objects.filter(project=project, user_id=user_id).exists():
            raise serializers.ValidationError({"user_id": "该用户已经是项目成员"})

        return attrs

    def create(self, validated_data):
        # 创建成员时通过上下文绑定项目，避免请求体伪造 project 归属。
        project = self.context['project']
        user_id = validated_data.pop('user_id')
        user = User.objects.get(id=user_id)
        return ProjectMember.objects.create(project=project, user=user, **validated_data)

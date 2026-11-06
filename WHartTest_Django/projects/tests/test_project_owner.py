from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from django.contrib.auth.models import Permission, User
from projects.models import Project, ProjectMember


class ProjectOwnerTests(TestCase):
    """测试项目拥有者相关功能"""

    def setUp(self):
        """设置测试环境"""
        # 创建测试用户
        self.owner = User.objects.create_user(
            username='owner_user',
            email='owner@example.com',
            password='password123'
        )
        self.admin = User.objects.create_user(
            username='admin_user',
            email='admin@example.com',
            password='password123'
        )
        self.member = User.objects.create_user(
            username='member_user',
            email='member@example.com',
            password='password123'
        )
        self.new_owner = User.objects.create_user(
            username='new_owner_user',
            email='new_owner@example.com',
            password='password123'
        )

        # 创建测试项目
        self.project = Project.objects.create(
            name='Test Project',
            description='Test Project Description',
            creator=self.owner
        )

        # 添加项目成员
        ProjectMember.objects.create(
            project=self.project,
            user=self.owner,
            role='owner'
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.admin,
            role='admin'
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.member,
            role='member'
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.new_owner,
            role='member'
        )

        change_role = Permission.objects.get(content_type__app_label='projects', codename='change_projectmember')
        self.owner.user_permissions.add(change_role)
        self.admin.user_permissions.add(change_role)
        self.member.user_permissions.add(change_role)

        # 设置API客户端
        self.client = APIClient()

    def test_change_project_owner(self):
        """测试更改项目拥有者，原拥有者应自动降级为管理员"""
        # 登录原拥有者
        self.client.force_authenticate(user=self.owner)

        # 构建请求URL和数据
        url = reverse('project-update-member-role', args=[self.project.id])
        data = {
            'user_id': self.new_owner.id,
            'role': 'owner'
        }

        # 发送请求
        response = self.client.patch(url, data, format='json')

        # 验证响应
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 验证新拥有者角色
        new_owner_membership = ProjectMember.objects.get(project=self.project, user=self.new_owner)
        self.assertEqual(new_owner_membership.role, 'owner')

        # 验证原拥有者角色已降级为管理员
        original_owner_membership = ProjectMember.objects.get(project=self.project, user=self.owner)
        self.assertEqual(original_owner_membership.role, 'admin')

    def test_only_one_owner_allowed(self):
        """测试项目只能有一个拥有者"""
        # 登录原拥有者
        self.client.force_authenticate(user=self.owner)

        # 先将管理员设为拥有者
        url = reverse('project-update-member-role', args=[self.project.id])
        data = {
            'user_id': self.admin.id,
            'role': 'owner'
        }
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 新拥有者执行下一次转移
        self.client.force_authenticate(user=self.admin)
        # 再将普通成员设为拥有者
        data = {
            'user_id': self.member.id,
            'role': 'owner'
        }
        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 验证只有一个拥有者
        owner_count = ProjectMember.objects.filter(project=self.project, role='owner').count()
        self.assertEqual(owner_count, 1)

        # 验证最新设置的成员是拥有者
        member_role = ProjectMember.objects.get(project=self.project, user=self.member)
        self.assertEqual(member_role.role, 'owner')

        # 验证其他人都是管理员
        admin_role = ProjectMember.objects.get(project=self.project, user=self.admin)
        owner_role = ProjectMember.objects.get(project=self.project, user=self.owner)
        self.assertEqual(admin_role.role, 'admin')
        self.assertEqual(owner_role.role, 'admin')

    def test_transfer_requires_role_and_model_permission(self):
        url = reverse('project-update-member-role', args=[self.project.id])
        for actor in (self.admin, self.member):
            with self.subTest(actor=actor.username):
                self.client.force_authenticate(user=actor)
                response = self.client.patch(url, {'user_id': self.new_owner.pk, 'role': 'owner'})
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.owner.user_permissions.clear()
        self.client.force_authenticate(user=self.owner)
        self.assertEqual(self.client.patch(url, {'user_id': self.member.pk, 'role': 'owner'}).status_code, 403)
        self.assertEqual(self.project.members.get(role='owner').user_id, self.owner.pk)

    def test_add_owner_transfers_existing_ownership(self):
        add_member = Permission.objects.get(content_type__app_label='projects', codename='add_projectmember')
        self.admin.user_permissions.add(add_member)
        self.owner.user_permissions.add(add_member)
        candidate = User.objects.create_user(username='candidate')
        url = reverse('project-add-member', args=[self.project.pk])
        payload = {'user_id': candidate.pk, 'role': 'owner'}
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.post(url, payload).status_code, 403)
        change_role = Permission.objects.get(content_type__app_label='projects', codename='change_projectmember')
        self.owner.user_permissions.remove(change_role)
        self.client.force_authenticate(user=User.objects.get(pk=self.owner.pk))
        self.assertEqual(self.client.post(url, payload).status_code, 403)
        self.owner.user_permissions.add(change_role)
        self.client.force_authenticate(user=User.objects.get(pk=self.owner.pk))
        self.assertEqual(self.client.post(url, payload).status_code, 201)
        self.assertEqual(self.project.members.get(role='owner').user_id, candidate.pk)
        self.assertEqual(self.project.members.get(user=self.owner).role, 'admin')

    def test_superuser_cannot_leave_project_without_owner(self):
        root = User.objects.create_superuser(username='root', password='test')
        self.client.force_authenticate(user=root)
        url = reverse('project-update-member-role', args=[self.project.pk])
        self.assertEqual(self.client.patch(url, {'user_id': self.owner.pk, 'role': 'admin'}).status_code, 403)
        url = reverse('project-remove-member', args=[self.project.pk])
        self.assertEqual(self.client.delete(url, {'user_id': self.owner.pk}).status_code, 403)
        self.assertEqual(self.project.members.get(role='owner').user_id, self.owner.pk)

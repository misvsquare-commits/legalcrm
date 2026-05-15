"""
Accounts app models — Custom User with role-based access control
Roles: admin, lawyer, assistant, client
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_CHOICES = [
        ('admin',     'Admin'),
        ('lawyer',    'Lawyer'),
        ('assistant', 'Assistant'),
        ('client',    'Client'),
    ]

    role       = models.CharField(max_length=20, choices=ROLE_CHOICES,
                                  default='assistant')
    phone      = models.CharField(max_length=20, blank=True, null=True)
    bar_number = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_admin_role(self):  return self.role == 'admin'
    @property
    def is_lawyer(self):      return self.role == 'lawyer'
    @property
    def is_assistant(self):   return self.role == 'assistant'
    @property
    def is_client(self):      return self.role == 'client'

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

"""Global login identities. Company permissions will come from memberships."""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import validate_email
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        return super().normalize_email(email).lower()

    def get_by_natural_key(self, email):
        return super().get_by_natural_key(self.normalize_email(email))

    async def aget_by_natural_key(self, email):
        return await super().aget_by_natural_key(self.normalize_email(email))

    def create_user(self, email, password=None, **extra_fields):
        if not email or not email.strip():
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        validate_email(email)
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        if extra_fields["is_staff"] != extra_fields["is_superuser"]:
            raise ValueError("Platform staff and superuser flags must match.")
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        for field in ("is_staff", "is_superuser", "is_active"):
            if extra_fields[field] is not True:
                raise ValueError(f"A superuser must have {field}=True.")
        if not password:
            raise ValueError("A superuser password is required.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")),
                name="accounts_user_email_lowercase",
            ),
            models.CheckConstraint(
                condition=models.Q(is_staff=models.F("is_superuser")),
                name="accounts_user_platform_flags_match",
            ),
        ]

    def clean(self):
        super().clean()
        self.email = type(self).objects.normalize_email(self.email)

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email)
        return super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name or self.email

"""Suppliers of one company and the people there who receive magic links."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.tenants.models import TenantModel


class Supplier(TenantModel):
    name = models.CharField(max_length=200)
    region = models.ForeignKey("tenants.Region", on_delete=models.PROTECT, null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "company", Lower("name"), name="suppliers_supplier_name_unique"
            ),
            models.UniqueConstraint(fields=["company", "id"], name="suppliers_supplier_tenant_id"),
        ]

    def __str__(self):
        return self.name


class SupplierContact(TenantModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="contacts")
    name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(max_length=254)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "email"], name="suppliers_contact_company_email_unique"
            ),
            models.UniqueConstraint(fields=["company", "id"], name="suppliers_contact_tenant_id"),
            models.UniqueConstraint(
                fields=["company", "supplier", "id"], name="suppliers_contact_supplier_id"
            ),
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="suppliers_contact_email_lowercase"
            ),
        ]

    def save(self, *args, **kwargs):
        self.email = (self.email or "").lower()
        if not self._state.adding:
            stored = (
                type(self)
                .objects.for_company(self.company_id)
                .filter(pk=self.pk, supplier_id=self.supplier_id)
            )
            if not stored.exists():
                raise ValidationError("A contact's company and supplier cannot change.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.email

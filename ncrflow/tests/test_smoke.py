"""Proves the skeleton loads. Needs no database."""

from django.apps import apps
from django.test import Client


def test_all_project_apps_are_installed():
    expected = {
        "tenants",
        "accounts",
        "suppliers",
        "ncr",
        "sncr",
        "notifications",
        "escalation",
        "reports",
    }
    installed = {config.label for config in apps.get_app_configs()}
    assert expected <= installed


def test_home_page_loads():
    response = Client().get("/")
    assert response.status_code == 200
    assert b"ncrflow" in response.content

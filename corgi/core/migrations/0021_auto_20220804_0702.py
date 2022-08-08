# Generated by Django 3.2.14 on 2022-08-05 03:25

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_rename_upstream_component_upstreams"),
    ]

    operations = [
        migrations.AddField(
            model_name="productstream",
            name="brew_tags",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="productstream",
            name="yum_repositories",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=200), default=list, size=None
            ),
        ),
        migrations.AddField(
            model_name="productstream",
            name="composes",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="productstream",
            name="active",
            field=models.BooleanField(default=False),
        ),
    ]

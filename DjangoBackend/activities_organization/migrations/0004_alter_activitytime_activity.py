# Generated by Django 4.2.3 on 2024-06-01 03:40

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("activities_organization", "0003_alter_timeoption_activity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="activitytime",
            name="activity",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="activity_time",
                to="activities_organization.createactivity",
                to_field="activity_id",
            ),
        ),
    ]

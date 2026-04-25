from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Merchant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'merchants',
            },
        ),
        migrations.CreateModel(
            name='BankAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('account_number', models.CharField(max_length=20)),
                ('ifsc_code', models.CharField(max_length=11)),
                ('account_holder_name', models.CharField(max_length=255)),
                ('is_primary', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bank_accounts', to='payouts.merchant')),
            ],
            options={
                'db_table': 'bank_accounts',
            },
        ),
        migrations.CreateModel(
            name='IdempotencyKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('key', models.CharField(db_index=True, max_length=255)),
                ('response_body', models.JSONField(blank=True, null=True)),
                ('response_status', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='idempotency_keys', to='payouts.merchant')),
            ],
            options={
                'db_table': 'idempotency_keys',
            },
        ),
        migrations.CreateModel(
            name='Payout',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount_paise', models.BigIntegerField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed')], db_index=True, default='pending', max_length=20)),
                ('retry_attempt', models.PositiveSmallIntegerField(default=0)),
                ('max_retries', models.PositiveSmallIntegerField(default=3)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('processing_started_at', models.DateTimeField(blank=True, null=True)),
                ('failure_reason', models.TextField(blank=True, default='')),
                ('bank_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payouts', to='payouts.bankaccount')),
                ('idempotency_record', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='payout', to='payouts.idempotencykey')),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payouts', to='payouts.merchant')),
            ],
            options={
                'db_table': 'payouts',
            },
        ),
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('entry_type', models.CharField(choices=[('credit', 'Credit'), ('debit', 'Debit')], db_index=True, max_length=10)),
                ('amount_paise', models.BigIntegerField()),
                ('description', models.CharField(max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ledger_entries', to='payouts.merchant')),
                ('payout', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ledger_entries', to='payouts.payout')),
            ],
            options={
                'db_table': 'ledger_entries',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['merchant', 'status'], name='payouts_merchant_status_idx'),
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['status', 'processing_started_at'], name='payouts_status_processing_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='idempotencykey',
            unique_together={('merchant', 'key')},
        ),
        migrations.AddIndex(
            model_name='idempotencykey',
            index=models.Index(fields=['merchant', 'key'], name='idempotency_merchant_key_idx'),
        ),
    ]

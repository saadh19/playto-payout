"""
Management command to seed test merchants with credit history.
Run: python manage.py seed_merchants
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from payouts.models import Merchant, BankAccount, LedgerEntry


MERCHANTS = [
    {
        'name': 'Aryan Creative Studio',
        'email': 'aryan@creativestudio.in',
        'bank': {
            'account_number': '00123456789',
            'ifsc_code': 'HDFC0001234',
            'account_holder_name': 'Aryan Sharma',
        },
        'credits': [
            (250000, 'Payment from client Acme Corp - Invoice #101'),
            (150000, 'Payment from client TechStart - Invoice #102'),
            (75000, 'Payment from client DesignHub - Invoice #103'),
        ],
    },
    {
        'name': 'Priya Freelance Dev',
        'email': 'priya@freelancedev.in',
        'bank': {
            'account_number': '00987654321',
            'ifsc_code': 'ICIC0005678',
            'account_holder_name': 'Priya Nair',
        },
        'credits': [
            (500000, 'Payment from client GlobalSaaS - Invoice #201'),
            (300000, 'Payment from client FinTech Ltd - Invoice #202'),
            (125000, 'Bonus payment from client GlobalSaaS - Invoice #203'),
        ],
    },
    {
        'name': 'Ravi Digital Agency',
        'email': 'ravi@digitalagency.in',
        'bank': {
            'account_number': '00456789012',
            'ifsc_code': 'SBIN0009012',
            'account_holder_name': 'Ravi Kumar',
        },
        'credits': [
            (1000000, 'Retainer payment from client MegaCorp - Q1'),
            (750000, 'Project payment from client StartupXYZ - Invoice #301'),
            (200000, 'Consultation fee from client VentureHub - Invoice #302'),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed test merchants with balance history'

    def handle(self, *args, **kwargs):
        with transaction.atomic():
            for data in MERCHANTS:
                merchant, created = Merchant.objects.get_or_create(
                    email=data['email'],
                    defaults={'name': data['name']},
                )
                if created:
                    self.stdout.write(f"Created merchant: {merchant.name}")
                else:
                    self.stdout.write(f"Merchant already exists: {merchant.name}")

                bank, _ = BankAccount.objects.get_or_create(
                    merchant=merchant,
                    account_number=data['bank']['account_number'],
                    defaults={
                        'ifsc_code': data['bank']['ifsc_code'],
                        'account_holder_name': data['bank']['account_holder_name'],
                        'is_primary': True,
                    },
                )

                # Only add credits if merchant has no ledger entries yet
                if not LedgerEntry.objects.filter(merchant=merchant).exists():
                    for amount, description in data['credits']:
                        LedgerEntry.objects.create(
                            merchant=merchant,
                            entry_type=LedgerEntry.CREDIT,
                            amount_paise=amount,
                            description=description,
                        )
                    self.stdout.write(f"  → Added {len(data['credits'])} credit entries")

                balance = merchant.get_balance_summary()
                self.stdout.write(
                    f"  → Balance: ₹{balance['available_paise'] / 100:.2f} "
                    f"(held: ₹{balance['held_paise'] / 100:.2f})"
                )

        self.stdout.write(self.style.SUCCESS('\nSeed complete! ✓'))

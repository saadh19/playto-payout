from rest_framework import serializers
from .models import Merchant, BankAccount, Payout, LedgerEntry


class BankAccountSerializer(serializers.ModelSerializer):
    account_number_masked = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = ['id', 'account_holder_name', 'ifsc_code', 'account_number_masked', 'is_primary', 'created_at']

    def get_account_number_masked(self, obj):
        n = obj.account_number
        return '*' * (len(n) - 4) + n[-4:]


class LedgerEntrySerializer(serializers.ModelSerializer):
    payout_id = serializers.UUIDField(source='payout.id', read_only=True, allow_null=True)

    class Meta:
        model = LedgerEntry
        fields = ['id', 'entry_type', 'amount_paise', 'description', 'payout_id', 'created_at']


class PayoutSerializer(serializers.ModelSerializer):
    bank_account = BankAccountSerializer(read_only=True)

    class Meta:
        model = Payout
        fields = [
            'id', 'amount_paise', 'status', 'bank_account',
            'retry_attempt', 'failure_reason', 'created_at', 'updated_at',
        ]


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ['id', 'name', 'email', 'created_at']


class MerchantDashboardSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()
    bank_accounts = BankAccountSerializer(many=True, read_only=True)

    class Meta:
        model = Merchant
        fields = ['id', 'name', 'email', 'balance', 'bank_accounts', 'created_at']

    def get_balance(self, obj):
        return obj.get_balance_summary()


class CreatePayoutSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=100)  # min 1 rupee
    bank_account_id = serializers.UUIDField()

    def validate_amount_paise(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be positive.")
        return value

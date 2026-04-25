from django.urls import path
from . import views

urlpatterns = [
    path('merchants/', views.list_merchants),
    path('merchants/<uuid:merchant_id>/', views.merchant_dashboard),
    path('merchants/<uuid:merchant_id>/ledger/', views.merchant_ledger),
    path('merchants/<uuid:merchant_id>/payouts/', views.merchant_payouts),
    path('merchants/<uuid:merchant_id>/payouts/create/', views.create_payout),
    path('merchants/<uuid:merchant_id>/payouts/<uuid:payout_id>/', views.payout_detail),
]

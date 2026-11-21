from django.urls import path
from routing.views import RouteFuelPlanView

urlpatterns = [
    path("route-fuel-plan/", RouteFuelPlanView.as_view(), name="route-fuel-plan"),
]
from rest_framework import serializers


class RouteFuelPlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=255,
        help_text="Start location within the USA (e.g., 'Los Angeles, CA' or '90001')",
    )
    finish = serializers.CharField(
        max_length=255,
        help_text="Finish location within the USA (e.g., 'New York, NY' or '10001')",
    )
from django.db import models


class FuelStation(models.Model):
    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, db_index=True)
    state = models.CharField(max_length=20, db_index=True)
    rack_id = models.IntegerField(null=True, blank=True)
    retail_price = models.FloatField()
    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["city", "state"]),
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.retail_price:.3f}"
from django.contrib import admin

# Register your models here.
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem, ModificationLog


@admin.register(TripRequest)
class TripRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "start_datetime", "end_datetime", "created_at")
    list_filter = ("purpose_main", "region_preference")
    ordering = ("-created_at",)


@admin.register(RecommendedCourse)
class RecommendedCourseAdmin(admin.ModelAdmin):
    list_display = ("id", "trip", "mode", "is_selected", "is_saved", "final_score", "created_at")
    list_filter = ("mode", "is_selected", "is_saved")


admin.site.register(ItineraryDay)
admin.site.register(ItineraryItem)
admin.site.register(ModificationLog)
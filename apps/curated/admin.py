from django.contrib import admin
from apps.curated.models import CuratedCourse, CuratedCourseItem


class CuratedCourseItemInline(admin.TabularInline):
    model = CuratedCourseItem
    extra = 1


@admin.register(CuratedCourse)
class CuratedCourseAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "region", "duration", "order"]
    list_filter = ["region", "duration"]
    inlines = [CuratedCourseItemInline]

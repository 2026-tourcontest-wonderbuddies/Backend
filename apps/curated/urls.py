from django.urls import path
from apps.curated import views

urlpatterns = [
    path("", views.CuratedCourseListView.as_view()),
    path("<int:course_id>/", views.CuratedCourseDetailView.as_view()),
]

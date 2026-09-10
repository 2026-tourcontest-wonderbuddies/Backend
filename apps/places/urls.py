from django.urls import path
from apps.places import views

urlpatterns = [
    path("saved/", views.SavedPlaceListView.as_view()),
    path("<str:content_id>/save/", views.PlaceSaveView.as_view()),
]
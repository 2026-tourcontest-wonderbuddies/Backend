from django.urls import path
from apps.places import views

urlpatterns = [
    path("<str:content_id>/", views.PlaceDetailView()),
    path("<str:content_id>/ask/", views.PlaceAskView.as_view()),
]
from django.urls import path
from apps.places import views, search

urlpatterns = [
    path("search/", search.PlaceSearchView.as_view()),
    path("saved/", views.SavedPlaceListView.as_view()),
    path("<str:content_id>/save/", views.PlaceSaveView.as_view()),
    path("<str:content_id>/", views.PlaceDetailView.as_view()),
    path("<str:content_id>/ask/", views.PlaceAskView.as_view()),
]
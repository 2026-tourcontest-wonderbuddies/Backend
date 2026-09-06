from django.urls import path
from apps.trips import views

urlpatterns = [
     path("trips/", views.TripRequestCreateView.as_view()),
     path("trips/<int:trip_id>/courses/", views.TripCoursesListView.as_view()),
     path("courses/<int:course_id>/select/", views.CourseSelectView.as_view()),
     path("courses/<int:course_id>/", views.CourseDetailView.as_view()), 
     path("courses/<int:course_id>/places/", views.CoursePlacesView.as_view()),
     path("courses/<int:course_id>/lodging-options/",
         views.CourseLodgingOptionsView.as_view()),
     path("courses/<int:course_id>/select-lodging/",
         views.CourseSelectLodgingView.as_view()),
     path("courses/<int:course_id>/modify/", views.CourseModifyView.as_view()), 
     path("auth/google/", GoogleLoginView.as_view()), 
]
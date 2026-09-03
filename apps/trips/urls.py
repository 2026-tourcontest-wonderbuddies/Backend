from django.urls import path
from apps.trips import views

urlpatterns = [
    path("trips/", views.TripRequestCreateView.as_view()),                                    # 1
    path("trips/<int:trip_id>/courses/", views.TripCoursesListView.as_view()),                 # 2
    path("courses/<int:course_id>/select/", views.CourseSelectView.as_view()),                 # 3
    path("courses/<int:course_id>/", views.CourseDetailView.as_view()),                        # 4
    path("courses/<int:course_id>/places/", views.CoursePlacesView.as_view()),                 # 5
    path("courses/<int:course_id>/days/<int:day_index>/lodging-options/",
         views.DayLodgingOptionsView.as_view()),                                                # 6
    path("courses/<int:course_id>/days/<int:day_index>/select-lodging/",
         views.DaySelectLodgingView.as_view()),                                                 # 7
    path("courses/<int:course_id>/modify/", views.CourseModifyView.as_view()),                 # 8
]
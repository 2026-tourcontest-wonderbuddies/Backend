from django.urls import path
from apps.trips import views

urlpatterns = [
    path("trips/", views.TripRequestCreateView.as_view()),
    path("trips/<int:trip_id>/courses/", views.TripCoursesListView.as_view()),

    # 구체적인 고정 경로들을 먼저 (course_id 캐치올보다 위에)
    path("courses/saved/", views.SavedCourseListView.as_view()),

    path("courses/<int:course_id>/select/", views.CourseSelectView.as_view()),
    path("courses/<int:course_id>/places/", views.CoursePlacesView.as_view()),
    path("courses/<int:course_id>/lodging-options/", views.CourseLodgingOptionsView.as_view()),
    path("courses/<int:course_id>/select-lodging/", views.CourseSelectLodgingView.as_view()),
    path("courses/<int:course_id>/modify/", views.CourseModifyView.as_view()),
    path("courses/<int:course_id>/save/", views.CourseSaveView.as_view()),   # ★ 추가

    # 캐치올(가장 넓게 매칭)은 반드시 맨 마지막
    path("courses/<int:course_id>/", views.CourseDetailView.as_view()),

    path("auth/google/", views.GoogleLoginView.as_view()),
]
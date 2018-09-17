from django.conf.urls import url
from balsam.core import views, api_views

urlpatterns = [
    url(r'^$', views.home_page, name="home"),
    url(r'^tasks/$', views.list_tasks, name="tasks"),
    url(r'^apps/', views.list_apps, name="apps"),
    url(r'^api/tasks_list', api_views.list_tasks, name="api_tasks"),
]

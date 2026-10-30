"""
URL configuration for Montada project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('Mainapp.urls')),
    path('api/subscriptions/', include('Subscriptions.urls')),
    path('api/signals/', include('Signals.urls')),
    path('api/followers/', include('Followers.urls')),
    path('api/moderation/', include('Moderation.urls')),
    path('api/dashboard/', include('Dashboard.urls')),
    path('api/admin/', include('MontadaAdmin.urls')),
    path('api/news/', include('News.urls')),
    path('api/chat/', include('chat.urls')),
]

# Serve media files. Development: Django's static helper. Production (DEBUG=False): nginx
# proxies /media/ to this app, which otherwise answers 404, so serve it here. Prefer an
# nginx `location /media/ { alias <project>/media/; }` block; if it exists nginx answers
# first and this route is never hit.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
            serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]

# -*- coding: utf-8 -*-
import urlparse
from django.conf.urls import patterns, url
from django.contrib import admin
from django.contrib.admin.options import ModelAdmin
from django.core.urlresolvers import reverse
from django.http import (HttpResponseForbidden, HttpResponseBadRequest, HttpResponseRedirect, QueryDict)
from django.views.generic.base import View
from itsdangerous import URLSafeTimedSerializer
from simple_sso.sso_server.models import Token, Consumer
import datetime
import urllib
from webservices.models import Provider
from webservices.sync import provider_for_django


class BaseProvider(Provider):
    max_age = 5

    def __init__(self, server):
        self.server = server

    def get_private_key(self, public_key):
        try:
            self.consumer = Consumer.objects.get(public_key=public_key)
        except Consumer.DoesNotExist:
            return None
        return self.consumer.private_key


class RequestTokenProvider(BaseProvider):
    def provide(self, data):
        redirect_to = data['redirect_to']
        token = Token.objects.create(consumer=self.consumer, redirect_to=redirect_to)
        return {'request_token': token.request_token}


class AuthorizeView(View):
    """
    The client get's redirected to this view with the `request_token` obtained
    by the Request Token Request by the client application beforehand.
    
    This view checks if the user is logged in on the server application and if
    that user has the necessary rights.
    
    If the user is not logged in, the user is prompted to log in.
    """
    server = None

    def get(self, request):
        request_token = request.GET.get('token', None)
        if not request_token:
            return self.missing_token_argument()
        try:
            self.token = Token.objects.select_related('consumer').get(request_token=request_token)
        except Token.DoesNotExist:
            return self.token_not_found()
        if not self.check_token_timeout():
            return self.token_timeout()
        self.token.refresh()
        if request.user.is_authenticated():
            return self.handle_authenticated_user()
        else:
            return self.handle_unauthenticated_user()

    def missing_token_argument(self):
        return HttpResponseBadRequest('Token missing')

    def token_not_found(self):
        return HttpResponseForbidden('Token not found')

    def token_timeout(self):
        return HttpResponseForbidden('Token timed out')

    def check_token_timeout(self):
        delta = datetime.datetime.now() - self.token.timestamp
        if delta > self.server.token_timeout:
            self.token.delete()
            return False
        else:
            return True

    def handle_authenticated_user(self):
        if self.server.has_access(self.request.user, self.token.consumer):
            return self.success()
        else:
            return self.access_denied()

    def handle_unauthenticated_user(self):
        next = '%s?%s' % (self.request.path, urllib.urlencode([('token', self.token.request_token)]))
        url = '%s?%s' % (reverse(self.server.auth_view_name), urllib.urlencode([('next', next)]))
        return HttpResponseRedirect(url)

    def access_denied(self):
        return HttpResponseForbidden("Access denied")

    def success(self):
        self.token.user = self.request.user
        self.token.save()
        serializer = URLSafeTimedSerializer(self.token.consumer.private_key)
        parse_result = urlparse.urlparse(self.token.redirect_to)
        query_dict = QueryDict(parse_result.query, mutable=True)
        query_dict['access_token'] = serializer.dumps(self.token.access_token)
        url = urlparse.urlunparse((parse_result.scheme, parse_result.netloc, parse_result.path, '', query_dict.urlencode(), ''))
        return HttpResponseRedirect(url)


class VerificationProvider(BaseProvider, AuthorizeView):
    def provide(self, data):
        token = data['access_token']
        try:
            self.token = Token.objects.select_related('user').get(access_token=token, consumer=self.consumer)
        except Token.DoesNotExist:
            return self.token_not_found()
        if not self.check_token_timeout():
            return self.token_timeout()
        if not self.token.user:
            return self.token_not_bound()
        return self.server.get_user_data(self.token.user, self.consumer)

    def token_not_bound(self):
        return HttpResponseForbidden("Invalid token")


class ConsumerAdmin(ModelAdmin):
    readonly_fields = ['public_key', 'private_key']


class Server(object):
    request_token_provider = RequestTokenProvider
    authorize_view = AuthorizeView
    verification_provider = VerificationProvider
    token_timeout = datetime.timedelta(minutes=5)
    client_admin = ConsumerAdmin
    auth_view_name = 'django.contrib.auth.views.login'

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.register_admin()

    def register_admin(self):
        admin.site.register(Consumer, self.client_admin)

    def has_access(self, user, consumer):
        return True

    def get_user_data(self, user, consumer):
        return {
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_staff': False,
            'is_superuser': False,
        }

    def get_urls(self):
        return patterns('',
            url(r'^request-token/$', provider_for_django(self.request_token_provider(server=self)), name='simple-sso-request-token'),
            url(r'^authorize/$', self.authorize_view.as_view(server=self), name='simple-sso-authorize'),
            url(r'^verify/$', provider_for_django(self.verification_provider(server=self)), name='simple-sso-verify'),
        )


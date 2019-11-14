# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017-2018 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import os

from mock import Mock

import pkg_resources

from twisted.internet import defer

import synapse.rest.admin
from synapse.api.constants import LoginType
from synapse.api.errors import Codes
from synapse.appservice import ApplicationService
from synapse.rest.client.v1 import login
from synapse.rest.client.v2_alpha import account, account_validity, register, sync

from tests import unittest


class RegisterRestServletTestCase(unittest.HomeserverTestCase):

    servlets = [register.register_servlets]

    def make_homeserver(self, reactor, clock):

        self.url = b"/_matrix/client/r0/register"

        self.hs = self.setup_test_homeserver()
        self.hs.config.enable_registration = True
        self.hs.config.registrations_require_3pid = []
        self.hs.config.auto_join_rooms = []
        self.hs.config.enable_registration_captcha = False
        self.hs.config.allow_guest_access = True

        return self.hs

    def test_POST_appservice_registration_valid(self):
        user_id = "@as_user_kermit:test"
        as_token = "i_am_an_app_service"

        appservice = ApplicationService(
            as_token,
            self.hs.config.server_name,
            id="1234",
            namespaces={"users": [{"regex": r"@as_user.*", "exclusive": True}]},
        )

        self.hs.get_datastore().services_cache.append(appservice)
        request_data = json.dumps({"username": "as_user_kermit"})

        request, channel = self.make_request(
            b"POST", self.url + b"?access_token=i_am_an_app_service", request_data
        )
        self.render(request)

        self.assertEquals(channel.result["code"], b"200", channel.result)
        det_data = {"user_id": user_id, "home_server": self.hs.hostname}
        self.assertDictContainsSubset(det_data, channel.json_body)

    def test_POST_appservice_registration_invalid(self):
        self.appservice = None  # no application service exists
        request_data = json.dumps({"username": "kermit"})
        request, channel = self.make_request(
            b"POST", self.url + b"?access_token=i_am_an_app_service", request_data
        )
        self.render(request)

        self.assertEquals(channel.result["code"], b"401", channel.result)

    def test_POST_bad_password(self):
        request_data = json.dumps({"username": "kermit", "password": 666})
        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        self.assertEquals(channel.result["code"], b"400", channel.result)
        self.assertEquals(channel.json_body["error"], "Invalid password")

    def test_POST_bad_username(self):
        request_data = json.dumps({"username": 777, "password": "monkey"})
        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        self.assertEquals(channel.result["code"], b"400", channel.result)
        self.assertEquals(channel.json_body["error"], "Invalid username")

    def test_POST_user_valid(self):
        user_id = "@kermit:test"
        device_id = "frogfone"
        params = {
            "username": "kermit",
            "password": "monkey",
            "device_id": device_id,
            "auth": {"type": LoginType.DUMMY},
        }
        request_data = json.dumps(params)
        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        det_data = {
            "user_id": user_id,
            "home_server": self.hs.hostname,
            "device_id": device_id,
        }
        self.assertEquals(channel.result["code"], b"200", channel.result)
        self.assertDictContainsSubset(det_data, channel.json_body)

    def test_POST_disabled_registration(self):
        self.hs.config.enable_registration = False
        request_data = json.dumps({"username": "kermit", "password": "monkey"})
        self.auth_result = (None, {"username": "kermit", "password": "monkey"}, None)

        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        self.assertEquals(channel.result["code"], b"403", channel.result)
        self.assertEquals(channel.json_body["error"], "Registration has been disabled")

    def test_POST_guest_registration(self):
        self.hs.config.macaroon_secret_key = "test"
        self.hs.config.allow_guest_access = True

        request, channel = self.make_request(b"POST", self.url + b"?kind=guest", b"{}")
        self.render(request)

        det_data = {"home_server": self.hs.hostname, "device_id": "guest_device"}
        self.assertEquals(channel.result["code"], b"200", channel.result)
        self.assertDictContainsSubset(det_data, channel.json_body)

    def test_POST_disabled_guest_registration(self):
        self.hs.config.allow_guest_access = False

        request, channel = self.make_request(b"POST", self.url + b"?kind=guest", b"{}")
        self.render(request)

        self.assertEquals(channel.result["code"], b"403", channel.result)
        self.assertEquals(channel.json_body["error"], "Guest access is disabled")

    def test_POST_ratelimiting_guest(self):
        self.hs.config.rc_registration.burst_count = 5
        self.hs.config.rc_registration.per_second = 0.17

        for i in range(0, 6):
            url = self.url + b"?kind=guest"
            request, channel = self.make_request(b"POST", url, b"{}")
            self.render(request)

            if i == 5:
                self.assertEquals(channel.result["code"], b"429", channel.result)
                retry_after_ms = int(channel.json_body["retry_after_ms"])
            else:
                self.assertEquals(channel.result["code"], b"200", channel.result)

        self.reactor.advance(retry_after_ms / 1000.0)

        request, channel = self.make_request(b"POST", self.url + b"?kind=guest", b"{}")
        self.render(request)

        self.assertEquals(channel.result["code"], b"200", channel.result)

    def test_POST_ratelimiting(self):
        self.hs.config.rc_registration.burst_count = 5
        self.hs.config.rc_registration.per_second = 0.17

        for i in range(0, 6):
            params = {
                "username": "kermit" + str(i),
                "password": "monkey",
                "device_id": "frogfone",
                "auth": {"type": LoginType.DUMMY},
            }
            request_data = json.dumps(params)
            request, channel = self.make_request(b"POST", self.url, request_data)
            self.render(request)

            if i == 5:
                self.assertEquals(channel.result["code"], b"429", channel.result)
                retry_after_ms = int(channel.json_body["retry_after_ms"])
            else:
                self.assertEquals(channel.result["code"], b"200", channel.result)

        self.reactor.advance(retry_after_ms / 1000.0)

        request, channel = self.make_request(b"POST", self.url + b"?kind=guest", b"{}")
        self.render(request)

        self.assertEquals(channel.result["code"], b"200", channel.result)


class RegisterHideProfileTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
    ]

    def make_homeserver(self, reactor, clock):

        self.url = b"/_matrix/client/r0/register"

        config = self.default_config()
        config["enable_registration"] = True
        config["show_users_in_user_directory"] = False
        config["replicate_user_profiles_to"] = ["fakeserver"]

        mock_http_client = Mock(spec=[
            "get_json",
            "post_json_get_json",
        ])
        mock_http_client.post_json_get_json.return_value = defer.succeed((200, "{}"))

        self.hs = self.setup_test_homeserver(
            config=config,
            simple_http_client=mock_http_client,
        )

        return self.hs

    def test_profile_hidden(self):
        user_id = self.register_user("kermit", "monkey")

        post_json = self.hs.get_simple_http_client().post_json_get_json

        # We expect post_json_get_json to have been called twice: once with the original
        # profile and once with the None profile resulting from the request to hide it
        # from the user directory.
        self.assertEqual(post_json.call_count, 2, post_json.call_args_list)

        # Get the args (and not kwargs) passed to post_json.
        args = post_json.call_args[0]
        # Make sure the last call was attempting to replicate profiles.
        split_uri = args[0].split("/")
        self.assertEqual(split_uri[len(split_uri) - 1], "replicate_profiles", args[0])
        # Make sure the last profile update was overriding the user's profile to None.
        self.assertEqual(args[1]["batch"][user_id], None, args[1])


class AccountValidityTestCase(unittest.HomeserverTestCase):

    servlets = [
        register.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        sync.register_servlets,
        account_validity.register_servlets,
        account.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        config = self.default_config()
        # Test for account expiring after a week.
        config["enable_registration"] = True
        config["account_validity"] = {
            "enabled": True,
            "period": 604800000,  # Time in ms for 1 week
        }
        self.hs = self.setup_test_homeserver(config=config)

        return self.hs

    def test_validity_period(self):
        self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")

        # The specific endpoint doesn't matter, all we need is an authenticated
        # endpoint.
        request, channel = self.make_request(b"GET", "/sync", access_token=tok)
        self.render(request)

        self.assertEquals(channel.result["code"], b"200", channel.result)

        self.reactor.advance(datetime.timedelta(weeks=1).total_seconds())

        request, channel = self.make_request(b"GET", "/sync", access_token=tok)
        self.render(request)

        self.assertEquals(channel.result["code"], b"403", channel.result)
        self.assertEquals(
            channel.json_body["errcode"], Codes.EXPIRED_ACCOUNT, channel.result
        )

    def test_manual_renewal(self):
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")

        self.reactor.advance(datetime.timedelta(weeks=1).total_seconds())

        # If we register the admin user at the beginning of the test, it will
        # expire at the same time as the normal user and the renewal request
        # will be denied.
        self.register_user("admin", "adminpassword", admin=True)
        admin_tok = self.login("admin", "adminpassword")

        url = "/_matrix/client/unstable/admin/account_validity/validity"
        params = {"user_id": user_id}
        request_data = json.dumps(params)
        request, channel = self.make_request(
            b"POST", url, request_data, access_token=admin_tok
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        # The specific endpoint doesn't matter, all we need is an authenticated
        # endpoint.
        request, channel = self.make_request(b"GET", "/sync", access_token=tok)
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

    def test_manual_expire(self):
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")

        self.register_user("admin", "adminpassword", admin=True)
        admin_tok = self.login("admin", "adminpassword")

        url = "/_matrix/client/unstable/admin/account_validity/validity"
        params = {
            "user_id": user_id,
            "expiration_ts": 0,
            "enable_renewal_emails": False,
        }
        request_data = json.dumps(params)
        request, channel = self.make_request(
            b"POST", url, request_data, access_token=admin_tok
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        # The specific endpoint doesn't matter, all we need is an authenticated
        # endpoint.
        request, channel = self.make_request(b"GET", "/sync", access_token=tok)
        self.render(request)
        self.assertEquals(channel.result["code"], b"403", channel.result)
        self.assertEquals(
            channel.json_body["errcode"], Codes.EXPIRED_ACCOUNT, channel.result
        )


class AccountValidityUserDirectoryTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.client.v1.profile.register_servlets,
        synapse.rest.client.v1.room.register_servlets,
        synapse.rest.client.v2_alpha.user_directory.register_servlets,
        login.register_servlets,
        register.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        account_validity.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        config = self.default_config()

        # Set accounts to expire after a week
        config["enable_registration"] = True
        config["account_validity"] = {
            "enabled": True,
            "period": 604800000,  # Time in ms for 1 week
        }
        config["replicate_user_profiles_to"] = "test.is"

        # Mock homeserver requests to an identity server
        mock_http_client = Mock(spec=[
            "get_json",
            "post_json_get_json",
        ])
        mock_http_client.post_json_get_json.return_value = defer.succeed((200, "{}"))

        self.hs = self.setup_test_homeserver(
            config=config,
            simple_http_client=mock_http_client,
        )

        return self.hs

    def test_expired_user_in_directory(self):
        """Test that an expired user is hidden in the user directory"""
        # Create an admin user to search the user directory
        admin_id = self.register_user("admin", "adminpassword", admin=True)
        admin_tok = self.login("admin", "adminpassword")

        # Ensure the admin never expires
        url = "/_matrix/client/unstable/admin/account_validity/validity"
        params = {
            "user_id": admin_id,
            "expiration_ts": 999999999999,
            "enable_renewal_emails": False,
        }
        request_data = json.dumps(params)
        request, channel = self.make_request(
            b"POST", url, request_data, access_token=admin_tok
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        # Create a user to expire
        username = "kermit"
        user_id = self.register_user(username, "monkey")
        self.login(username, "monkey")

        self.pump(1000)
        self.reactor.advance(1000)
        self.pump()

        # Expire the user
        url = "/_matrix/client/unstable/admin/account_validity/validity"
        params = {
            "user_id": user_id,
            "expiration_ts": 0,
            "enable_renewal_emails": False,
        }
        request_data = json.dumps(params)
        request, channel = self.make_request(
            b"POST", url, request_data, access_token=admin_tok
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        # Wait for the background job to run which hides expired users in the directory
        self.pump(60 * 60 * 1000)

        # Mock the homeserver's HTTP client
        post_json = self.hs.get_simple_http_client().post_json_get_json

        # Check if the homeserver has replicated the user's profile to the identity server
        self.assertNotEquals(post_json.call_args, None, post_json.call_args)
        payload = post_json.call_args[0][1]
        batch = payload.get("batch")
        self.assertNotEquals(batch, None, batch)
        self.assertEquals(len(batch), 1, batch)
        replicated_user_id = list(batch.keys())[0]
        self.assertEquals(replicated_user_id, user_id, replicated_user_id)

        # There was replicated information about our user
        # Check that it's None, signifying that the user should be removed from the user
        # directory because they were expired
        replicated_content = batch[user_id]
        self.assertIsNone(replicated_content)


class AccountValidityRenewalByEmailTestCase(unittest.HomeserverTestCase):

    servlets = [
        register.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        sync.register_servlets,
        account_validity.register_servlets,
        account.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        config = self.default_config()

        # Test for account expiring after a week and renewal emails being sent 2
        # days before expiry.
        config["enable_registration"] = True
        config["account_validity"] = {
            "enabled": True,
            "period": 604800000,  # Time in ms for 1 week
            "renew_at": 172800000,  # Time in ms for 2 days
            "renew_by_email_enabled": True,
            "renew_email_subject": "Renew your account",
            "account_renewed_html_path": "account_renewed.html",
            "invalid_token_html_path": "invalid_token.html",
        }

        # Email config.
        self.email_attempts = []

        def sendmail(*args, **kwargs):
            self.email_attempts.append((args, kwargs))
            return

        config["email"] = {
            "enable_notifs": True,
            "template_dir": os.path.abspath(
                pkg_resources.resource_filename('synapse', 'res/templates')
            ),
            "expiry_template_html": "notice_expiry.html",
            "expiry_template_text": "notice_expiry.txt",
            "notif_template_html": "notif_mail.html",
            "notif_template_text": "notif_mail.txt",
            "smtp_host": "127.0.0.1",
            "smtp_port": 20,
            "require_transport_security": False,
            "smtp_user": None,
            "smtp_pass": None,
            "notif_from": "test@example.com",
        }
        config["public_baseurl"] = "aaa"

        self.hs = self.setup_test_homeserver(config=config, sendmail=sendmail)

        self.store = self.hs.get_datastore()

        return self.hs

    def test_renewal_email(self):
        self.email_attempts = []

        (user_id, tok) = self.create_user()

        # Move 6 days forward. This should trigger a renewal email to be sent.
        self.reactor.advance(datetime.timedelta(days=6).total_seconds())
        self.assertEqual(len(self.email_attempts), 1)

        # Retrieving the URL from the email is too much pain for now, so we
        # retrieve the token from the DB.
        renewal_token = self.get_success(self.store.get_renewal_token_for_user(user_id))
        url = "/_matrix/client/unstable/account_validity/renew?token=%s" % renewal_token
        request, channel = self.make_request(b"GET", url)
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        # Check that we're getting HTML back.
        content_type = None
        for header in channel.result.get("headers", []):
            if header[0] == b"Content-Type":
                content_type = header[1]
        self.assertEqual(content_type, b"text/html; charset=utf-8", channel.result)

        # Check that the HTML we're getting is the one we expect on a successful renewal.
        expected_html = self.hs.config.account_validity.account_renewed_html_content
        self.assertEqual(
            channel.result["body"], expected_html.encode("utf8"), channel.result
        )

        # Move 3 days forward. If the renewal failed, every authed request with
        # our access token should be denied from now, otherwise they should
        # succeed.
        self.reactor.advance(datetime.timedelta(days=3).total_seconds())
        request, channel = self.make_request(b"GET", "/sync", access_token=tok)
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

    def test_renewal_invalid_token(self):
        # Hit the renewal endpoint with an invalid token and check that it behaves as
        # expected, i.e. that it responds with 404 Not Found and the correct HTML.
        url = "/_matrix/client/unstable/account_validity/renew?token=123"
        request, channel = self.make_request(b"GET", url)
        self.render(request)
        self.assertEquals(channel.result["code"], b"404", channel.result)

        # Check that we're getting HTML back.
        content_type = None
        for header in channel.result.get("headers", []):
            if header[0] == b"Content-Type":
                content_type = header[1]
        self.assertEqual(content_type, b"text/html; charset=utf-8", channel.result)

        # Check that the HTML we're getting is the one we expect when using an
        # invalid/unknown token.
        expected_html = self.hs.config.account_validity.invalid_token_html_content
        self.assertEqual(
            channel.result["body"], expected_html.encode("utf8"), channel.result
        )

    def test_manual_email_send(self):
        self.email_attempts = []

        (user_id, tok) = self.create_user()
        request, channel = self.make_request(
            b"POST",
            "/_matrix/client/unstable/account_validity/send_mail",
            access_token=tok,
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        self.assertEqual(len(self.email_attempts), 1)

    def test_deactivated_user(self):
        self.email_attempts = []

        (user_id, tok) = self.create_user()

        request_data = json.dumps({
            "auth": {
                "type": "m.login.password",
                "user": user_id,
                "password": "monkey",
            },
            "erase": False,
        })
        request, channel = self.make_request(
            "POST",
            "account/deactivate",
            request_data,
            access_token=tok,
        )
        self.render(request)
        self.assertEqual(request.code, 200, channel.result)

        self.reactor.advance(datetime.timedelta(days=8).total_seconds())

        self.assertEqual(len(self.email_attempts), 0)

    def create_user(self):
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")
        # We need to manually add an email address otherwise the handler will do
        # nothing.
        now = self.hs.clock.time_msec()
        self.get_success(
            self.store.user_add_threepid(
                user_id=user_id,
                medium="email",
                address="kermit@example.com",
                validated_at=now,
                added_at=now,
            )
        )
        return (user_id, tok)

    def test_manual_email_send_expired_account(self):
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")

        # We need to manually add an email address otherwise the handler will do
        # nothing.
        now = self.hs.clock.time_msec()
        self.get_success(
            self.store.user_add_threepid(
                user_id=user_id,
                medium="email",
                address="kermit@example.com",
                validated_at=now,
                added_at=now,
            )
        )

        # Make the account expire.
        self.reactor.advance(datetime.timedelta(days=8).total_seconds())

        # Ignore all emails sent by the automatic background task and only focus on the
        # ones sent manually.
        self.email_attempts = []

        # Test that we're still able to manually trigger a mail to be sent.
        request, channel = self.make_request(
            b"POST",
            "/_matrix/client/unstable/account_validity/send_mail",
            access_token=tok,
        )
        self.render(request)
        self.assertEquals(channel.result["code"], b"200", channel.result)

        self.assertEqual(len(self.email_attempts), 1)


class AccountValidityBackgroundJobTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
    ]

    def make_homeserver(self, reactor, clock):
        self.validity_period = 10
        self.max_delta = self.validity_period * 10. / 100.

        config = self.default_config()

        config["enable_registration"] = True
        config["account_validity"] = {
            "enabled": False,
        }

        self.hs = self.setup_test_homeserver(config=config)
        self.hs.config.account_validity.period = self.validity_period

        self.store = self.hs.get_datastore()

        return self.hs

    def test_background_job(self):
        """
        Tests the same thing as test_background_job, except that it sets the
        startup_job_max_delta parameter and checks that the expiration date is within the
        allowed range.
        """
        user_id = self.register_user("kermit_delta", "user")

        self.hs.config.account_validity.startup_job_max_delta = self.max_delta

        now_ms = self.hs.clock.time_msec()
        self.get_success(self.store._set_expiration_date_when_missing())

        res = self.get_success(self.store.get_expiration_ts_for_user(user_id))

        self.assertGreaterEqual(res, now_ms + self.validity_period - self.max_delta)
        self.assertLessEqual(res, now_ms + self.validity_period)

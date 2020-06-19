# -*- coding: utf-8 -*-
# Copyright 2015-2016 OpenMarket Ltd
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

import hmac
import logging
import re
from typing import List, Union

from six import string_types

import synapse
import synapse.api.auth
import synapse.types
from synapse.api.constants import LoginType
from synapse.api.errors import (
    Codes,
    LimitExceededError,
    SynapseError,
    ThreepidValidationError,
    UnrecognizedRequestError,
)
from synapse.config import ConfigError
from synapse.config.captcha import CaptchaConfig
from synapse.config.consent_config import ConsentConfig
from synapse.config.emailconfig import ThreepidBehaviour
from synapse.config.ratelimiting import FederationRateLimitConfig
from synapse.config.registration import RegistrationConfig
from synapse.config.server import is_threepid_reserved
from synapse.handlers.auth import AuthHandler
from synapse.http.server import finish_request
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
    parse_string,
)
from synapse.push.mailer import load_jinja2_templates
from synapse.util.msisdn import phone_number_to_msisdn
from synapse.util.ratelimitutils import FederationRateLimiter
from synapse.util.stringutils import assert_valid_client_secret, random_string
from synapse.util.threepids import check_3pid_allowed

from ._base import client_patterns, interactive_auth_handler

# We ought to be using hmac.compare_digest() but on older pythons it doesn't
# exist. It's a _really minor_ security flaw to use plain string comparison
# because the timing attack is so obscured by all the other code here it's
# unlikely to make much difference
if hasattr(hmac, "compare_digest"):
    compare_digest = hmac.compare_digest
else:

    def compare_digest(a, b):
        return a == b


logger = logging.getLogger(__name__)


class EmailRegisterRequestTokenRestServlet(RestServlet):
    PATTERNS = client_patterns("/register/email/requestToken$")

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        super(EmailRegisterRequestTokenRestServlet, self).__init__()
        self.hs = hs
        self.identity_handler = hs.get_handlers().identity_handler
        self.config = hs.config

        if self.hs.config.threepid_behaviour_email == ThreepidBehaviour.LOCAL:
            from synapse.push.mailer import Mailer, load_jinja2_templates

            template_html, template_text = load_jinja2_templates(
                self.config.email_template_dir,
                [
                    self.config.email_registration_template_html,
                    self.config.email_registration_template_text,
                ],
                apply_format_ts_filter=True,
                apply_mxc_to_http_filter=True,
                public_baseurl=self.config.public_baseurl,
            )
            self.mailer = Mailer(
                hs=self.hs,
                app_name=self.config.email_app_name,
                template_html=template_html,
                template_text=template_text,
            )

    async def on_POST(self, request):
        if self.hs.config.threepid_behaviour_email == ThreepidBehaviour.OFF:
            if self.hs.config.local_threepid_handling_disabled_due_to_email_config:
                logger.warning(
                    "Email registration has been disabled due to lack of email config"
                )
            raise SynapseError(
                400, "Email-based registration has been disabled on this server"
            )
        body = parse_json_object_from_request(request)

        assert_params_in_dict(body, ["client_secret", "email", "send_attempt"])

        # Extract params from body
        client_secret = body["client_secret"]
        assert_valid_client_secret(client_secret)

        email = body["email"]
        send_attempt = body["send_attempt"]
        next_link = body.get("next_link")  # Optional param

        if not (await check_3pid_allowed(self.hs, "email", body["email"])):
            raise SynapseError(
                403,
                "Your email is not authorized to register on this server",
                Codes.THREEPID_DENIED,
            )

        existing_user_id = await self.hs.get_datastore().get_user_id_by_threepid(
            "email", body["email"]
        )

        if existing_user_id is not None:
            if self.hs.config.request_token_inhibit_3pid_errors:
                # Make the client think the operation succeeded. See the rationale in the
                # comments for request_token_inhibit_3pid_errors.
                return 200, {"sid": random_string(16)}

            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        if self.config.threepid_behaviour_email == ThreepidBehaviour.REMOTE:
            assert self.hs.config.account_threepid_delegate_email

            # Have the configured identity server handle the request
            ret = await self.identity_handler.requestEmailToken(
                self.hs.config.account_threepid_delegate_email,
                email,
                client_secret,
                send_attempt,
                next_link,
            )
        else:
            # Send registration emails from Synapse
            sid = await self.identity_handler.send_threepid_validation(
                email,
                client_secret,
                send_attempt,
                self.mailer.send_registration_mail,
                next_link,
            )

            # Wrap the session id in a JSON object
            ret = {"sid": sid}

        return 200, ret


class MsisdnRegisterRequestTokenRestServlet(RestServlet):
    PATTERNS = client_patterns("/register/msisdn/requestToken$")

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        super(MsisdnRegisterRequestTokenRestServlet, self).__init__()
        self.hs = hs
        self.identity_handler = hs.get_handlers().identity_handler

    async def on_POST(self, request):
        body = parse_json_object_from_request(request)

        assert_params_in_dict(
            body, ["client_secret", "country", "phone_number", "send_attempt"]
        )
        client_secret = body["client_secret"]
        country = body["country"]
        phone_number = body["phone_number"]
        send_attempt = body["send_attempt"]
        next_link = body.get("next_link")  # Optional param

        msisdn = phone_number_to_msisdn(country, phone_number)

        assert_valid_client_secret(body["client_secret"])

        if not (await check_3pid_allowed(self.hs, "msisdn", msisdn)):
            raise SynapseError(
                403,
                "Phone numbers are not authorized to register on this server",
                Codes.THREEPID_DENIED,
            )

        existing_user_id = await self.hs.get_datastore().get_user_id_by_threepid(
            "msisdn", msisdn
        )

        if existing_user_id is not None:
            if self.hs.config.request_token_inhibit_3pid_errors:
                # Make the client think the operation succeeded. See the rationale in the
                # comments for request_token_inhibit_3pid_errors.
                return 200, {"sid": random_string(16)}

            raise SynapseError(
                400, "Phone number is already in use", Codes.THREEPID_IN_USE
            )

        if not self.hs.config.account_threepid_delegate_msisdn:
            logger.warning(
                "No upstream msisdn account_threepid_delegate configured on the server to "
                "handle this request"
            )
            raise SynapseError(
                400, "Registration by phone number is not supported on this homeserver"
            )

        ret = await self.identity_handler.requestMsisdnToken(
            self.hs.config.account_threepid_delegate_msisdn,
            country,
            phone_number,
            client_secret,
            send_attempt,
            next_link,
        )

        return 200, ret


class RegistrationSubmitTokenServlet(RestServlet):
    """Handles registration 3PID validation token submission"""

    PATTERNS = client_patterns(
        "/registration/(?P<medium>[^/]*)/submit_token$", releases=(), unstable=True
    )

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        super(RegistrationSubmitTokenServlet, self).__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.config = hs.config
        self.clock = hs.get_clock()
        self.store = hs.get_datastore()

        if self.config.threepid_behaviour_email == ThreepidBehaviour.LOCAL:
            (self.failure_email_template,) = load_jinja2_templates(
                self.config.email_template_dir,
                [self.config.email_registration_template_failure_html],
            )

        if self.config.threepid_behaviour_email == ThreepidBehaviour.LOCAL:
            (self.failure_email_template,) = load_jinja2_templates(
                self.config.email_template_dir,
                [self.config.email_registration_template_failure_html],
            )

    async def on_GET(self, request, medium):
        if medium != "email":
            raise SynapseError(
                400, "This medium is currently not supported for registration"
            )
        if self.config.threepid_behaviour_email == ThreepidBehaviour.OFF:
            if self.config.local_threepid_handling_disabled_due_to_email_config:
                logger.warning(
                    "User registration via email has been disabled due to lack of email config"
                )
            raise SynapseError(
                400, "Email-based registration is disabled on this server"
            )

        sid = parse_string(request, "sid", required=True)
        client_secret = parse_string(request, "client_secret", required=True)
        token = parse_string(request, "token", required=True)

        # Attempt to validate a 3PID session
        try:
            # Mark the session as valid
            next_link = await self.store.validate_threepid_session(
                sid, client_secret, token, self.clock.time_msec()
            )

            # Perform a 302 redirect if next_link is set
            if next_link:
                if next_link.startswith("file:///"):
                    logger.warning(
                        "Not redirecting to next_link as it is a local file: address"
                    )
                else:
                    request.setResponseCode(302)
                    request.setHeader("Location", next_link)
                    finish_request(request)
                    return None

            # Otherwise show the success template
            html = self.config.email_registration_template_success_html_content

            request.setResponseCode(200)
        except ThreepidValidationError as e:
            request.setResponseCode(e.code)

            # Show a failure page with a reason
            template_vars = {"failure_reason": e.msg}
            html = self.failure_email_template.render(**template_vars)

        request.write(html.encode("utf-8"))
        finish_request(request)


class UsernameAvailabilityRestServlet(RestServlet):
    PATTERNS = client_patterns("/register/available")

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        super(UsernameAvailabilityRestServlet, self).__init__()
        self.hs = hs
        self.registration_handler = hs.get_registration_handler()
        self.ratelimiter = FederationRateLimiter(
            hs.get_clock(),
            FederationRateLimitConfig(
                # Time window of 2s
                window_size=2000,
                # Artificially delay requests if rate > sleep_limit/window_size
                sleep_limit=1,
                # Amount of artificial delay to apply
                sleep_msec=1000,
                # Error with 429 if more than reject_limit requests are queued
                reject_limit=1,
                # Allow 1 request at a time
                concurrent_requests=1,
            ),
        )

    async def on_GET(self, request):
        if not self.hs.config.enable_registration:
            raise SynapseError(
                403, "Registration has been disabled", errcode=Codes.FORBIDDEN
            )

        # We are not interested in logging in via a username in this deployment.
        # Simply allow anything here as it won't be used later.
        return 200, {"available": True}


class RegisterRestServlet(RestServlet):
    PATTERNS = client_patterns("/register$")

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        super(RegisterRestServlet, self).__init__()

        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.auth_handler = hs.get_auth_handler()
        self.registration_handler = hs.get_registration_handler()
        self.identity_handler = hs.get_handlers().identity_handler
        self.room_member_handler = hs.get_room_member_handler()
        self.macaroon_gen = hs.get_macaroon_generator()
        self.ratelimiter = hs.get_registration_ratelimiter()
        self.password_policy_handler = hs.get_password_policy_handler()
        self.clock = hs.get_clock()

        self._registration_flows = _calculate_registration_flows(
            hs.config, self.auth_handler
        )

    @interactive_auth_handler
    async def on_POST(self, request):
        body = parse_json_object_from_request(request)

        client_addr = request.getClientIP()

        time_now = self.clock.time()

        allowed, time_allowed = self.ratelimiter.can_do_action(
            client_addr,
            time_now_s=time_now,
            rate_hz=self.hs.config.rc_registration.per_second,
            burst_count=self.hs.config.rc_registration.burst_count,
            update=False,
        )

        if not allowed:
            raise LimitExceededError(
                retry_after_ms=int(1000 * (time_allowed - time_now))
            )

        kind = b"user"
        if b"kind" in request.args:
            kind = request.args[b"kind"][0]

        if kind == b"guest":
            ret = await self._do_guest_registration(body, address=client_addr)
            return ret
        elif kind != b"user":
            raise UnrecognizedRequestError(
                "Do not understand membership kind: %s" % (kind.decode("utf8"),)
            )

        # we do basic sanity checks here because the auth layer will store these
        # in sessions. Pull out the username/password provided to us.
        desired_password_hash = None
        if "password" in body:
            password = body.pop("password")
            if not isinstance(password, string_types) or len(password) > 512:
                raise SynapseError(400, "Invalid password")
            self.password_policy_handler.validate_password(password)

            # If the password is valid, hash it and store it back on the body.
            # This ensures that only the hashed password is handled everywhere.
            if "password_hash" in body:
                raise SynapseError(400, "Unexpected property: password_hash")
            body["password_hash"] = await self.auth_handler.hash(password)
            desired_password_hash = body["password_hash"]

        # We don't care about usernames for this deployments. In fact,
        # the act of checking whether they exist already can leak metadata about
        # which users are already registered.
        #
        # Usernames are already derived via the provided email.
        # So, if they're not necessary, just ignore them.
        #
        # (we do still allow appservices to set them below)
        desired_username = None

        desired_display_name = body.get("display_name")

        appservice = None
        if self.auth.has_access_token(request):
            appservice = await self.auth.get_appservice_by_req(request)

        # fork off as soon as possible for ASes which have completely
        # different registration flows to normal users

        # == Application Service Registration ==
        if appservice:
            # Set the desired user according to the AS API (which uses the
            # 'user' key not 'username'). Since this is a new addition, we'll
            # fallback to 'username' if they gave one.
            desired_username = body.get("user", body.get("username"))

            # XXX we should check that desired_username is valid. Currently
            # we give appservices carte blanche for any insanity in mxids,
            # because the IRC bridges rely on being able to register stupid
            # IDs.

            access_token = self.auth.get_access_token_from_request(request)

            if isinstance(desired_username, string_types):
                result = await self._do_appservice_registration(
                    desired_username,
                    desired_password_hash,
                    desired_display_name,
                    access_token,
                    body,
                )
            return 200, result  # we throw for non 200 responses

        # == Normal User Registration == (everyone else)
        if not self.hs.config.enable_registration:
            raise SynapseError(403, "Registration has been disabled")

        guest_access_token = body.get("guest_access_token", None)

        if "initial_device_display_name" in body and "password_hash" not in body:
            # ignore 'initial_device_display_name' if sent without
            # a password to work around a client bug where it sent
            # the 'initial_device_display_name' param alone, wiping out
            # the original registration params
            logger.warning("Ignoring initial_device_display_name without password")
            del body["initial_device_display_name"]

        session_id = self.auth_handler.get_session_id(body)
        registered_user_id = None
        if session_id:
            # if we get a registered user id out of here, it means we previously
            # registered a user for this session, so we could just return the
            # user here. We carry on and go through the auth checks though,
            # for paranoia.
            registered_user_id = await self.auth_handler.get_session_data(
                session_id, "registered_user_id", None
            )

        auth_result, params, session_id = await self.auth_handler.check_auth(
            self._registration_flows,
            request,
            body,
            self.hs.get_ip_from_request(request),
            "register a new account",
        )

        # Check that we're not trying to register a denied 3pid.
        #
        # the user-facing checks will probably already have happened in
        # /register/email/requestToken when we requested a 3pid, but that's not
        # guaranteed.

        if auth_result:
            for login_type in [LoginType.EMAIL_IDENTITY, LoginType.MSISDN]:
                if login_type in auth_result:
                    medium = auth_result[login_type]["medium"]
                    address = auth_result[login_type]["address"]

                    if not (await check_3pid_allowed(self.hs, medium, address)):
                        raise SynapseError(
                            403,
                            "Third party identifiers (email/phone numbers)"
                            + " are not authorized on this server",
                            Codes.THREEPID_DENIED,
                        )

                    existingUid = await self.store.get_user_id_by_threepid(
                        medium, address
                    )

                    if existingUid is not None:
                        raise SynapseError(
                            400, "%s is already in use" % medium, Codes.THREEPID_IN_USE
                        )

        if self.hs.config.register_mxid_from_3pid:
            # override the desired_username based on the 3PID if any.
            # reset it first to avoid folks picking their own username.
            desired_username = None

            # we should have an auth_result at this point if we're going to progress
            # to register the user (i.e. we haven't picked up a registered_user_id
            # from our session store), in which case get ready and gen the
            # desired_username
            if auth_result:
                if (
                    self.hs.config.register_mxid_from_3pid == "email"
                    and LoginType.EMAIL_IDENTITY in auth_result
                ):
                    address = auth_result[LoginType.EMAIL_IDENTITY]["address"]
                    desired_username = synapse.types.strip_invalid_mxid_characters(
                        address.replace("@", "-").lower()
                    )

                    # find a unique mxid for the account, suffixing numbers
                    # if needed
                    while True:
                        try:
                            await self.registration_handler.check_username(
                                desired_username,
                                guest_access_token=guest_access_token,
                                assigned_user_id=registered_user_id,
                            )
                            # if we got this far we passed the check.
                            break
                        except SynapseError as e:
                            if e.errcode == Codes.USER_IN_USE:
                                m = re.match(r"^(.*?)(\d+)$", desired_username)
                                if m:
                                    desired_username = m.group(1) + str(
                                        int(m.group(2)) + 1
                                    )
                                else:
                                    desired_username += "1"
                            else:
                                # something else went wrong.
                                break

                    if self.hs.config.register_just_use_email_for_display_name:
                        desired_display_name = address
                    else:
                        # Custom mapping between email address and display name
                        desired_display_name = _map_email_to_displayname(address)
                elif (
                    self.hs.config.register_mxid_from_3pid == "msisdn"
                    and LoginType.MSISDN in auth_result
                ):
                    desired_username = auth_result[LoginType.MSISDN]["address"]
                else:
                    raise SynapseError(
                        400, "Cannot derive mxid from 3pid; no recognised 3pid"
                    )

        if desired_username is not None:
            await self.registration_handler.check_username(
                desired_username,
                guest_access_token=guest_access_token,
                assigned_user_id=registered_user_id,
            )

        if registered_user_id is not None:
            logger.info(
                "Already registered user ID %r for this session", registered_user_id
            )
            # don't re-register the threepids
            registered = False
        else:
            # NB: This may be from the auth handler and NOT from the POST
            assert_params_in_dict(params, ["password_hash"])

            if not self.hs.config.register_mxid_from_3pid:
                desired_username = params.get("username", None)
            else:
                # we keep the original desired_username derived from the 3pid above
                pass

            guest_access_token = params.get("guest_access_token", None)
            new_password_hash = params.get("password_hash", None)

            if desired_username is not None:
                desired_username = desired_username.lower()

            threepid = None
            if auth_result:
                threepid = auth_result.get(LoginType.EMAIL_IDENTITY)

                # Also check that we're not trying to register a 3pid that's already
                # been registered.
                #
                # This has probably happened in /register/email/requestToken as well,
                # but if a user hits this endpoint twice then clicks on each link from
                # the two activation emails, they would register the same 3pid twice.
                for login_type in [LoginType.EMAIL_IDENTITY, LoginType.MSISDN]:
                    if login_type in auth_result:
                        medium = auth_result[login_type]["medium"]
                        address = auth_result[login_type]["address"]

                        existing_user_id = await self.store.get_user_id_by_threepid(
                            medium, address
                        )

                        if existing_user_id is not None:
                            raise SynapseError(
                                400,
                                "%s is already in use" % medium,
                                Codes.THREEPID_IN_USE,
                            )

            registered_user_id = await self.registration_handler.register_user(
                localpart=desired_username,
                password_hash=new_password_hash,
                guest_access_token=guest_access_token,
                default_display_name=desired_display_name,
                threepid=threepid,
                address=client_addr,
            )
            # Necessary due to auth checks prior to the threepid being
            # written to the db
            if threepid:
                if is_threepid_reserved(
                    self.hs.config.mau_limits_reserved_threepids, threepid
                ):
                    await self.store.upsert_monthly_active_user(registered_user_id)

            if self.hs.config.shadow_server:
                await self.registration_handler.shadow_register(
                    localpart=desired_username,
                    display_name=desired_display_name,
                    auth_result=auth_result,
                    params=params,
                )

            # remember that we've now registered that user account, and with
            #  what user ID (since the user may not have specified)
            await self.auth_handler.set_session_data(
                session_id, "registered_user_id", registered_user_id
            )

            registered = True

        return_dict = await self._create_registration_details(
            registered_user_id, params
        )

        if registered:
            await self.registration_handler.post_registration_actions(
                user_id=registered_user_id,
                auth_result=auth_result,
                access_token=return_dict.get("access_token"),
            )

        return 200, return_dict

    def on_OPTIONS(self, _):
        return 200, {}

    async def _do_appservice_registration(
        self, username, password_hash, display_name, as_token, body
    ):
        # FIXME: appservice_register() is horribly duplicated with register()
        # and they should probably just be combined together with a config flag.
        user_id = await self.registration_handler.appservice_register(
            username, as_token, password_hash, display_name
        )
        result = await self._create_registration_details(user_id, body)

        auth_result = body.get("auth_result")
        if auth_result and LoginType.EMAIL_IDENTITY in auth_result:
            threepid = auth_result[LoginType.EMAIL_IDENTITY]
            await self.registration_handler.register_email_threepid(
                user_id, threepid, result["access_token"]
            )

        if auth_result and LoginType.MSISDN in auth_result:
            threepid = auth_result[LoginType.MSISDN]
            await self.registration_handler.register_msisdn_threepid(
                user_id, threepid, result["access_token"]
            )

        return result

    async def _create_registration_details(self, user_id, params):
        """Complete registration of newly-registered user

        Allocates device_id if one was not given; also creates access_token.

        Args:
            (str) user_id: full canonical @user:id
            (object) params: registration parameters, from which we pull
                device_id, initial_device_name and inhibit_login
        Returns:
            defer.Deferred: (object) dictionary for response from /register
        """
        result = {"user_id": user_id, "home_server": self.hs.hostname}
        if not params.get("inhibit_login", False):
            device_id = params.get("device_id")
            initial_display_name = params.get("initial_device_display_name")
            device_id, access_token = await self.registration_handler.register_device(
                user_id, device_id, initial_display_name, is_guest=False
            )

            result.update({"access_token": access_token, "device_id": device_id})
        return result

    async def _do_guest_registration(self, params, address=None):
        if not self.hs.config.allow_guest_access:
            raise SynapseError(403, "Guest access is disabled")
        user_id = await self.registration_handler.register_user(
            make_guest=True, address=address
        )

        # we don't allow guests to specify their own device_id, because
        # we have nowhere to store it.
        device_id = synapse.api.auth.GUEST_DEVICE_ID
        initial_display_name = params.get("initial_device_display_name")
        device_id, access_token = await self.registration_handler.register_device(
            user_id, device_id, initial_display_name, is_guest=True
        )

        return (
            200,
            {
                "user_id": user_id,
                "device_id": device_id,
                "access_token": access_token,
                "home_server": self.hs.hostname,
            },
        )


def cap(name):
    """Capitalise parts of a name containing different words, including those
    separated by hyphens.
    For example, 'John-Doe'

    Args:
        name (str): The name to parse
    """
    if not name:
        return name

    # Split the name by whitespace then hyphens, capitalizing each part then
    # joining it back together.
    capatilized_name = " ".join(
        "-".join(part.capitalize() for part in space_part.split("-"))
        for space_part in name.split()
    )
    return capatilized_name


def _map_email_to_displayname(address):
    """Custom mapping from an email address to a user displayname

    Args:
        address (str): The email address to process
    Returns:
        str: The new displayname
    """
    # Split the part before and after the @ in the email.
    # Replace all . with spaces in the first part
    parts = address.replace(".", " ").split("@")

    # Figure out which org this email address belongs to
    org_parts = parts[1].split(" ")

    # If this is a ...matrix.org email, mark them as an Admin
    if org_parts[-2] == "matrix" and org_parts[-1] == "org":
        org = "Tchap Admin"

    # Is this is a ...gouv.fr address, set the org to whatever is before
    # gouv.fr. If there isn't anything (a @gouv.fr email) simply mark their
    # org as "gouv"
    elif org_parts[-2] == "gouv" and org_parts[-1] == "fr":
        org = org_parts[-3] if len(org_parts) > 2 else org_parts[-2]

    # Otherwise, mark their org as the email's second-level domain name
    else:
        org = org_parts[-2]

    desired_display_name = cap(parts[0]) + " [" + cap(org) + "]"

    return desired_display_name


def _calculate_registration_flows(
    # technically `config` has to provide *all* of these interfaces, not just one
    config: Union[RegistrationConfig, ConsentConfig, CaptchaConfig],
    auth_handler: AuthHandler,
) -> List[List[str]]:
    """Get a suitable flows list for registration

    Args:
        config: server configuration
        auth_handler: authorization handler

    Returns: a list of supported flows
    """
    # FIXME: need a better error than "no auth flow found" for scenarios
    # where we required 3PID for registration but the user didn't give one
    require_email = "email" in config.registrations_require_3pid
    require_msisdn = "msisdn" in config.registrations_require_3pid

    show_msisdn = True
    show_email = True

    if config.disable_msisdn_registration:
        show_msisdn = False
        require_msisdn = False

    enabled_auth_types = auth_handler.get_enabled_auth_types()
    if LoginType.EMAIL_IDENTITY not in enabled_auth_types:
        show_email = False
        if require_email:
            raise ConfigError(
                "Configuration requires email address at registration, but email "
                "validation is not configured"
            )

    if LoginType.MSISDN not in enabled_auth_types:
        show_msisdn = False
        if require_msisdn:
            raise ConfigError(
                "Configuration requires msisdn at registration, but msisdn "
                "validation is not configured"
            )

    flows = []

    # only support 3PIDless registration if no 3PIDs are required
    if not require_email and not require_msisdn:
        # Add a dummy step here, otherwise if a client completes
        # recaptcha first we'll assume they were going for this flow
        # and complete the request, when they could have been trying to
        # complete one of the flows with email/msisdn auth.
        flows.append([LoginType.DUMMY])

    # only support the email-only flow if we don't require MSISDN 3PIDs
    if show_email and not require_msisdn:
        flows.append([LoginType.EMAIL_IDENTITY])

    # only support the MSISDN-only flow if we don't require email 3PIDs
    if show_msisdn and not require_email:
        flows.append([LoginType.MSISDN])

    if show_email and show_msisdn:
        # always let users provide both MSISDN & email
        flows.append([LoginType.MSISDN, LoginType.EMAIL_IDENTITY])

    # Prepend m.login.terms to all flows if we're requiring consent
    if config.user_consent_at_registration:
        for flow in flows:
            flow.insert(0, LoginType.TERMS)

    # Prepend recaptcha to all flows if we're requiring captcha
    if config.enable_registration_captcha:
        for flow in flows:
            flow.insert(0, LoginType.RECAPTCHA)

    return flows


def register_servlets(hs, http_server):
    EmailRegisterRequestTokenRestServlet(hs).register(http_server)
    MsisdnRegisterRequestTokenRestServlet(hs).register(http_server)
    UsernameAvailabilityRestServlet(hs).register(http_server)
    RegistrationSubmitTokenServlet(hs).register(http_server)
    RegisterRestServlet(hs).register(http_server)

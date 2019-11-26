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
from hashlib import sha1

from six import string_types

from twisted.internet import defer

import synapse
import synapse.types
from synapse.api.constants import LoginType
from synapse.api.errors import (
    Codes,
    LimitExceededError,
    SynapseError,
    UnrecognizedRequestError,
)
from synapse.config.ratelimiting import FederationRateLimitConfig
from synapse.config.server import is_threepid_reserved
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
    parse_string,
)
from synapse.util.msisdn import phone_number_to_msisdn
from synapse.util.ratelimitutils import FederationRateLimiter
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

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        assert_params_in_dict(body, [
            'id_server', 'client_secret', 'email', 'send_attempt'
        ])

        if not (yield check_3pid_allowed(self.hs, "email", body['email'])):
            raise SynapseError(
                403,
                "Your email is not authorized to register on this server",
                Codes.THREEPID_DENIED,
            )

        existingUid = yield self.hs.get_datastore().get_user_id_by_threepid(
            'email', body['email']
        )

        if existingUid is not None:
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        ret = yield self.identity_handler.requestEmailToken(**body)
        defer.returnValue((200, ret))


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

    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        assert_params_in_dict(body, [
            'id_server', 'client_secret',
            'country', 'phone_number',
            'send_attempt',
        ])

        msisdn = phone_number_to_msisdn(body['country'], body['phone_number'])

        if not (yield check_3pid_allowed(self.hs, "msisdn", msisdn)):
            raise SynapseError(
                403,
                "Phone numbers are not authorized to register on this server",
                Codes.THREEPID_DENIED,
            )

        existingUid = yield self.hs.get_datastore().get_user_id_by_threepid(
            'msisdn', msisdn
        )

        if existingUid is not None:
            raise SynapseError(
                400, "Phone number is already in use", Codes.THREEPID_IN_USE
            )

        ret = yield self.identity_handler.requestMsisdnToken(**body)
        defer.returnValue((200, ret))


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
            )
        )

    @defer.inlineCallbacks
    def on_GET(self, request):
        ip = self.hs.get_ip_from_request(request)
        with self.ratelimiter.ratelimit(ip) as wait_deferred:
            yield wait_deferred

            username = parse_string(request, "username", required=True)

            yield self.registration_handler.check_username(username)

            defer.returnValue((200, {"available": True}))


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

    @interactive_auth_handler
    @defer.inlineCallbacks
    def on_POST(self, request):
        body = parse_json_object_from_request(request)

        client_addr = request.getClientIP()

        time_now = self.clock.time()

        allowed, time_allowed = self.ratelimiter.can_do_action(
            client_addr, time_now_s=time_now,
            rate_hz=self.hs.config.rc_registration.per_second,
            burst_count=self.hs.config.rc_registration.burst_count,
            update=False,
        )

        if not allowed:
            raise LimitExceededError(
                retry_after_ms=int(1000 * (time_allowed - time_now)),
            )

        kind = b"user"
        if b"kind" in request.args:
            kind = request.args[b"kind"][0]

        if kind == b"guest":
            ret = yield self._do_guest_registration(body, address=client_addr)
            defer.returnValue(ret)
            return
        elif kind != b"user":
            raise UnrecognizedRequestError(
                "Do not understand membership kind: %s" % (kind,)
            )

        # we do basic sanity checks here because the auth layer will store these
        # in sessions. Pull out the username/password provided to us.
        desired_password = None
        if 'password' in body:
            if (not isinstance(body['password'], string_types) or
                    len(body['password']) > 512):
                raise SynapseError(400, "Invalid password")
            self.password_policy_handler.validate_password(body['password'])
            desired_password = body["password"]

        desired_username = None
        if 'username' in body:
            if (not isinstance(body['username'], string_types) or
                    len(body['username']) > 512):
                raise SynapseError(400, "Invalid username")
            desired_username = body['username']

        desired_display_name = body.get('display_name')

        appservice = None
        if self.auth.has_access_token(request):
            appservice = yield self.auth.get_appservice_by_req(request)

        # fork off as soon as possible for ASes and shared secret auth which
        # have completely different registration flows to normal users

        # == Application Service Registration ==
        if appservice:
            # Set the desired user according to the AS API (which uses the
            # 'user' key not 'username'). Since this is a new addition, we'll
            # fallback to 'username' if they gave one.
            desired_username = body.get("user", desired_username)

            # XXX we should check that desired_username is valid. Currently
            # we give appservices carte blanche for any insanity in mxids,
            # because the IRC bridges rely on being able to register stupid
            # IDs.

            access_token = self.auth.get_access_token_from_request(request)

            if isinstance(desired_username, string_types):
                result = yield self._do_appservice_registration(
                    desired_username, desired_password, desired_display_name,
                    access_token, body
                )
            defer.returnValue((200, result))  # we throw for non 200 responses
            return

        # for either shared secret or regular registration, downcase the
        # provided username before attempting to register it. This should mean
        # that people who try to register with upper-case in their usernames
        # don't get a nasty surprise. (Note that we treat username
        # case-insenstively in login, so they are free to carry on imagining
        # that their username is CrAzYh4cKeR if that keeps them happy)
        if desired_username is not None:
            desired_username = desired_username.lower()

        # == Shared Secret Registration == (e.g. create new user scripts)
        if 'mac' in body:
            # FIXME: Should we really be determining if this is shared secret
            # auth based purely on the 'mac' key?
            result = yield self._do_shared_secret_registration(
                desired_username, desired_password, body
            )
            defer.returnValue((200, result))  # we throw for non 200 responses
            return

        # == Normal User Registration == (everyone else)
        if not self.hs.config.enable_registration:
            raise SynapseError(403, "Registration has been disabled")

        guest_access_token = body.get("guest_access_token", None)

        if (
            'initial_device_display_name' in body and
            'password' not in body
        ):
            # ignore 'initial_device_display_name' if sent without
            # a password to work around a client bug where it sent
            # the 'initial_device_display_name' param alone, wiping out
            # the original registration params
            logger.warn("Ignoring initial_device_display_name without password")
            del body['initial_device_display_name']

        session_id = self.auth_handler.get_session_id(body)
        registered_user_id = None
        if session_id:
            # if we get a registered user id out of here, it means we previously
            # registered a user for this session, so we could just return the
            # user here. We carry on and go through the auth checks though,
            # for paranoia.
            registered_user_id = self.auth_handler.get_session_data(
                session_id, "registered_user_id", None
            )

        if desired_username is not None:
            yield self.registration_handler.check_username(
                desired_username,
                guest_access_token=guest_access_token,
                assigned_user_id=registered_user_id,
            )

        # FIXME: need a better error than "no auth flow found" for scenarios
        # where we required 3PID for registration but the user didn't give one
        require_email = 'email' in self.hs.config.registrations_require_3pid
        require_msisdn = 'msisdn' in self.hs.config.registrations_require_3pid

        show_msisdn = True
        if self.hs.config.disable_msisdn_registration:
            show_msisdn = False
            require_msisdn = False

        flows = []
        if self.hs.config.enable_registration_captcha:
            # only support 3PIDless registration if no 3PIDs are required
            if not require_email and not require_msisdn:
                # Also add a dummy flow here, otherwise if a client completes
                # recaptcha first we'll assume they were going for this flow
                # and complete the request, when they could have been trying to
                # complete one of the flows with email/msisdn auth.
                flows.extend([[LoginType.RECAPTCHA, LoginType.DUMMY]])
            # only support the email-only flow if we don't require MSISDN 3PIDs
            if not require_msisdn:
                flows.extend([[LoginType.RECAPTCHA, LoginType.EMAIL_IDENTITY]])

            if show_msisdn:
                # only support the MSISDN-only flow if we don't require email 3PIDs
                if not require_email:
                    flows.extend([[LoginType.RECAPTCHA, LoginType.MSISDN]])
                # always let users provide both MSISDN & email
                flows.extend([
                    [LoginType.RECAPTCHA, LoginType.MSISDN, LoginType.EMAIL_IDENTITY],
                ])
        else:
            # only support 3PIDless registration if no 3PIDs are required
            if not require_email and not require_msisdn:
                flows.extend([[LoginType.DUMMY]])
            # only support the email-only flow if we don't require MSISDN 3PIDs
            if not require_msisdn:
                flows.extend([[LoginType.EMAIL_IDENTITY]])

            if show_msisdn:
                # only support the MSISDN-only flow if we don't require email 3PIDs
                if not require_email or require_msisdn:
                    flows.extend([[LoginType.MSISDN]])
                # always let users provide both MSISDN & email
                flows.extend([
                    [LoginType.MSISDN, LoginType.EMAIL_IDENTITY]
                ])

        # Append m.login.terms to all flows if we're requiring consent
        if self.hs.config.user_consent_at_registration:
            new_flows = []
            for flow in flows:
                inserted = False
                # m.login.terms should go near the end but before msisdn or email auth
                for i, stage in enumerate(flow):
                    if stage == LoginType.EMAIL_IDENTITY or stage == LoginType.MSISDN:
                        flow.insert(i, LoginType.TERMS)
                        inserted = True
                        break
                if not inserted:
                    flow.append(LoginType.TERMS)
            flows.extend(new_flows)

        auth_result, params, session_id = yield self.auth_handler.check_auth(
            flows, body, self.hs.get_ip_from_request(request)
        )

        # Check that we're not trying to register a denied 3pid.
        #
        # the user-facing checks will probably already have happened in
        # /register/email/requestToken when we requested a 3pid, but that's not
        # guaranteed.

        if auth_result:
            for login_type in [LoginType.EMAIL_IDENTITY, LoginType.MSISDN]:
                if login_type in auth_result:
                    medium = auth_result[login_type]['medium']
                    address = auth_result[login_type]['address']

                    if not (yield check_3pid_allowed(self.hs, medium, address)):
                        raise SynapseError(
                            403,
                            "Third party identifiers (email/phone numbers)" +
                            " are not authorized on this server",
                            Codes.THREEPID_DENIED,
                        )

                    existingUid = yield self.store.get_user_id_by_threepid(
                        medium, address,
                    )

                    if existingUid is not None:
                        raise SynapseError(
                            400,
                            "%s is already in use" % medium,
                            Codes.THREEPID_IN_USE,
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
                    self.hs.config.register_mxid_from_3pid == 'email' and
                    LoginType.EMAIL_IDENTITY in auth_result
                ):
                    address = auth_result[LoginType.EMAIL_IDENTITY]['address']
                    desired_username = synapse.types.strip_invalid_mxid_characters(
                        address.replace('@', '-').lower()
                    )

                    # find a unique mxid for the account, suffixing numbers
                    # if needed
                    while True:
                        try:
                            yield self.registration_handler.check_username(
                                desired_username,
                                guest_access_token=guest_access_token,
                                assigned_user_id=registered_user_id,
                            )
                            # if we got this far we passed the check.
                            break
                        except SynapseError as e:
                            if e.errcode == Codes.USER_IN_USE:
                                m = re.match(r'^(.*?)(\d+)$', desired_username)
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
                        desired_display_name = self._map_email_to_displayname(address)
                elif (
                    self.hs.config.register_mxid_from_3pid == 'msisdn' and
                    LoginType.MSISDN in auth_result
                ):
                    desired_username = auth_result[LoginType.MSISDN]['address']
                else:
                    raise SynapseError(
                        400, "Cannot derive mxid from 3pid; no recognised 3pid"
                    )

        if desired_username is not None:
            yield self.registration_handler.check_username(
                desired_username,
                guest_access_token=guest_access_token,
                assigned_user_id=registered_user_id,
            )

        if registered_user_id is not None:
            logger.info(
                "Already registered user ID %r for this session",
                registered_user_id
            )
            # don't re-register the threepids
            registered = False
        else:
            # NB: This may be from the auth handler and NOT from the POST
            assert_params_in_dict(params, ["password"])

            if not self.hs.config.register_mxid_from_3pid:
                desired_username = params.get("username", None)
            else:
                # we keep the original desired_username derived from the 3pid above
                pass

            guest_access_token = params.get("guest_access_token", None)

            # XXX: don't we need to validate these for length etc like we did on
            # the ones from the JSON body earlier on in the method?

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
                        medium = auth_result[login_type]['medium']
                        address = auth_result[login_type]['address']

                        existingUid = yield self.store.get_user_id_by_threepid(
                            medium, address,
                        )

                        if existingUid is not None:
                            raise SynapseError(
                                400,
                                "%s is already in use" % medium,
                                Codes.THREEPID_IN_USE,
                            )

            (registered_user_id, _) = yield self.registration_handler.register(
                localpart=desired_username,
                password=params.get("password", None),
                guest_access_token=guest_access_token,
                generate_token=False,
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
                    yield self.store.upsert_monthly_active_user(registered_user_id)

            if self.hs.config.shadow_server:
                yield self.registration_handler.shadow_register(
                    localpart=desired_username,
                    display_name=desired_display_name,
                    auth_result=auth_result,
                    params=params,
                )

            # remember that we've now registered that user account, and with
            #  what user ID (since the user may not have specified)
            self.auth_handler.set_session_data(
                session_id, "registered_user_id", registered_user_id
            )

            registered = True

        return_dict = yield self._create_registration_details(
            registered_user_id, params
        )

        if registered:
            yield self.registration_handler.post_registration_actions(
                user_id=registered_user_id,
                auth_result=auth_result,
                access_token=return_dict.get("access_token"),
                bind_email=params.get("bind_email"),
                bind_msisdn=params.get("bind_msisdn"),
            )

        defer.returnValue((200, return_dict))

    def on_OPTIONS(self, _):
        return 200, {}

    @defer.inlineCallbacks
    def _do_appservice_registration(
        self, username, password, display_name, as_token, body
    ):

        # FIXME: appservice_register() is horribly duplicated with register()
        # and they should probably just be combined together with a config flag.
        user_id = yield self.registration_handler.appservice_register(
            username, as_token, password, display_name
        )
        result = yield self._create_registration_details(user_id, body)

        auth_result = body.get('auth_result')
        if auth_result and LoginType.EMAIL_IDENTITY in auth_result:
            threepid = auth_result[LoginType.EMAIL_IDENTITY]
            yield self._register_email_threepid(
                user_id, threepid, result["access_token"],
                body.get("bind_email")
            )

        if auth_result and LoginType.MSISDN in auth_result:
            threepid = auth_result[LoginType.MSISDN]
            yield self._register_msisdn_threepid(
                user_id, threepid, result["access_token"],
                body.get("bind_msisdn")
            )

        defer.returnValue(result)

    @defer.inlineCallbacks
    def _do_shared_secret_registration(self, username, password, body):
        if not self.hs.config.registration_shared_secret:
            raise SynapseError(400, "Shared secret registration is not enabled")
        if not username:
            raise SynapseError(
                400, "username must be specified", errcode=Codes.BAD_JSON,
            )

        # use the username from the original request rather than the
        # downcased one in `username` for the mac calculation
        user = body["username"].encode("utf-8")

        # str() because otherwise hmac complains that 'unicode' does not
        # have the buffer interface
        got_mac = str(body["mac"])

        # FIXME this is different to the /v1/register endpoint, which
        # includes the password and admin flag in the hashed text. Why are
        # these different?
        want_mac = hmac.new(
            key=self.hs.config.registration_shared_secret.encode(),
            msg=user,
            digestmod=sha1,
        ).hexdigest()

        if not compare_digest(want_mac, got_mac):
            raise SynapseError(
                403, "HMAC incorrect",
            )

        (user_id, _) = yield self.registration_handler.register(
            localpart=username, password=password, generate_token=False,
        )

        result = yield self._create_registration_details(user_id, body)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def _create_registration_details(self, user_id, params):
        """Complete registration of newly-registered user

        Allocates device_id if one was not given; also creates access_token.

        Args:
            (str) user_id: full canonical @user:id
            (object) params: registration parameters, from which we pull
                device_id, initial_device_name and inhibit_login
        Returns:
            defer.Deferred: (object) dictionary for response from /register
        """
        result = {
            "user_id": user_id,
            "home_server": self.hs.hostname,
        }
        if not params.get("inhibit_login", False):
            device_id = params.get("device_id")
            initial_display_name = params.get("initial_device_display_name")
            device_id, access_token = yield self.registration_handler.register_device(
                user_id, device_id, initial_display_name, is_guest=False,
            )

            result.update({
                "access_token": access_token,
                "device_id": device_id,
            })
        defer.returnValue(result)

    @defer.inlineCallbacks
    def _do_guest_registration(self, params, address=None):
        if not self.hs.config.allow_guest_access:
            raise SynapseError(403, "Guest access is disabled")
        user_id, _ = yield self.registration_handler.register(
            generate_token=False,
            make_guest=True,
            address=address,
        )

        # we don't allow guests to specify their own device_id, because
        # we have nowhere to store it.
        device_id = synapse.api.auth.GUEST_DEVICE_ID
        initial_display_name = params.get("initial_device_display_name")
        device_id, access_token = yield self.registration_handler.register_device(
            user_id, device_id, initial_display_name, is_guest=True,
        )

        defer.returnValue((200, {
            "user_id": user_id,
            "device_id": device_id,
            "access_token": access_token,
            "home_server": self.hs.hostname,
        }))


def _map_email_to_displayname(address):
    """Custom mapping from an email address to a user displayname

    Args:
        address (str): The email address to process
    Returns:
        str: The new displayname
    """
    # Split the part before and after the @ in the email.
    # Replace all . with spaces in the first part
    parts = address.replace('.', ' ').split('@')

    # Figure out which org this email address belongs to
    org_parts = parts[1].split(' ')

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

    def cap(s):
        """Capitalise words in a string, including examples such as
        'John-Doe'"""
        if not s:
            return s

        # Convert str to a list so that we can edit each character
        s = list(s)

        # Capatilise the first letter
        s[0] = s[0].capitalize()

        s_len = len(s)
        for i in range(s_len):
            if (s[i] == " " or s[i] == "-") and i < s_len - 1:
                s[i + 1] = s[i + 1].capitalize()

        # Convert list back to a str
        return ''.join(s)

    desired_display_name = (
        cap(parts[0]) + " [" + cap(org) + "]"
    )

    return desired_display_name


def register_servlets(hs, http_server):
    EmailRegisterRequestTokenRestServlet(hs).register(http_server)
    MsisdnRegisterRequestTokenRestServlet(hs).register(http_server)
    UsernameAvailabilityRestServlet(hs).register(http_server)
    RegisterRestServlet(hs).register(http_server)

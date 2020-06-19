# -*- coding: utf-8 -*-
# Copyright 2014 - 2016 OpenMarket Ltd
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

"""Contains functions for registering clients."""
import logging

from twisted.internet import defer

from synapse import types
from synapse.api.constants import MAX_USERID_LENGTH, LoginType
from synapse.api.errors import AuthError, Codes, ConsentNotGivenError, SynapseError
from synapse.config.server import is_threepid_reserved
from synapse.http.servlet import assert_params_in_dict
from synapse.replication.http.login import RegisterDeviceReplicationServlet
from synapse.replication.http.register import (
    ReplicationPostRegisterActionsServlet,
    ReplicationRegisterServlet,
)
from synapse.types import RoomAlias, RoomID, UserID, create_requester
from synapse.util.async_helpers import Linearizer

from ._base import BaseHandler

logger = logging.getLogger(__name__)


class RegistrationHandler(BaseHandler):
    def __init__(self, hs):
        """

        Args:
            hs (synapse.server.HomeServer):
        """
        super(RegistrationHandler, self).__init__(hs)
        self.hs = hs
        self.auth = hs.get_auth()
        self._auth_handler = hs.get_auth_handler()
        self.profile_handler = hs.get_profile_handler()
        self.user_directory_handler = hs.get_user_directory_handler()
        self.http_client = hs.get_simple_http_client()
        self.identity_handler = self.hs.get_handlers().identity_handler
        self.ratelimiter = hs.get_registration_ratelimiter()

        self._next_generated_user_id = None

        self.macaroon_gen = hs.get_macaroon_generator()

        self._generate_user_id_linearizer = Linearizer(
            name="_generate_user_id_linearizer"
        )
        self._server_notices_mxid = hs.config.server_notices_mxid

        self._show_in_user_directory = self.hs.config.show_users_in_user_directory

        if hs.config.worker_app:
            self._register_client = ReplicationRegisterServlet.make_client(hs)
            self._register_device_client = RegisterDeviceReplicationServlet.make_client(
                hs
            )
            self._post_registration_client = ReplicationPostRegisterActionsServlet.make_client(
                hs
            )
        else:
            self.device_handler = hs.get_device_handler()
            self.pusher_pool = hs.get_pusherpool()

        self.session_lifetime = hs.config.session_lifetime

    @defer.inlineCallbacks
    def check_username(
        self, localpart, guest_access_token=None, assigned_user_id=None,
    ):
        """

        Args:
            localpart (str|None): The user's localpart
            guest_access_token (str|None): A guest's access token
            assigned_user_id (str|None): An existing User ID for this user if pre-calculated

        Returns:
            Deferred
        """
        if types.contains_invalid_mxid_characters(localpart):
            raise SynapseError(
                400,
                "User ID can only contain characters a-z, 0-9, or '=_-./'",
                Codes.INVALID_USERNAME,
            )

        if not localpart:
            raise SynapseError(400, "User ID cannot be empty", Codes.INVALID_USERNAME)

        if localpart[0] == "_":
            raise SynapseError(
                400, "User ID may not begin with _", Codes.INVALID_USERNAME
            )

        user = UserID(localpart, self.hs.hostname)
        user_id = user.to_string()

        if assigned_user_id:
            if user_id == assigned_user_id:
                return
            else:
                raise SynapseError(
                    400,
                    "A different user ID has already been registered for this session",
                )

        self.check_user_id_not_appservice_exclusive(user_id)

        if len(user_id) > MAX_USERID_LENGTH:
            raise SynapseError(
                400,
                "User ID may not be longer than %s characters" % (MAX_USERID_LENGTH,),
                Codes.INVALID_USERNAME,
            )

        users = yield self.store.get_users_by_id_case_insensitive(user_id)
        if users:
            if not guest_access_token:
                # Note that we don't want to give this exception to any clients, as they
                # could use it to infer whether a user exists on a server or not
                raise SynapseError(
                    400, "User ID already taken.", errcode=Codes.USER_IN_USE
                )

            # Retrieve guest user information from provided access token
            user_data = yield self.auth.get_user_by_access_token(guest_access_token)
            if not user_data["is_guest"] or user_data["user"].localpart != localpart:
                raise AuthError(
                    403,
                    "Cannot register taken user ID without valid guest "
                    "credentials for that user.",
                    errcode=Codes.FORBIDDEN,
                )

    @defer.inlineCallbacks
    def register_user(
        self,
        localpart=None,
        password_hash=None,
        guest_access_token=None,
        make_guest=False,
        admin=False,
        threepid=None,
        user_type=None,
        default_display_name=None,
        address=None,
        bind_emails=[],
    ):
        """Registers a new client on the server.

        Args:
            localpart: The local part of the user ID to register. If None,
              one will be generated.
            password_hash (str|None): The hashed password to assign to this user so they can
              login again. This can be None which means they cannot login again
              via a password (e.g. the user is an application service user).
            user_type (str|None): type of user. One of the values from
              api.constants.UserTypes, or None for a normal user.
            default_display_name (unicode|None): if set, the new user's displayname
              will be set to this. Defaults to 'localpart'.
            address (str|None): the IP address used to perform the registration.
            bind_emails (List[str]): list of emails to bind to this account.
        Returns:
            Deferred[str]: user_id
        Raises:
            SynapseError if there was a problem registering.
        """
        yield self.check_registration_ratelimit(address)

        yield self.auth.check_auth_blocking(threepid=threepid)

        if localpart is not None:
            yield self.check_username(localpart, guest_access_token=guest_access_token)

            was_guest = guest_access_token is not None

            if not was_guest:
                try:
                    int(localpart)
                    raise SynapseError(
                        400, "Numeric user IDs are reserved for guest users."
                    )
                except ValueError:
                    pass

            user = UserID(localpart, self.hs.hostname)
            user_id = user.to_string()

            if was_guest:
                # If the user was a guest then they already have a profile
                default_display_name = None

            elif default_display_name is None:
                default_display_name = localpart

            yield self.register_with_store(
                user_id=user_id,
                password_hash=password_hash,
                was_guest=was_guest,
                make_guest=make_guest,
                create_profile_with_displayname=default_display_name,
                admin=admin,
                user_type=user_type,
                address=address,
            )

            if default_display_name:
                yield defer.ensureDeferred(
                    self.profile_handler.set_displayname(
                        user, None, default_display_name, by_admin=True
                    )
                )

            if self.hs.config.user_directory_search_all_users:
                profile = yield self.store.get_profileinfo(localpart)
                yield self.user_directory_handler.handle_local_profile_change(
                    user_id, profile
                )

        else:
            # autogen a sequential user ID
            fail_count = 0
            user = None
            while not user:
                # Fail after being unable to find a suitable ID a few times
                if fail_count > 10:
                    raise SynapseError(500, "Unable to find a suitable guest user ID")

                localpart = yield self._generate_user_id()
                user = UserID(localpart, self.hs.hostname)
                user_id = user.to_string()
                yield self.check_user_id_not_appservice_exclusive(user_id)
                if default_display_name is None:
                    default_display_name = localpart
                try:
                    yield self.register_with_store(
                        user_id=user_id,
                        password_hash=password_hash,
                        make_guest=make_guest,
                        create_profile_with_displayname=default_display_name,
                        address=address,
                    )

                    yield defer.ensureDeferred(
                        self.profile_handler.set_displayname(
                            user, None, default_display_name, by_admin=True
                        )
                    )

                    # Successfully registered
                    break
                except SynapseError:
                    # if user id is taken, just generate another
                    user = None
                    user_id = None
                    fail_count += 1

        if not self.hs.config.user_consent_at_registration:
            yield defer.ensureDeferred(self._auto_join_rooms(user_id))
        else:
            logger.info(
                "Skipping auto-join for %s because consent is required at registration",
                user_id,
            )

        # Bind any specified emails to this account
        current_time = self.hs.get_clock().time_msec()
        for email in bind_emails:
            # generate threepid dict
            threepid_dict = {
                "medium": "email",
                "address": email,
                "validated_at": current_time,
            }

            # Bind email to new account
            yield self.register_email_threepid(user_id, threepid_dict, None)

        # Prevent the new user from showing up in the user directory if the server
        # mandates it.
        if not self._show_in_user_directory:
            yield self.store.add_account_data_for_user(
                user_id, "im.vector.hide_profile", {"hide_profile": True}
            )
            yield self.profile_handler.set_active(user, False, True)

        return user_id

    async def _auto_join_rooms(self, user_id):
        """Automatically joins users to auto join rooms - creating the room in the first place
        if the user is the first to be created.

        Args:
            user_id(str): The user to join
        """
        # auto-join the user to any rooms we're supposed to dump them into
        fake_requester = create_requester(user_id)

        # try to create the room if we're the first real user on the server. Note
        # that an auto-generated support or bot user is not a real user and will never be
        # the user to create the room
        should_auto_create_rooms = False
        is_real_user = await self.store.is_real_user(user_id)
        if self.hs.config.autocreate_auto_join_rooms and is_real_user:
            count = await self.store.count_real_users()
            should_auto_create_rooms = count == 1
        for r in self.hs.config.auto_join_rooms:
            logger.info("Auto-joining %s to %s", user_id, r)
            try:
                if should_auto_create_rooms:
                    room_alias = RoomAlias.from_string(r)
                    if self.hs.hostname != room_alias.domain:
                        logger.warning(
                            "Cannot create room alias %s, "
                            "it does not match server domain",
                            r,
                        )
                    else:
                        # create room expects the localpart of the room alias
                        room_alias_localpart = room_alias.localpart

                        # getting the RoomCreationHandler during init gives a dependency
                        # loop
                        await self.hs.get_room_creation_handler().create_room(
                            fake_requester,
                            config={
                                "preset": "public_chat",
                                "room_alias_name": room_alias_localpart,
                            },
                            ratelimit=False,
                        )
                else:
                    await self._join_user_to_room(fake_requester, r)
            except ConsentNotGivenError as e:
                # Technically not necessary to pull out this error though
                # moving away from bare excepts is a good thing to do.
                logger.error("Failed to join new user to %r: %r", r, e)
            except Exception as e:
                logger.error("Failed to join new user to %r: %r", r, e)

    async def post_consent_actions(self, user_id):
        """A series of registration actions that can only be carried out once consent
        has been granted

        Args:
            user_id (str): The user to join
        """
        await self._auto_join_rooms(user_id)

    @defer.inlineCallbacks
    def appservice_register(
        self, user_localpart, as_token, password_hash, display_name
    ):
        # FIXME: this should be factored out and merged with normal register()

        user = UserID(user_localpart, self.hs.hostname)
        user_id = user.to_string()
        service = self.store.get_app_service_by_token(as_token)
        if not service:
            raise AuthError(403, "Invalid application service token.")
        if not service.is_interested_in_user(user_id):
            raise SynapseError(
                400,
                "Invalid user localpart for this application service.",
                errcode=Codes.EXCLUSIVE,
            )

        service_id = service.id if service.is_exclusive_user(user_id) else None

        yield self.check_user_id_not_appservice_exclusive(
            user_id, allowed_appservice=service
        )

        display_name = display_name or user.localpart

        yield self.register_with_store(
            user_id=user_id,
            password_hash=password_hash,
            appservice_id=service_id,
            create_profile_with_displayname=display_name,
        )

        yield defer.ensureDeferred(
            self.profile_handler.set_displayname(
                user, None, display_name, by_admin=True
            )
        )

        if self.hs.config.user_directory_search_all_users:
            profile = yield self.store.get_profileinfo(user_localpart)
            yield self.user_directory_handler.handle_local_profile_change(
                user_id, profile
            )

        return user_id

    def check_user_id_not_appservice_exclusive(self, user_id, allowed_appservice=None):
        # don't allow people to register the server notices mxid
        if self._server_notices_mxid is not None:
            if user_id == self._server_notices_mxid:
                raise SynapseError(
                    400, "This user ID is reserved.", errcode=Codes.EXCLUSIVE
                )

        # valid user IDs must not clash with any user ID namespaces claimed by
        # application services.
        services = self.store.get_app_services()
        interested_services = [
            s
            for s in services
            if s.is_interested_in_user(user_id) and s != allowed_appservice
        ]
        for service in interested_services:
            if service.is_exclusive_user(user_id):
                raise SynapseError(
                    400,
                    "This user ID is reserved by an application service.",
                    errcode=Codes.EXCLUSIVE,
                )

    @defer.inlineCallbacks
    def shadow_register(self, localpart, display_name, auth_result, params):
        """Invokes the current registration on another server, using
        shared secret registration, passing in any auth_results from
        other registration UI auth flows (e.g. validated 3pids)
        Useful for setting up shadow/backup accounts on a parallel deployment.
        """

        # TODO: retries
        shadow_hs_url = self.hs.config.shadow_server.get("hs_url")
        as_token = self.hs.config.shadow_server.get("as_token")

        yield self.http_client.post_json_get_json(
            "%s/_matrix/client/r0/register?access_token=%s" % (shadow_hs_url, as_token),
            {
                # XXX: auth_result is an unspecified extension for shadow registration
                "auth_result": auth_result,
                # XXX: another unspecified extension for shadow registration to ensure
                # that the displayname is correctly set by the masters erver
                "display_name": display_name,
                "username": localpart,
                "password": params.get("password"),
                "bind_msisdn": params.get("bind_msisdn"),
                "device_id": params.get("device_id"),
                "initial_device_display_name": params.get(
                    "initial_device_display_name"
                ),
                "inhibit_login": False,
                "access_token": as_token,
            },
        )

    @defer.inlineCallbacks
    def _generate_user_id(self):
        if self._next_generated_user_id is None:
            with (yield self._generate_user_id_linearizer.queue(())):
                if self._next_generated_user_id is None:
                    self._next_generated_user_id = (
                        yield self.store.find_next_generated_user_id_localpart()
                    )

        id = self._next_generated_user_id
        self._next_generated_user_id += 1
        return str(id)

    async def _join_user_to_room(self, requester, room_identifier):
        room_member_handler = self.hs.get_room_member_handler()
        if RoomID.is_valid(room_identifier):
            room_id = room_identifier
        elif RoomAlias.is_valid(room_identifier):
            room_alias = RoomAlias.from_string(room_identifier)
            room_id, remote_room_hosts = await room_member_handler.lookup_room_alias(
                room_alias
            )
            room_id = room_id.to_string()
        else:
            raise SynapseError(
                400, "%s was not legal room ID or room alias" % (room_identifier,)
            )

        await room_member_handler.update_membership(
            requester=requester,
            target=requester.user,
            room_id=room_id,
            remote_room_hosts=remote_room_hosts,
            action="join",
            ratelimit=False,
        )

    def check_registration_ratelimit(self, address):
        """A simple helper method to check whether the registration rate limit has been hit
        for a given IP address

        Args:
            address (str|None): the IP address used to perform the registration. If this is
                None, no ratelimiting will be performed.

        Raises:
            LimitExceededError: If the rate limit has been exceeded.
        """
        if not address:
            return

        time_now = self.clock.time()

        self.ratelimiter.ratelimit(
            address,
            time_now_s=time_now,
            rate_hz=self.hs.config.rc_registration.per_second,
            burst_count=self.hs.config.rc_registration.burst_count,
        )

    def register_with_store(
        self,
        user_id,
        password_hash=None,
        was_guest=False,
        make_guest=False,
        appservice_id=None,
        create_profile_with_displayname=None,
        admin=False,
        user_type=None,
        address=None,
    ):
        """Register user in the datastore.

        Args:
            user_id (str): The desired user ID to register.
            password_hash (str|None): Optional. The password hash for this user.
            was_guest (bool): Optional. Whether this is a guest account being
                upgraded to a non-guest account.
            make_guest (boolean): True if the the new user should be guest,
                false to add a regular user account.
            appservice_id (str|None): The ID of the appservice registering the user.
            create_profile_with_displayname (unicode|None): Optionally create a
                profile for the user, setting their displayname to the given value
            admin (boolean): is an admin user?
            user_type (str|None): type of user. One of the values from
                api.constants.UserTypes, or None for a normal user.
            address (str|None): the IP address used to perform the registration.

        Returns:
            Deferred
        """
        if self.hs.config.worker_app:
            return self._register_client(
                user_id=user_id,
                password_hash=password_hash,
                was_guest=was_guest,
                make_guest=make_guest,
                appservice_id=appservice_id,
                create_profile_with_displayname=create_profile_with_displayname,
                admin=admin,
                user_type=user_type,
                address=address,
            )
        else:
            return self.store.register_user(
                user_id=user_id,
                password_hash=password_hash,
                was_guest=was_guest,
                make_guest=make_guest,
                appservice_id=appservice_id,
                create_profile_with_displayname=create_profile_with_displayname,
                admin=admin,
                user_type=user_type,
            )

    @defer.inlineCallbacks
    def register_device(self, user_id, device_id, initial_display_name, is_guest=False):
        """Register a device for a user and generate an access token.

        The access token will be limited by the homeserver's session_lifetime config.

        Args:
            user_id (str): full canonical @user:id
            device_id (str|None): The device ID to check, or None to generate
                a new one.
            initial_display_name (str|None): An optional display name for the
                device.
            is_guest (bool): Whether this is a guest account

        Returns:
            defer.Deferred[tuple[str, str]]: Tuple of device ID and access token
        """

        if self.hs.config.worker_app:
            r = yield self._register_device_client(
                user_id=user_id,
                device_id=device_id,
                initial_display_name=initial_display_name,
                is_guest=is_guest,
            )
            return r["device_id"], r["access_token"]

        valid_until_ms = None
        if self.session_lifetime is not None:
            if is_guest:
                raise Exception(
                    "session_lifetime is not currently implemented for guest access"
                )
            valid_until_ms = self.clock.time_msec() + self.session_lifetime

        device_id = yield self.device_handler.check_device_registered(
            user_id, device_id, initial_display_name
        )
        if is_guest:
            assert valid_until_ms is None
            access_token = self.macaroon_gen.generate_access_token(
                user_id, ["guest = true"]
            )
        else:
            access_token = yield defer.ensureDeferred(
                self._auth_handler.get_access_token_for_user_id(
                    user_id, device_id=device_id, valid_until_ms=valid_until_ms
                )
            )

        return (device_id, access_token)

    async def post_registration_actions(self, user_id, auth_result, access_token):
        """A user has completed registration

        Args:
            user_id (str): The user ID that consented
            auth_result (dict): The authenticated credentials of the newly
                registered user.
            access_token (str|None): The access token of the newly logged in
                device, or None if `inhibit_login` enabled.
        """
        if self.hs.config.worker_app:
            await self._post_registration_client(
                user_id=user_id, auth_result=auth_result, access_token=access_token
            )
            return

        if auth_result and LoginType.EMAIL_IDENTITY in auth_result:
            threepid = auth_result[LoginType.EMAIL_IDENTITY]
            # Necessary due to auth checks prior to the threepid being
            # written to the db
            if is_threepid_reserved(
                self.hs.config.mau_limits_reserved_threepids, threepid
            ):
                await self.store.upsert_monthly_active_user(user_id)

            await self.register_email_threepid(user_id, threepid, access_token)

            if self.hs.config.account_threepid_delegate_email:
                # Bind the 3PID to the identity server
                logger.debug(
                    "Binding email to %s on id_server %s",
                    user_id,
                    self.hs.config.account_threepid_delegate_email,
                )
                threepid_creds = threepid["threepid_creds"]

                # Remove the protocol scheme before handling to `bind_threepid`
                # `bind_threepid` will add https:// to it, so this restricts
                # account_threepid_delegate.email to https:// addresses only
                # We assume this is always the case for dinsic however.
                if self.hs.config.account_threepid_delegate_email.startswith(
                    "https://"
                ):
                    id_server = self.hs.config.account_threepid_delegate_email[8:]
                else:
                    # Must start with http:// instead
                    id_server = self.hs.config.account_threepid_delegate_email[7:]

                await self.identity_handler.bind_threepid(
                    threepid_creds["client_secret"],
                    threepid_creds["sid"],
                    user_id,
                    id_server,
                    threepid_creds.get("id_access_token"),
                )

        if auth_result and LoginType.MSISDN in auth_result:
            threepid = auth_result[LoginType.MSISDN]
            await self._register_msisdn_threepid(user_id, threepid)

        if auth_result and LoginType.TERMS in auth_result:
            await self._on_user_consented(user_id, self.hs.config.user_consent_version)

    async def _on_user_consented(self, user_id, consent_version):
        """A user consented to the terms on registration

        Args:
            user_id (str): The user ID that consented.
            consent_version (str): version of the policy the user has
                consented to.
        """
        logger.info("%s has consented to the privacy policy", user_id)
        await self.store.user_set_consent_version(user_id, consent_version)
        await self.post_consent_actions(user_id)

    @defer.inlineCallbacks
    def register_email_threepid(self, user_id, threepid, token):
        """Add an email address as a 3pid identifier

        Also adds an email pusher for the email address, if configured in the
        HS config

        Must be called on master.

        Args:
            user_id (str): id of user
            threepid (object): m.login.email.identity auth response
            token (str|None): access_token for the user, or None if not logged
                in.
        Returns:
            defer.Deferred:
        """
        reqd = ("medium", "address", "validated_at")
        if any(x not in threepid for x in reqd):
            # This will only happen if the ID server returns a malformed response
            logger.info("Can't add incomplete 3pid")
            return

        yield defer.ensureDeferred(
            self._auth_handler.add_threepid(
                user_id,
                threepid["medium"],
                threepid["address"],
                threepid["validated_at"],
            )
        )

        # And we add an email pusher for them by default, but only
        # if email notifications are enabled (so people don't start
        # getting mail spam where they weren't before if email
        # notifs are set up on a homeserver)
        if (
            self.hs.config.email_enable_notifs
            and self.hs.config.email_notif_for_new_users
            and token
        ):
            # Pull the ID of the access token back out of the db
            # It would really make more sense for this to be passed
            # up when the access token is saved, but that's quite an
            # invasive change I'd rather do separately.
            user_tuple = yield self.store.get_user_by_access_token(token)
            token_id = user_tuple["token_id"]

            yield self.pusher_pool.add_pusher(
                user_id=user_id,
                access_token=token_id,
                kind="email",
                app_id="m.email",
                app_display_name="Email Notifications",
                device_display_name=threepid["address"],
                pushkey=threepid["address"],
                lang=None,  # We don't know a user's language here
                data={},
            )

    @defer.inlineCallbacks
    def _register_msisdn_threepid(self, user_id, threepid):
        """Add a phone number as a 3pid identifier

        Must be called on master.

        Args:
            user_id (str): id of user
            threepid (object): m.login.msisdn auth response
        Returns:
            defer.Deferred:
        """
        try:
            assert_params_in_dict(threepid, ["medium", "address", "validated_at"])
        except SynapseError as ex:
            if ex.errcode == Codes.MISSING_PARAM:
                # This will only happen if the ID server returns a malformed response
                logger.info("Can't add incomplete 3pid")
                return None
            raise

        yield defer.ensureDeferred(
            self._auth_handler.add_threepid(
                user_id,
                threepid["medium"],
                threepid["address"],
                threepid["validated_at"],
            )
        )

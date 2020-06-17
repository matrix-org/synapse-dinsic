# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
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

import logging

from synapse.api.errors import AuthError, NotFoundError, SynapseError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.types import UserID

from ._base import client_patterns

logger = logging.getLogger(__name__)


class AccountDataServlet(RestServlet):
    """
    PUT /user/{user_id}/account_data/{account_dataType} HTTP/1.1
    GET /user/{user_id}/account_data/{account_dataType} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/account_data/(?P<account_data_type>[^/]*)"
    )

    def __init__(self, hs):
        super(AccountDataServlet, self).__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.notifier = hs.get_notifier()
        self._is_worker = hs.config.worker_app is not None
        self._profile_handler = hs.get_profile_handler()

    async def on_PUT(self, request, user_id, account_data_type):
        if self._is_worker:
            raise Exception("Cannot handle PUT /account_data on worker")

        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add account data for other users.")

        body = parse_json_object_from_request(request)

        if account_data_type == "im.vector.hide_profile":
            user = UserID.from_string(user_id)
            hide_profile = body.get("hide_profile")
            await self._profile_handler.set_active([user], not hide_profile, True)

        max_id = await self.store.add_account_data_for_user(
            user_id, account_data_type, body
        )

        self.notifier.on_new_event("account_data_key", max_id, users=[user_id])

        return 200, {}

    async def on_GET(self, request, user_id, account_data_type):
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot get account data for other users.")

        event = await self.store.get_global_account_data_by_type_for_user(
            account_data_type, user_id
        )

        if event is None:
            raise NotFoundError("Account data not found")

        return 200, event


class RoomAccountDataServlet(RestServlet):
    """
    PUT /user/{user_id}/rooms/{room_id}/account_data/{account_dataType} HTTP/1.1
    GET /user/{user_id}/rooms/{room_id}/account_data/{account_dataType} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)"
        "/rooms/(?P<room_id>[^/]*)"
        "/account_data/(?P<account_data_type>[^/]*)"
    )

    def __init__(self, hs):
        super(RoomAccountDataServlet, self).__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.notifier = hs.get_notifier()
        self._is_worker = hs.config.worker_app is not None

    async def on_PUT(self, request, user_id, room_id, account_data_type):
        if self._is_worker:
            raise Exception("Cannot handle PUT /account_data on worker")

        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add account data for other users.")

        body = parse_json_object_from_request(request)

        if account_data_type == "m.fully_read":
            raise SynapseError(
                405,
                "Cannot set m.fully_read through this API."
                " Use /rooms/!roomId:server.name/read_markers",
            )

        max_id = await self.store.add_account_data_to_room(
            user_id, room_id, account_data_type, body
        )

        self.notifier.on_new_event("account_data_key", max_id, users=[user_id])

        return 200, {}

    async def on_GET(self, request, user_id, room_id, account_data_type):
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot get account data for other users.")

        event = await self.store.get_account_data_for_room_and_type(
            user_id, room_id, account_data_type
        )

        if event is None:
            raise NotFoundError("Room account data not found")

        return 200, event


def register_servlets(hs, http_server):
    AccountDataServlet(hs).register(http_server)
    RoomAccountDataServlet(hs).register(http_server)

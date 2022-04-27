# Copyright 2018 New Vector Ltd
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
from http import HTTPStatus
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.handlers.presence import PresenceHandler
from synapse.rest.client import presence
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest
from tests.test_utils import make_awaitable


class PresenceTestCase(unittest.HomeserverTestCase):
    """Tests presence REST API."""

    user_id = "@sid:red"

    user = UserID.from_string(user_id)
    servlets = [presence.register_servlets]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:

        presence_handler = Mock(spec=PresenceHandler)
        presence_handler.set_state.return_value = make_awaitable(None)

        hs = self.setup_test_homeserver(
            "red",
            federation_http_client=None,
            federation_client=Mock(),
            presence_handler=presence_handler,
        )

        return hs

    def test_put_presence(self) -> None:
        """
        PUT to the status endpoint with use_presence enabled will call
        set_state on the presence handler.
        """
        self.hs.config.server.use_presence = True

        body = {"presence": "here", "status_msg": "beep boop"}
        channel = self.make_request(
            "PUT", "/presence/%s/status" % (self.user_id,), body
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(self.hs.get_presence_handler().set_state.call_count, 1)

    @unittest.override_config({"use_presence": False})
    def test_put_presence_disabled(self) -> None:
        """
        PUT to the status endpoint with use_presence disabled will NOT call
        set_state on the presence handler.
        """

        body = {"presence": "here", "status_msg": "beep boop"}
        channel = self.make_request(
            "PUT", "/presence/%s/status" % (self.user_id,), body
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(self.hs.get_presence_handler().set_state.call_count, 0)

# Copyright 2018 New Vector
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

from twisted.web.resource import Resource

from synapse.rest.well_known import well_known_resource

from tests import unittest


class WellKnownTests(unittest.HomeserverTestCase):
    def create_test_resource(self) -> Resource:
        # replace the JsonResource with a Resource wrapping the WellKnownResource
        res = Resource()
        res.putChild(b".well-known", well_known_resource(self.hs))
        return res

    @unittest.override_config(
        {
            "public_baseurl": "https://tesths",
            "default_identity_server": "https://testis",
        }
    )
    def test_client_well_known(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            channel.json_body,
            {
                "m.homeserver": {"base_url": "https://tesths/"},
                "m.identity_server": {"base_url": "https://testis"},
            },
        )

    @unittest.override_config(
        {
            "public_baseurl": None,
        }
    )
    def test_client_well_known_no_public_baseurl(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/client", shorthand=False
        )

        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)

    @unittest.override_config({"serve_server_wellknown": True})
    def test_server_well_known(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/server", shorthand=False
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            channel.json_body,
            {"m.server": "test:443"},
        )

    def test_server_well_known_disabled(self) -> None:
        channel = self.make_request(
            "GET", "/.well-known/matrix/server", shorthand=False
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)

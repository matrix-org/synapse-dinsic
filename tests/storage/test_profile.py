# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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
from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest


class ProfileStoreTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.u_frank = UserID.from_string("@frank:test")

    def test_displayname(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank.localpart))

        self.get_success(
            self.store.set_profile_displayname(self.u_frank.localpart, "Frank", 1)
        )

        self.assertEqual(
            "Frank",
            (
                self.get_success(
                    self.store.get_profile_displayname(self.u_frank.localpart)
                )
            ),
        )

        # test set to None
        self.get_success(
            self.store.set_profile_displayname(self.u_frank.localpart, None, 1)
        )

        self.assertIsNone(
            self.get_success(self.store.get_profile_displayname(self.u_frank.localpart))
        )

    def test_avatar_url(self) -> None:
        self.get_success(self.store.create_profile(self.u_frank.localpart))

        self.get_success(
            self.store.set_profile_avatar_url(
                self.u_frank.localpart, "http://my.site/here", 1
            )
        )

        self.assertEqual(
            "http://my.site/here",
            (
                self.get_success(
                    self.store.get_profile_avatar_url(self.u_frank.localpart)
                )
            ),
        )

        # test set to None
        self.get_success(
            self.store.set_profile_avatar_url(self.u_frank.localpart, None, 1)
        )

        self.assertIsNone(
            self.get_success(self.store.get_profile_avatar_url(self.u_frank.localpart))
        )

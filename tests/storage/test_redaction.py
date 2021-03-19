# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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


from mock import Mock

from canonicaljson import json

from twisted.internet import defer

from synapse.api.constants import EventTypes, Membership
from synapse.api.room_versions import RoomVersions
from synapse.types import RoomID, UserID

from tests import unittest
from tests.utils import create_room


class RedactionTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor, clock):
        config = self.default_config()
        config["redaction_retention_period"] = "30d"
        return self.setup_test_homeserver(
            resource_for_federation=Mock(), federation_http_client=None, config=config
        )

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()
        self.storage = hs.get_storage()
        self.event_builder_factory = hs.get_event_builder_factory()
        self.event_creation_handler = hs.get_event_creation_handler()

        self.u_alice = UserID.from_string("@alice:test")
        self.u_bob = UserID.from_string("@bob:test")

        self.room1 = RoomID.from_string("!abc123:test")

        self.get_success(
            create_room(hs, self.room1.to_string(), self.u_alice.to_string())
        )

        self.depth = 1

    def inject_room_member(
        self, room, user, membership, replaces_state=None, extra_content={}
    ):
        content = {"membership": membership}
        content.update(extra_content)
        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Member,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": content,
            },
        )

        event, context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        self.get_success(self.storage.persistence.persist_event(event, context))

        return event

    def inject_message(self, room, user, body):
        self.depth += 1

        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Message,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": {"body": body, "msgtype": "message"},
            },
        )

        event, context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        self.get_success(self.storage.persistence.persist_event(event, context))

        return event

    def inject_redaction(self, room, event_id, user, reason):
        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Redaction,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": {"reason": reason},
                "redacts": event_id,
            },
        )

        event, context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        self.get_success(self.storage.persistence.persist_event(event, context))

        return event

    def test_redact(self):
        self.get_success(
            self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)
        )

        msg_event = self.get_success(self.inject_message(self.room1, self.u_alice, "t"))

        # Check event has not been redacted:
        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {"body": "t", "msgtype": "message"},
            },
            event,
        )

        self.assertFalse("redacted_because" in event.unsigned)

        # Redact event
        reason = "Because I said so"
        self.get_success(
            self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)
        )

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertEqual(msg_event.event_id, event.event_id)

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {},
            },
            event,
        )

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Redaction,
                "user_id": self.u_alice.to_string(),
                "content": {"reason": reason},
            },
            event.unsigned["redacted_because"],
        )

    def test_redact_join(self):
        self.get_success(
            self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)
        )

        msg_event = self.get_success(
            self.inject_room_member(
                self.room1, self.u_bob, Membership.JOIN, extra_content={"blue": "red"}
            )
        )

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Member,
                "user_id": self.u_bob.to_string(),
                "content": {"membership": Membership.JOIN, "blue": "red"},
            },
            event,
        )

        self.assertFalse(hasattr(event, "redacted_because"))

        # Redact event
        reason = "Because I said so"
        self.get_success(
            self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)
        )

        # Check redaction

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Member,
                "user_id": self.u_bob.to_string(),
                "content": {"membership": Membership.JOIN},
            },
            event,
        )

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Redaction,
                "user_id": self.u_alice.to_string(),
                "content": {"reason": reason},
            },
            event.unsigned["redacted_because"],
        )

    def test_circular_redaction(self):
        redaction_event_id1 = "$redaction1_id:test"
        redaction_event_id2 = "$redaction2_id:test"

        class EventIdManglingBuilder:
            def __init__(self, base_builder, event_id):
                self._base_builder = base_builder
                self._event_id = event_id

            @defer.inlineCallbacks
            def build(self, prev_event_ids, auth_event_ids):
                built_event = yield defer.ensureDeferred(
                    self._base_builder.build(prev_event_ids, auth_event_ids)
                )

                built_event._event_id = self._event_id
                built_event._dict["event_id"] = self._event_id
                assert built_event.event_id == self._event_id

                return built_event

            @property
            def room_id(self):
                return self._base_builder.room_id

            @property
            def type(self):
                return self._base_builder.type

        event_1, context_1 = self.get_success(
            self.event_creation_handler.create_new_client_event(
                EventIdManglingBuilder(
                    self.event_builder_factory.for_room_version(
                        RoomVersions.V1,
                        {
                            "type": EventTypes.Redaction,
                            "sender": self.u_alice.to_string(),
                            "room_id": self.room1.to_string(),
                            "content": {"reason": "test"},
                            "redacts": redaction_event_id2,
                        },
                    ),
                    redaction_event_id1,
                )
            )
        )

        self.get_success(self.storage.persistence.persist_event(event_1, context_1))

        event_2, context_2 = self.get_success(
            self.event_creation_handler.create_new_client_event(
                EventIdManglingBuilder(
                    self.event_builder_factory.for_room_version(
                        RoomVersions.V1,
                        {
                            "type": EventTypes.Redaction,
                            "sender": self.u_alice.to_string(),
                            "room_id": self.room1.to_string(),
                            "content": {"reason": "test"},
                            "redacts": redaction_event_id1,
                        },
                    ),
                    redaction_event_id2,
                )
            )
        )
        self.get_success(self.storage.persistence.persist_event(event_2, context_2))

        # fetch one of the redactions
        fetched = self.get_success(self.store.get_event(redaction_event_id1))

        # it should have been redacted
        self.assertEqual(fetched.unsigned["redacted_by"], redaction_event_id2)
        self.assertEqual(
            fetched.unsigned["redacted_because"].event_id, redaction_event_id2
        )

    def test_redact_censor(self):
        """Test that a redacted event gets censored in the DB after a month
        """

        self.get_success(
            self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)
        )

        msg_event = self.get_success(self.inject_message(self.room1, self.u_alice, "t"))

        # Check event has not been redacted:
        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {"body": "t", "msgtype": "message"},
            },
            event,
        )

        self.assertFalse("redacted_because" in event.unsigned)

        # Redact event
        reason = "Because I said so"
        self.get_success(
            self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)
        )

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {},
            },
            event,
        )

        event_json = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="event_json",
                keyvalues={"event_id": msg_event.event_id},
                retcol="json",
            )
        )

        self.assert_dict(
            {"content": {"body": "t", "msgtype": "message"}}, json.loads(event_json)
        )

        # Advance by 30 days, then advance again to ensure that the looping call
        # for updating the stream position gets called and then the looping call
        # for the censoring gets called.
        self.reactor.advance(60 * 60 * 24 * 31)
        self.reactor.advance(60 * 60 * 2)

        event_json = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="event_json",
                keyvalues={"event_id": msg_event.event_id},
                retcol="json",
            )
        )

        self.assert_dict({"content": {}}, json.loads(event_json))

    def test_redact_redaction(self):
        """Tests that we can redact a redaction and can fetch it again.
        """

        self.get_success(
            self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)
        )

        msg_event = self.get_success(self.inject_message(self.room1, self.u_alice, "t"))

        first_redact_event = self.get_success(
            self.inject_redaction(
                self.room1, msg_event.event_id, self.u_alice, "Redacting message"
            )
        )

        self.get_success(
            self.inject_redaction(
                self.room1,
                first_redact_event.event_id,
                self.u_alice,
                "Redacting redaction",
            )
        )

        # Now lets jump to the future where we have censored the redaction event
        # in the DB.
        self.reactor.advance(60 * 60 * 24 * 31)

        # We just want to check that fetching the event doesn't raise an exception.
        self.get_success(
            self.store.get_event(first_redact_event.event_id, allow_none=True)
        )

    def test_store_redacted_redaction(self):
        """Tests that we can store a redacted redaction.
        """

        self.get_success(
            self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)
        )

        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Redaction,
                "sender": self.u_alice.to_string(),
                "room_id": self.room1.to_string(),
                "content": {"reason": "foo"},
            },
        )

        redaction_event, context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        self.get_success(
            self.storage.persistence.persist_event(redaction_event, context)
        )

        # Now lets jump to the future where we have censored the redaction event
        # in the DB.
        self.reactor.advance(60 * 60 * 24 * 31)

        # We just want to check that fetching the event doesn't raise an exception.
        self.get_success(
            self.store.get_event(redaction_event.event_id, allow_none=True)
        )

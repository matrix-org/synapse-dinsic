# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from synapse.api.constants import EventTypes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions

from ._base import Config

# The default types of room state to send to users to are invited to or knock on a room.
DEFAULT_ROOM_STATE_TYPES = [
    EventTypes.JoinRules,
    EventTypes.CanonicalAlias,
    EventTypes.RoomAvatar,
    EventTypes.RoomEncryption,
    EventTypes.Name,
]


class ApiConfig(Config):
    section = "api"

    def read_config(self, config, **kwargs):
        self.room_invite_state_types = config.get(
            "room_invite_state_types", DEFAULT_ROOM_STATE_TYPES
        )
        msc2403_enabled = config.get("msc2403_enabled", False)
        if msc2403_enabled:
            # Enable the MSC2403 unstable room version
            KNOWN_ROOM_VERSIONS.update(
                {RoomVersions.MSC2403_DEV.identifier: RoomVersions.MSC2403_DEV}
            )

    def generate_config_section(cls, **kwargs):
        return """\
        ## API Configuration ##

        # A list of event types from a room that will be given to users when they
        # are invited to a room. This allows clients to display information about the
        # room that they've been invited to, without actually being in the room yet.
        #
        #room_invite_state_types:
        #  - "{JoinRules}"
        #  - "{CanonicalAlias}"
        #  - "{RoomAvatar}"
        #  - "{RoomEncryption}"
        #  - "{Name}"

        # Uncomment to enable experimental room knocking support as defined by
        # MSC2403.
        #
        # Note that the APIs used by this feature are unstable and will break in the
        # future.
        #
        #msc2403_enabled: true
        """.format(
            **vars(EventTypes)
        )

# -*- coding: utf-8 -*-
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config._base import Config
from synapse.types import JsonDict


class ExperimentalConfig(Config):
    """Config section for enabling experimental features"""

    section = "experimental"

    def read_config(self, config: JsonDict, **kwargs):
        experimental = config.get("experimental_features") or {}

        # MSC2403 (room knocking)
        self.msc2403_enabled = experimental.get("msc2403_enabled", False)  # type: bool
        if self.msc2403_enabled:
            # Enable the MSC2403 unstable room version
            KNOWN_ROOM_VERSIONS.update({RoomVersions.V7.identifier: RoomVersions.V7})

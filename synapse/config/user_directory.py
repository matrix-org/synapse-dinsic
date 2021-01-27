# -*- coding: utf-8 -*-
# Copyright 2017 New Vector Ltd
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
from synapse.util.module_loader import load_module

from ._base import Config


class UserDirectoryConfig(Config):
    """User Directory Configuration
    Configuration for the behaviour of the /user_directory API
    """

    section = "userdirectory"

    def read_config(self, config, **kwargs):
        self.user_directory_search_enabled = True
        self.user_directory_search_all_users = False
        self.user_directory_defer_to_id_server = None
        self.user_directory_search_module = None
        user_directory_config = config.get("user_directory") or {}
        if user_directory_config:
            self.user_directory_search_enabled = user_directory_config.get(
                "enabled", True
            )
            self.user_directory_search_all_users = user_directory_config.get(
                "search_all_users", False
            )
            self.user_directory_defer_to_id_server = user_directory_config.get(
                "defer_to_id_server", None
            )

            provider = user_directory_config.get("user_directory_search_module", None)
            if provider is not None:
                self.user_directory_search_module = load_module(provider)

    def generate_config_section(self, config_dir_path, server_name, **kwargs):
        return """
        # User Directory configuration
        #
        # 'enabled' defines whether users can search the user directory. If
        # false then empty responses are returned to all queries. Defaults to
        # true.
        #
        # 'search_all_users' defines whether to search all users visible to your HS
        # when searching the user directory, rather than limiting to users visible
        # in public rooms.  Defaults to false.  If you set it True, you'll have to
        # rebuild the user_directory search indexes, see
        # https://github.com/matrix-org/synapse/blob/master/docs/user_directory.md
        #
        #user_directory:
        #  enabled: true
        #  search_all_users: false
        #
        #  # If this is set, user search will be delegated to this ID server instead
        #  # of synapse performing the search itself.
        #  # This is an experimental API.
        #  defer_to_id_server: https://id.example.com
        #
        #  # Server admins can define a Python module that implements extra rules for
        #  # user directory search. In order to work, this module needs to
        #  # override the methods defined in
        #  # synapse/storage/database/main/user_directory_search_module.py.
        #  #
        #  custom_user_directory_search_module:
        #    module: "my_custom_module.UserDirectorySearch"
        #    config:
        #      example_option: 'things'
        """

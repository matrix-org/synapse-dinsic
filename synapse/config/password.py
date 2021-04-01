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

from ._base import Config


class PasswordConfig(Config):
    """Password login configuration
    """

    section = "password"

    def read_config(self, config, **kwargs):
        password_config = config.get("password_config", {})
        if password_config is None:
            password_config = {}

        self.password_enabled = password_config.get("enabled", True)
        self.password_localdb_enabled = password_config.get("localdb_enabled", True)
        self.password_pepper = password_config.get("pepper", "")

        # Password policy
        self.password_policy = password_config.get("policy") or {}
        self.password_policy_enabled = self.password_policy.get("enabled", False)

    def generate_config_section(self, config_dir_path, server_name, **kwargs):
        return """\
        password_config:
           # Uncomment to disable password login
           #
           #enabled: false

           # Uncomment to disable authentication against the local password
           # database. This is ignored if `enabled` is false, and is only useful
           # if you have other password_providers.
           #
           #localdb_enabled: false

           # Uncomment and change to a secret random string for extra security.
           # DO NOT CHANGE THIS AFTER INITIAL SETUP!
           #
           #pepper: "EVEN_MORE_SECRET"

           # Define and enforce a password policy. Each parameter is optional.
           # This is an implementation of MSC2000.
           #
           policy:
              # Whether to enforce the password policy.
              # Defaults to 'false'.
              #
              #enabled: true

              # Minimum accepted length for a password.
              # Defaults to 0.
              #
              #minimum_length: 15

              # Whether a password must contain at least one digit.
              # Defaults to 'false'.
              #
              #require_digit: true

              # Whether a password must contain at least one symbol.
              # A symbol is any character that's not a number or a letter.
              # Defaults to 'false'.
              #
              #require_symbol: true

              # Whether a password must contain at least one lowercase letter.
              # Defaults to 'false'.
              #
              #require_lowercase: true

              # Whether a password must contain at least one lowercase letter.
              # Defaults to 'false'.
              #
              #require_uppercase: true
        """

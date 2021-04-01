# -*- coding: utf-8 -*-
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
import os

from synapse.config._base import Config, ConfigError


class AccountValidityConfig(Config):
    section = "account_validity"

    def read_config(self, config, **kwargs):
        account_validity_config = config.get("account_validity") or {}
        self.account_validity_enabled = account_validity_config.get("enabled", False)
        self.account_validity_renew_by_email_enabled = (
            "renew_at" in account_validity_config
        )

        if self.account_validity_enabled:
            if "period" in account_validity_config:
                self.account_validity_period = self.parse_duration(
                    account_validity_config["period"]
                )
            else:
                raise ConfigError("'period' is required when using account validity")

            if "renew_at" in account_validity_config:
                self.account_validity_renew_at = self.parse_duration(
                    account_validity_config["renew_at"]
                )

            if "renew_email_subject" in account_validity_config:
                self.account_validity_renew_email_subject = account_validity_config[
                    "renew_email_subject"
                ]
            else:
                self.account_validity_renew_email_subject = "Renew your %(app)s account"

            self.account_validity_startup_job_max_delta = (
                self.account_validity_period * 10.0 / 100.0
            )

        if self.account_validity_renew_by_email_enabled:
            if not self.public_baseurl:
                raise ConfigError("Can't send renewal emails without 'public_baseurl'")

        # Load account validity templates.
        # We do this here instead of in AccountValidityConfig as read_templates
        # relies on state that hasn't been initialised in AccountValidityConfig
        account_renewed_template_filename = account_validity_config.get(
            "account_renewed_html_path", "account_renewed.html"
        )
        account_previously_renewed_template_filename = account_validity_config.get(
            "account_previously_renewed_html_path", "account_previously_renewed.html"
        )
        invalid_token_template_filename = account_validity_config.get(
            "invalid_token_html_path", "invalid_token.html"
        )
        custom_template_directory = account_validity_config.get("template_dir")

        (
            self.account_validity_account_renewed_template,
            self.account_validity_account_previously_renewed_template,
            self.account_validity_invalid_token_template,
        ) = self.read_templates(
            [
                account_renewed_template_filename,
                account_previously_renewed_template_filename,
                invalid_token_template_filename,
            ],
            custom_template_directory=custom_template_directory,
        )

        if "invalid_token_html_path" in config:
            file_path = os.path.join(self.default_template_dir, config["invalid_token_html_path"])

            self.invalid_token_html_content = self.read_file(
                file_path, "account_validity.invalid_token_html_path"
            )
        else:
            self.invalid_token_html_content = (
                "<html><body>Invalid renewal token.</body><html>"
            )

    def generate_config_section(self, **kwargs):
        return """\
        ## Account Validity ##
        #
        # Optional account validity configuration. This allows for accounts to be denied
        # any request after a given period.
        #
        # Once this feature is enabled, Synapse will look for registered users without an
        # expiration date at startup and will add one to every account it found using the
        # current settings at that time.
        # This means that, if a validity period is set, and Synapse is restarted (it will
        # then derive an expiration date from the current validity period), and some time
        # after that the validity period changes and Synapse is restarted, the users'
        # expiration dates won't be updated unless their account is manually renewed. This
        # date will be randomly selected within a range [now + period - d ; now + period],
        # where d is equal to 10% of the validity period.
        #
        account_validity:
          # The account validity feature is disabled by default. Uncomment the
          # following line to enable it.
          #
          #enabled: true

          # The period after which an account is valid after its registration. When
          # renewing the account, its validity period will be extended by this amount
          # of time. This parameter is required when using the account validity
          # feature.
          #
          #period: 6w

          # The amount of time before an account's expiry date at which Synapse will
          # send an email to the account's email address with a renewal link. By
          # default, no such emails are sent.
          #
          # If you enable this setting, you will also need to fill out the 'email' and
          # 'public_baseurl' configuration sections.
          #
          #renew_at: 1w

          # The subject of the email sent out with the renewal link. '%(app)s' can be
          # used as a placeholder for the 'app_name' parameter from the 'email'
          # section.
          #
          # Note that the placeholder must be written '%(app)s', including the
          # trailing 's'.
          #
          # If this is not set, a default value is used.
          #
          #renew_email_subject: "Renew your %(app)s account"

          # Directory in which Synapse will try to find templates for the HTML files to
          # serve to the user when trying to renew an account. If not set, default
          # templates from within the Synapse package will be used.
          #
          #template_dir: "res/templates"

          # File within 'template_dir' giving the HTML to be displayed to the user after
          # they successfully renewed their account. If not set, default text is used.
          #
          #account_renewed_html_path: "account_renewed.html"

          # File within 'template_dir' giving the HTML to be displayed when the user
          # tries to renew an account with an invalid renewal token. If not set,
          # default text is used.
          #
          #invalid_token_html_path: "invalid_token.html"
        """

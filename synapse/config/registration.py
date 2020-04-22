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

import os
from distutils.util import strtobool

import pkg_resources

from synapse.config._base import Config, ConfigError
from synapse.types import RoomAlias
from synapse.util.stringutils import random_string_with_symbols


class AccountValidityConfig(Config):
    section = "accountvalidity"

    def __init__(self, config, synapse_config):
        if config is None:
            return
        super(AccountValidityConfig, self).__init__()
        self.enabled = config.get("enabled", False)
        self.renew_by_email_enabled = "renew_at" in config

        if self.enabled:
            if "period" in config:
                self.period = self.parse_duration(config["period"])
            else:
                raise ConfigError("'period' is required when using account validity")

            if "renew_at" in config:
                self.renew_at = self.parse_duration(config["renew_at"])

            if "renew_email_subject" in config:
                self.renew_email_subject = config["renew_email_subject"]
            else:
                self.renew_email_subject = "Renew your %(app)s account"

            self.startup_job_max_delta = self.period * 10.0 / 100.0

        if self.renew_by_email_enabled:
            if "public_baseurl" not in synapse_config:
                raise ConfigError("Can't send renewal emails without 'public_baseurl'")

        template_dir = config.get("template_dir")

        if not template_dir:
            template_dir = pkg_resources.resource_filename("synapse", "res/templates")

        if "account_renewed_html_path" in config:
            file_path = os.path.join(template_dir, config["account_renewed_html_path"])

            self.account_renewed_html_content = self.read_file(
                file_path, "account_validity.account_renewed_html_path"
            )
        else:
            self.account_renewed_html_content = (
                "<html><body>Your account has been successfully renewed.</body><html>"
            )

        if "invalid_token_html_path" in config:
            file_path = os.path.join(template_dir, config["invalid_token_html_path"])

            self.invalid_token_html_content = self.read_file(
                file_path, "account_validity.invalid_token_html_path"
            )
        else:
            self.invalid_token_html_content = (
                "<html><body>Invalid renewal token.</body><html>"
            )


class RegistrationConfig(Config):
    section = "registration"

    def read_config(self, config, **kwargs):
        self.enable_registration = bool(
            strtobool(str(config.get("enable_registration", False)))
        )
        if "disable_registration" in config:
            self.enable_registration = not bool(
                strtobool(str(config["disable_registration"]))
            )

        self.account_validity = AccountValidityConfig(
            config.get("account_validity") or {}, config
        )

        self.registrations_require_3pid = config.get("registrations_require_3pid", [])
        self.allowed_local_3pids = config.get("allowed_local_3pids", [])
        self.check_is_for_allowed_local_3pids = config.get(
            "check_is_for_allowed_local_3pids", None
        )
        self.allow_invited_3pids = config.get("allow_invited_3pids", False)

        self.disable_3pid_changes = config.get("disable_3pid_changes", False)

        self.enable_3pid_lookup = config.get("enable_3pid_lookup", True)
        self.registration_shared_secret = config.get("registration_shared_secret")
        self.register_mxid_from_3pid = config.get("register_mxid_from_3pid")
        self.register_just_use_email_for_display_name = config.get(
            "register_just_use_email_for_display_name", False
        )

        self.bcrypt_rounds = config.get("bcrypt_rounds", 12)
        self.trusted_third_party_id_servers = config.get(
            "trusted_third_party_id_servers", ["matrix.org", "vector.im"]
        )
        account_threepid_delegates = config.get("account_threepid_delegates") or {}
        self.account_threepid_delegate_email = account_threepid_delegates.get("email")
        if (
            self.account_threepid_delegate_email
            and not self.account_threepid_delegate_email.startswith("http")
        ):
            raise ConfigError(
                "account_threepid_delegates.email must begin with http:// or https://"
            )
        self.account_threepid_delegate_msisdn = account_threepid_delegates.get("msisdn")
        if (
            self.account_threepid_delegate_msisdn
            and not self.account_threepid_delegate_msisdn.startswith("http")
        ):
            raise ConfigError(
                "account_threepid_delegates.msisdn must begin with http:// or https://"
            )
        if self.account_threepid_delegate_msisdn and not self.public_baseurl:
            raise ConfigError(
                "The configuration option `public_baseurl` is required if "
                "`account_threepid_delegate.msisdn` is set, such that "
                "clients know where to submit validation tokens to. Please "
                "configure `public_baseurl`."
            )

        self.default_identity_server = config.get("default_identity_server")
        self.allow_guest_access = config.get("allow_guest_access", False)

        if config.get("invite_3pid_guest", False):
            raise ConfigError("invite_3pid_guest is no longer supported")

        self.auto_join_rooms = config.get("auto_join_rooms", [])
        for room_alias in self.auto_join_rooms:
            if not RoomAlias.is_valid(room_alias):
                raise ConfigError("Invalid auto_join_rooms entry %s" % (room_alias,))
        self.autocreate_auto_join_rooms = config.get("autocreate_auto_join_rooms", True)

        self.disable_set_displayname = config.get("disable_set_displayname", False)
        self.disable_set_avatar_url = config.get("disable_set_avatar_url", False)

        self.replicate_user_profiles_to = config.get("replicate_user_profiles_to", [])
        if not isinstance(self.replicate_user_profiles_to, list):
            self.replicate_user_profiles_to = [self.replicate_user_profiles_to]

        self.shadow_server = config.get("shadow_server", None)
        self.rewrite_identity_server_urls = config.get(
            "rewrite_identity_server_urls"
        ) or {}

        self.disable_msisdn_registration = config.get(
            "disable_msisdn_registration", False
        )

        session_lifetime = config.get("session_lifetime")
        if session_lifetime is not None:
            session_lifetime = self.parse_duration(session_lifetime)
        self.session_lifetime = session_lifetime

    def generate_config_section(self, generate_secrets=False, **kwargs):
        if generate_secrets:
            registration_shared_secret = 'registration_shared_secret: "%s"' % (
                random_string_with_symbols(50),
            )
        else:
            registration_shared_secret = (
                "# registration_shared_secret: <PRIVATE STRING>"
            )

        return (
            """\
        ## Registration ##
        #
        # Registration can be rate-limited using the parameters in the "Ratelimiting"
        # section of this file.

        # Enable registration for new users.
        #
        #enable_registration: false

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
        # where d is equal to 10%% of the validity period.
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

          # The subject of the email sent out with the renewal link. '%%(app)s' can be
          # used as a placeholder for the 'app_name' parameter from the 'email'
          # section.
          #
          # Note that the placeholder must be written '%%(app)s', including the
          # trailing 's'.
          #
          # If this is not set, a default value is used.
          #
          #renew_email_subject: "Renew your %%(app)s account"

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

        # Time that a user's session remains valid for, after they log in.
        #
        # Note that this is not currently compatible with guest logins.
        #
        # Note also that this is calculated at login time: changes are not applied
        # retrospectively to users who have already logged in.
        #
        # By default, this is infinite.
        #
        #session_lifetime: 24h

        # The user must provide all of the below types of 3PID when registering.
        #
        #registrations_require_3pid:
        #  - email
        #  - msisdn

        # Explicitly disable asking for MSISDNs from the registration
        # flow (overrides registrations_require_3pid if MSISDNs are set as required)
        #
        #disable_msisdn_registration: true

        # Derive the user's matrix ID from a type of 3PID used when registering.
        # This overrides any matrix ID the user proposes when calling /register
        # The 3PID type should be present in registrations_require_3pid to avoid
        # users failing to register if they don't specify the right kind of 3pid.
        #
        #register_mxid_from_3pid: email

        # Uncomment to set the display name of new users to their email address,
        # rather than using the default heuristic.
        #
        #register_just_use_email_for_display_name: true

        # Mandate that users are only allowed to associate certain formats of
        # 3PIDs with accounts on this server.
        #
        # Use an Identity Server to establish which 3PIDs are allowed to register?
        # Overrides allowed_local_3pids below.
        #
        #check_is_for_allowed_local_3pids: matrix.org
        #
        # If you are using an IS you can also check whether that IS registers
        # pending invites for the given 3PID (and then allow it to sign up on
        # the platform):
        #
        #allow_invited_3pids: False
        #
        #allowed_local_3pids:
        #  - medium: email
        #    pattern: '.*@matrix\\.org'
        #  - medium: email
        #    pattern: '.*@vector\\.im'
        #  - medium: msisdn
        #    pattern: '\\+44'

        # If true, stop users from trying to change the 3PIDs associated with
        # their accounts.
        #
        #disable_3pid_changes: False

        # Enable 3PIDs lookup requests to identity servers from this server.
        #
        #enable_3pid_lookup: true

        # If set, allows registration of standard or admin accounts by anyone who
        # has the shared secret, even if registration is otherwise disabled.
        #
        %(registration_shared_secret)s

        # Set the number of bcrypt rounds used to generate password hash.
        # Larger numbers increase the work factor needed to generate the hash.
        # The default number is 12 (which equates to 2^12 rounds).
        # N.B. that increasing this will exponentially increase the time required
        # to register or login - e.g. 24 => 2^24 rounds which will take >20 mins.
        #
        #bcrypt_rounds: 12

        # Allows users to register as guests without a password/email/etc, and
        # participate in rooms hosted on this server which have been made
        # accessible to anonymous users.
        #
        #allow_guest_access: false

        # The identity server which we suggest that clients should use when users log
        # in on this server.
        #
        # (By default, no suggestion is made, so it is left up to the client.
        # This setting is ignored unless public_baseurl is also set.)
        #
        #default_identity_server: https://matrix.org

        # The list of identity servers trusted to verify third party
        # identifiers by this server.
        #
        # Also defines the ID server which will be called when an account is
        # deactivated (one will be picked arbitrarily).
        #
        # Note: This option is deprecated. Since v0.99.4, Synapse has tracked which identity
        # server a 3PID has been bound to. For 3PIDs bound before then, Synapse runs a
        # background migration script, informing itself that the identity server all of its
        # 3PIDs have been bound to is likely one of the below.
        #
        # As of Synapse v1.4.0, all other functionality of this option has been deprecated, and
        # it is now solely used for the purposes of the background migration script, and can be
        # removed once it has run.
        #trusted_third_party_id_servers:
        #  - matrix.org
        #  - vector.im

        # If enabled, user IDs, display names and avatar URLs will be replicated
        # to this server whenever they change.
        # This is an experimental API currently implemented by sydent to support
        # cross-homeserver user directories.
        #
        #replicate_user_profiles_to: example.com

        # If specified, attempt to replay registrations, profile changes & 3pid
        # bindings on the given target homeserver via the AS API. The HS is authed
        # via a given AS token.
        #
        #shadow_server:
        #  hs_url: https://shadow.example.com
        #  hs: shadow.example.com
        #  as_token: 12u394refgbdhivsia

        # If enabled, don't let users set their own display names/avatars
        # other than for the very first time (unless they are a server admin).
        # Useful when provisioning users based on the contents of a 3rd party
        # directory and to avoid ambiguities.
        #
        #disable_set_displayname: False
        #disable_set_avatar_url: False

        # Handle threepid (email/phone etc) registration and password resets through a set of
        # *trusted* identity servers. Note that this allows the configured identity server to
        # reset passwords for accounts!
        #
        # Be aware that if `email` is not set, and SMTP options have not been
        # configured in the email config block, registration and user password resets via
        # email will be globally disabled.
        #
        # Additionally, if `msisdn` is not set, registration and password resets via msisdn
        # will be disabled regardless. This is due to Synapse currently not supporting any
        # method of sending SMS messages on its own.
        #
        # To enable using an identity server for operations regarding a particular third-party
        # identifier type, set the value to the URL of that identity server as shown in the
        # examples below.
        #
        # Servers handling the these requests must answer the `/requestToken` endpoints defined
        # by the Matrix Identity Service API specification:
        # https://matrix.org/docs/spec/identity_service/latest
        #
        # If a delegate is specified, the config option public_baseurl must also be filled out.
        #
        account_threepid_delegates:
            #email: https://example.com     # Delegate email sending to example.com
            #msisdn: http://localhost:8090  # Delegate SMS sending to this local process

        # Users who register on this homeserver will automatically be joined
        # to these rooms
        #
        #auto_join_rooms:
        #  - "#example:example.com"

        # Where auto_join_rooms are specified, setting this flag ensures that the
        # the rooms exist by creating them when the first user on the
        # homeserver registers.
        # Setting to false means that if the rooms are not manually created,
        # users cannot be auto-joined since they do not exist.
        #
        #autocreate_auto_join_rooms: true

        # Rewrite identity server URLs with a map from one URL to another. Applies to URLs
        # provided by clients (which have https:// prepended) and those specified
        # in `account_threepid_delegates`. URLs should not feature a trailing slash.
        #
        rewrite_identity_server_urls:
        #   "https://somewhere.example.com": "https://somewhereelse.example.com"
        """
            % locals()
        )

    @staticmethod
    def add_arguments(parser):
        reg_group = parser.add_argument_group("registration")
        reg_group.add_argument(
            "--enable-registration",
            action="store_true",
            default=None,
            help="Enable registration for new users.",
        )

    def read_arguments(self, args):
        if args.enable_registration is not None:
            self.enable_registration = bool(strtobool(str(args.enable_registration)))

# -*- coding: utf-8 -*-
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

import logging
import re

logger = logging.getLogger(__name__)


async def check_3pid_allowed(hs, medium, address):
    """Checks whether a given 3PID is allowed to be used on this HS

    Args:
        hs (synapse.server.HomeServer): server
        medium (str): 3pid medium - e.g. email, msisdn
        address (str): address within that medium (e.g. "wotan@matrix.org")
            msisdns need to first have been canonicalised
    Returns:
        bool: whether the 3PID medium/address is allowed to be added to this HS
    """

    if hs.config.check_is_for_allowed_local_3pids:
        data = await hs.get_simple_http_client().get_json(
            "https://%s%s"
            % (
                hs.config.check_is_for_allowed_local_3pids,
                "/_matrix/identity/api/v1/internal-info",
            ),
            {"medium": medium, "address": address},
        )

        # Check for invalid response
        if "hs" not in data and "shadow_hs" not in data:
            return False

        # Check if this user is intended to register for this homeserver
        if (
            data.get("hs") != hs.config.server_name
            and data.get("shadow_hs") != hs.config.server_name
        ):
            return False

        if data.get("requires_invite", False) and not data.get("invited", False):
            # Requires an invite but hasn't been invited
            return False

        return True

    if hs.config.allowed_local_3pids:
        for constraint in hs.config.allowed_local_3pids:
            logger.debug(
                "Checking 3PID %s (%s) against %s (%s)",
                address,
                medium,
                constraint["pattern"],
                constraint["medium"],
            )
            if medium == constraint["medium"] and re.match(
                constraint["pattern"], address
            ):
                return True
    else:
        return True

    return False


def canonicalise_email(address: str) -> str:
    """'Canonicalise' email address
    Case folding of local part of email address and lowercase domain part
    See MSC2265, https://github.com/matrix-org/matrix-doc/pull/2265

    Args:
        address: email address to be canonicalised
    Returns:
        The canonical form of the email address
    Raises:
        ValueError if the address could not be parsed.
    """

    address = address.strip()

    parts = address.split("@")
    if len(parts) != 2:
        logger.debug("Couldn't parse email address %s", address)
        raise ValueError("Unable to parse email address")

    return parts[0].casefold() + "@" + parts[1].lower()

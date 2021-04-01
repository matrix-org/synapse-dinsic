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
import logging
import urllib
from typing import TYPE_CHECKING, List, Optional, Tuple

from prometheus_client import Counter

from synapse.api.constants import EventTypes, Membership, ThirdPartyEntityKind
from synapse.api.errors import CodeMessageException
from synapse.events import EventBase
from synapse.events.utils import serialize_event
from synapse.http.client import SimpleHttpClient
from synapse.types import JsonDict, ThirdPartyInstanceID
from synapse.util.caches.response_cache import ResponseCache

if TYPE_CHECKING:
    from synapse.appservice import ApplicationService

logger = logging.getLogger(__name__)

sent_transactions_counter = Counter(
    "synapse_appservice_api_sent_transactions",
    "Number of /transactions/ requests sent",
    ["service"],
)

failed_transactions_counter = Counter(
    "synapse_appservice_api_failed_transactions",
    "Number of /transactions/ requests that failed to send",
    ["service"],
)

sent_events_counter = Counter(
    "synapse_appservice_api_sent_events", "Number of events sent to the AS", ["service"]
)

HOUR_IN_MS = 60 * 60 * 1000


APP_SERVICE_PREFIX = "/_matrix/app/unstable"


def _is_valid_3pe_metadata(info):
    if "instances" not in info:
        return False
    if not isinstance(info["instances"], list):
        return False
    return True


def _is_valid_3pe_result(r, field):
    if not isinstance(r, dict):
        return False

    for k in (field, "protocol"):
        if k not in r:
            return False
        if not isinstance(r[k], str):
            return False

    if "fields" not in r:
        return False
    fields = r["fields"]
    if not isinstance(fields, dict):
        return False
    for k in fields.keys():
        if not isinstance(fields[k], str):
            return False

    return True


class ApplicationServiceApi(SimpleHttpClient):
    """This class manages HS -> AS communications, including querying and
    pushing.
    """

    def __init__(self, hs):
        super().__init__(hs)
        self.clock = hs.get_clock()

        self.protocol_meta_cache = ResponseCache(
            hs, "as_protocol_meta", timeout_ms=HOUR_IN_MS
        )  # type: ResponseCache[Tuple[str, str]]

    async def query_user(self, service, user_id):
        if service.url is None:
            return False
        uri = service.url + ("/users/%s" % urllib.parse.quote(user_id))
        try:
            response = await self.get_json(uri, {"access_token": service.hs_token})
            if response is not None:  # just an empty json object
                return True
        except CodeMessageException as e:
            if e.code == 404:
                return False
            logger.warning("query_user to %s received %s", uri, e.code)
        except Exception as ex:
            logger.warning("query_user to %s threw exception %s", uri, ex)
        return False

    async def query_alias(self, service, alias):
        if service.url is None:
            return False
        uri = service.url + ("/rooms/%s" % urllib.parse.quote(alias))
        try:
            response = await self.get_json(uri, {"access_token": service.hs_token})
            if response is not None:  # just an empty json object
                return True
        except CodeMessageException as e:
            logger.warning("query_alias to %s received %s", uri, e.code)
            if e.code == 404:
                return False
        except Exception as ex:
            logger.warning("query_alias to %s threw exception %s", uri, ex)
        return False

    async def query_3pe(self, service, kind, protocol, fields):
        if kind == ThirdPartyEntityKind.USER:
            required_field = "userid"
        elif kind == ThirdPartyEntityKind.LOCATION:
            required_field = "alias"
        else:
            raise ValueError("Unrecognised 'kind' argument %r to query_3pe()", kind)
        if service.url is None:
            return []

        uri = "%s%s/thirdparty/%s/%s" % (
            service.url,
            APP_SERVICE_PREFIX,
            kind,
            urllib.parse.quote(protocol),
        )
        try:
            response = await self.get_json(uri, fields)
            if not isinstance(response, list):
                logger.warning(
                    "query_3pe to %s returned an invalid response %r", uri, response
                )
                return []

            ret = []
            for r in response:
                if _is_valid_3pe_result(r, field=required_field):
                    ret.append(r)
                else:
                    logger.warning(
                        "query_3pe to %s returned an invalid result %r", uri, r
                    )

            return ret
        except Exception as ex:
            logger.warning("query_3pe to %s threw exception %s", uri, ex)
            return []

    async def get_3pe_protocol(
        self, service: "ApplicationService", protocol: str
    ) -> Optional[JsonDict]:
        if service.url is None:
            return {}

        async def _get() -> Optional[JsonDict]:
            uri = "%s%s/thirdparty/protocol/%s" % (
                service.url,
                APP_SERVICE_PREFIX,
                urllib.parse.quote(protocol),
            )
            try:
                info = await self.get_json(uri)

                if not _is_valid_3pe_metadata(info):
                    logger.warning(
                        "query_3pe_protocol to %s did not return a valid result", uri
                    )
                    return None

                for instance in info.get("instances", []):
                    network_id = instance.get("network_id", None)
                    if network_id is not None:
                        instance["instance_id"] = ThirdPartyInstanceID(
                            service.id, network_id
                        ).to_string()

                return info
            except Exception as ex:
                logger.warning("query_3pe_protocol to %s threw exception %s", uri, ex)
                return None

        key = (service.id, protocol)
        return await self.protocol_meta_cache.wrap(key, _get)

    async def push_bulk(
        self,
        service: "ApplicationService",
        events: List[EventBase],
        ephemeral: List[JsonDict],
        txn_id: Optional[int] = None,
    ):
        if service.url is None:
            return True

        events = self._serialize(service, events)

        if txn_id is None:
            logger.warning(
                "push_bulk: Missing txn ID sending events to %s", service.url
            )
            txn_id = 0

        uri = service.url + ("/transactions/%s" % urllib.parse.quote(str(txn_id)))

        # Never send ephemeral events to appservices that do not support it
        if service.supports_ephemeral:
            body = {"events": events, "de.sorunome.msc2409.ephemeral": ephemeral}
        else:
            body = {"events": events}

        try:
            await self.put_json(
                uri=uri, json_body=body, args={"access_token": service.hs_token},
            )
            sent_transactions_counter.labels(service.id).inc()
            sent_events_counter.labels(service.id).inc(len(events))
            return True
        except CodeMessageException as e:
            logger.warning("push_bulk to %s received %s", uri, e.code)
        except Exception as ex:
            logger.warning("push_bulk to %s threw exception %s", uri, ex)
        failed_transactions_counter.labels(service.id).inc()
        return False

    def _serialize(self, service, events):
        time_now = self.clock.time_msec()
        return [
            serialize_event(
                e,
                time_now,
                as_client_event=True,
                # If this is an invite or a knock membership event, and we're interested
                # in this user, then include any stripped state alongside the event.
                include_stripped_room_state=(
                    e.type == EventTypes.Member
                    and (
                        e.membership == Membership.INVITE
                        or e.membership == Membership.KNOCK
                    )
                    and service.is_interested_in_user(e.state_key)
                ),
            )
            for e in events
        ]

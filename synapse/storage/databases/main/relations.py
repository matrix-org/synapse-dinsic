# Copyright 2019 New Vector Ltd
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
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple, Union, cast

import attr
from frozendict import frozendict

from synapse.api.constants import EventTypes, RelationTypes
from synapse.events import EventBase
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.stream import generate_pagination_where_clause
from synapse.storage.relations import (
    AggregationPaginationToken,
    PaginationChunk,
    RelationPaginationToken,
)
from synapse.types import JsonDict
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _ThreadAggregation:
    latest_event: EventBase
    count: int
    current_user_participated: bool


@attr.s(slots=True, auto_attribs=True)
class BundledAggregations:
    """
    The bundled aggregations for an event.

    Some values require additional processing during serialization.
    """

    annotations: Optional[JsonDict] = None
    references: Optional[JsonDict] = None
    replace: Optional[EventBase] = None
    thread: Optional[_ThreadAggregation] = None

    def __bool__(self) -> bool:
        return bool(self.annotations or self.references or self.replace or self.thread)


class RelationsWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._msc3440_enabled = hs.config.experimental.msc3440_enabled

    @cached(tree=True)
    async def get_relations_for_event(
        self,
        event_id: str,
        room_id: str,
        relation_type: Optional[str] = None,
        event_type: Optional[str] = None,
        aggregation_key: Optional[str] = None,
        limit: int = 5,
        direction: str = "b",
        from_token: Optional[RelationPaginationToken] = None,
        to_token: Optional[RelationPaginationToken] = None,
    ) -> PaginationChunk:
        """Get a list of relations for an event, ordered by topological ordering.

        Args:
            event_id: Fetch events that relate to this event ID.
            room_id: The room the event belongs to.
            relation_type: Only fetch events with this relation type, if given.
            event_type: Only fetch events with this event type, if given.
            aggregation_key: Only fetch events with this aggregation key, if given.
            limit: Only fetch the most recent `limit` events.
            direction: Whether to fetch the most recent first (`"b"`) or the
                oldest first (`"f"`).
            from_token: Fetch rows from the given token, or from the start if None.
            to_token: Fetch rows up to the given token, or up to the end if None.

        Returns:
            List of event IDs that match relations requested. The rows are of
            the form `{"event_id": "..."}`.
        """

        where_clause = ["relates_to_id = ?", "room_id = ?"]
        where_args: List[Union[str, int]] = [event_id, room_id]

        if relation_type is not None:
            where_clause.append("relation_type = ?")
            where_args.append(relation_type)

        if event_type is not None:
            where_clause.append("type = ?")
            where_args.append(event_type)

        if aggregation_key:
            where_clause.append("aggregation_key = ?")
            where_args.append(aggregation_key)

        pagination_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("topological_ordering", "stream_ordering"),
            from_token=attr.astuple(from_token) if from_token else None,  # type: ignore[arg-type]
            to_token=attr.astuple(to_token) if to_token else None,  # type: ignore[arg-type]
            engine=self.database_engine,
        )

        if pagination_clause:
            where_clause.append(pagination_clause)

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        sql = """
            SELECT event_id, topological_ordering, stream_ordering
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE %s
            ORDER BY topological_ordering %s, stream_ordering %s
            LIMIT ?
        """ % (
            " AND ".join(where_clause),
            order,
            order,
        )

        def _get_recent_references_for_event_txn(
            txn: LoggingTransaction,
        ) -> PaginationChunk:
            txn.execute(sql, where_args + [limit + 1])

            last_topo_id = None
            last_stream_id = None
            events = []
            for row in txn:
                events.append({"event_id": row[0]})
                last_topo_id = row[1]
                last_stream_id = row[2]

            next_batch = None
            if len(events) > limit and last_topo_id and last_stream_id:
                next_batch = RelationPaginationToken(last_topo_id, last_stream_id)

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return await self.db_pool.runInteraction(
            "get_recent_references_for_event", _get_recent_references_for_event_txn
        )

    async def event_includes_relation(self, event_id: str) -> bool:
        """Check if the given event relates to another event.

        An event has a relation if it has a valid m.relates_to with a rel_type
        and event_id in the content:

        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$other_event_id"
                }
            }
        }

        Args:
            event_id: The event to check.

        Returns:
            True if the event includes a valid relation.
        """

        result = await self.db_pool.simple_select_one_onecol(
            table="event_relations",
            keyvalues={"event_id": event_id},
            retcol="event_id",
            allow_none=True,
            desc="event_includes_relation",
        )
        return result is not None

    async def event_is_target_of_relation(self, parent_id: str) -> bool:
        """Check if the given event is the target of another event's relation.

        An event is the target of an event relation if it has a valid
        m.relates_to with a rel_type and event_id pointing to parent_id in the
        content:

        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$parent_id"
                }
            }
        }

        Args:
            parent_id: The event to check.

        Returns:
            True if the event is the target of another event's relation.
        """

        result = await self.db_pool.simple_select_one_onecol(
            table="event_relations",
            keyvalues={"relates_to_id": parent_id},
            retcol="event_id",
            allow_none=True,
            desc="event_is_target_of_relation",
        )
        return result is not None

    @cached(tree=True)
    async def get_aggregation_groups_for_event(
        self,
        event_id: str,
        room_id: str,
        event_type: Optional[str] = None,
        limit: int = 5,
        direction: str = "b",
        from_token: Optional[AggregationPaginationToken] = None,
        to_token: Optional[AggregationPaginationToken] = None,
    ) -> PaginationChunk:
        """Get a list of annotations on the event, grouped by event type and
        aggregation key, sorted by count.

        This is used e.g. to get the what and how many reactions have happend
        on an event.

        Args:
            event_id: Fetch events that relate to this event ID.
            room_id: The room the event belongs to.
            event_type: Only fetch events with this event type, if given.
            limit: Only fetch the `limit` groups.
            direction: Whether to fetch the highest count first (`"b"`) or
                the lowest count first (`"f"`).
            from_token: Fetch rows from the given token, or from the start if None.
            to_token: Fetch rows up to the given token, or up to the end if None.

        Returns:
            List of groups of annotations that match. Each row is a dict with
            `type`, `key` and `count` fields.
        """

        where_clause = ["relates_to_id = ?", "room_id = ?", "relation_type = ?"]
        where_args: List[Union[str, int]] = [
            event_id,
            room_id,
            RelationTypes.ANNOTATION,
        ]

        if event_type:
            where_clause.append("type = ?")
            where_args.append(event_type)

        having_clause = generate_pagination_where_clause(
            direction=direction,
            column_names=("COUNT(*)", "MAX(stream_ordering)"),
            from_token=attr.astuple(from_token) if from_token else None,  # type: ignore[arg-type]
            to_token=attr.astuple(to_token) if to_token else None,  # type: ignore[arg-type]
            engine=self.database_engine,
        )

        if direction == "b":
            order = "DESC"
        else:
            order = "ASC"

        if having_clause:
            having_clause = "HAVING " + having_clause
        else:
            having_clause = ""

        sql = """
            SELECT type, aggregation_key, COUNT(DISTINCT sender), MAX(stream_ordering)
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE {where_clause}
            GROUP BY relation_type, type, aggregation_key
            {having_clause}
            ORDER BY COUNT(*) {order}, MAX(stream_ordering) {order}
            LIMIT ?
        """.format(
            where_clause=" AND ".join(where_clause),
            order=order,
            having_clause=having_clause,
        )

        def _get_aggregation_groups_for_event_txn(
            txn: LoggingTransaction,
        ) -> PaginationChunk:
            txn.execute(sql, where_args + [limit + 1])

            next_batch = None
            events = []
            for row in txn:
                events.append({"type": row[0], "key": row[1], "count": row[2]})
                next_batch = AggregationPaginationToken(row[2], row[3])

            if len(events) <= limit:
                next_batch = None

            return PaginationChunk(
                chunk=list(events[:limit]), next_batch=next_batch, prev_batch=from_token
            )

        return await self.db_pool.runInteraction(
            "get_aggregation_groups_for_event", _get_aggregation_groups_for_event_txn
        )

    @cached()
    async def get_applicable_edit(
        self, event_id: str, room_id: str
    ) -> Optional[EventBase]:
        """Get the most recent edit (if any) that has happened for the given
        event.

        Correctly handles checking whether edits were allowed to happen.

        Args:
            event_id: The original event ID
            room_id: The original event's room ID

        Returns:
            The most recent edit, if any.
        """

        # We only allow edits for `m.room.message` events that have the same sender
        # and event type. We can't assert these things during regular event auth so
        # we have to do the checks post hoc.

        # Fetches latest edit that has the same type and sender as the
        # original, and is an `m.room.message`.
        sql = """
            SELECT edit.event_id FROM events AS edit
            INNER JOIN event_relations USING (event_id)
            INNER JOIN events AS original ON
                original.event_id = relates_to_id
                AND edit.type = original.type
                AND edit.sender = original.sender
            WHERE
                relates_to_id = ?
                AND relation_type = ?
                AND edit.room_id = ?
                AND edit.type = 'm.room.message'
            ORDER by edit.origin_server_ts DESC, edit.event_id DESC
            LIMIT 1
        """

        def _get_applicable_edit_txn(txn: LoggingTransaction) -> Optional[str]:
            txn.execute(sql, (event_id, RelationTypes.REPLACE, room_id))
            row = txn.fetchone()
            if row:
                return row[0]
            return None

        edit_id = await self.db_pool.runInteraction(
            "get_applicable_edit", _get_applicable_edit_txn
        )

        if not edit_id:
            return None

        return await self.get_event(edit_id, allow_none=True)  # type: ignore[attr-defined]

    @cached()
    async def get_thread_summary(
        self, event_id: str, room_id: str
    ) -> Tuple[int, Optional[EventBase]]:
        """Get the number of threaded replies and the latest reply (if any) for the given event.

        Args:
            event_id: Summarize the thread related to this event ID.
            room_id: The room the event belongs to.

        Returns:
            The number of items in the thread and the most recent response, if any.
        """

        def _get_thread_summary_txn(
            txn: LoggingTransaction,
        ) -> Tuple[int, Optional[str]]:
            # Fetch the latest event ID in the thread.
            # TODO Should this only allow m.room.message events.
            sql = """
                SELECT event_id
                FROM event_relations
                INNER JOIN events USING (event_id)
                WHERE
                    relates_to_id = ?
                    AND room_id = ?
                    AND relation_type = ?
                ORDER BY topological_ordering DESC, stream_ordering DESC
                LIMIT 1
            """

            txn.execute(sql, (event_id, room_id, RelationTypes.THREAD))
            row = txn.fetchone()
            if row is None:
                return 0, None

            latest_event_id = row[0]

            # Fetch the number of threaded replies.
            sql = """
                SELECT COUNT(event_id)
                FROM event_relations
                INNER JOIN events USING (event_id)
                WHERE
                    relates_to_id = ?
                    AND room_id = ?
                    AND relation_type = ?
            """
            txn.execute(sql, (event_id, room_id, RelationTypes.THREAD))
            count = cast(Tuple[int], txn.fetchone())[0]

            return count, latest_event_id

        count, latest_event_id = await self.db_pool.runInteraction(
            "get_thread_summary", _get_thread_summary_txn
        )

        latest_event = None
        if latest_event_id:
            latest_event = await self.get_event(latest_event_id, allow_none=True)  # type: ignore[attr-defined]

        return count, latest_event

    @cached()
    async def get_thread_participated(
        self, event_id: str, room_id: str, user_id: str
    ) -> bool:
        """Get whether the requesting user participated in a thread.

        This is separate from get_thread_summary since that can be cached across
        all users while this value is specific to the requeser.

        Args:
            event_id: The thread related to this event ID.
            room_id: The room the event belongs to.
            user_id: The user requesting the summary.

        Returns:
            True if the requesting user participated in the thread, otherwise false.
        """

        def _get_thread_summary_txn(txn: LoggingTransaction) -> bool:
            # Fetch whether the requester has participated or not.
            sql = """
                SELECT 1
                FROM event_relations
                INNER JOIN events USING (event_id)
                WHERE
                    relates_to_id = ?
                    AND room_id = ?
                    AND relation_type = ?
                    AND sender = ?
            """

            txn.execute(sql, (event_id, room_id, RelationTypes.THREAD, user_id))
            return bool(txn.fetchone())

        return await self.db_pool.runInteraction(
            "get_thread_summary", _get_thread_summary_txn
        )

    async def events_have_relations(
        self,
        parent_ids: List[str],
        relation_senders: Optional[List[str]],
        relation_types: Optional[List[str]],
    ) -> List[str]:
        """Check which events have a relationship from the given senders of the
        given types.

        Args:
            parent_ids: The events being annotated
            relation_senders: The relation senders to check.
            relation_types: The relation types to check.

        Returns:
            True if the event has at least one relationship from one of the given senders of the given type.
        """
        # If no restrictions are given then the event has the required relations.
        if not relation_senders and not relation_types:
            return parent_ids

        sql = """
            SELECT relates_to_id FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE
                %s;
        """

        def _get_if_events_have_relations(txn) -> List[str]:
            clauses: List[str] = []
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "relates_to_id", parent_ids
            )
            clauses.append(clause)

            if relation_senders:
                clause, temp_args = make_in_list_sql_clause(
                    txn.database_engine, "sender", relation_senders
                )
                clauses.append(clause)
                args.extend(temp_args)
            if relation_types:
                clause, temp_args = make_in_list_sql_clause(
                    txn.database_engine, "relation_type", relation_types
                )
                clauses.append(clause)
                args.extend(temp_args)

            txn.execute(sql % " AND ".join(clauses), args)

            return [row[0] for row in txn]

        return await self.db_pool.runInteraction(
            "get_if_events_have_relations", _get_if_events_have_relations
        )

    async def has_user_annotated_event(
        self, parent_id: str, event_type: str, aggregation_key: str, sender: str
    ) -> bool:
        """Check if a user has already annotated an event with the same key
        (e.g. already liked an event).

        Args:
            parent_id: The event being annotated
            event_type: The event type of the annotation
            aggregation_key: The aggregation key of the annotation
            sender: The sender of the annotation

        Returns:
            True if the event is already annotated.
        """

        sql = """
            SELECT 1 FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE
                relates_to_id = ?
                AND relation_type = ?
                AND type = ?
                AND sender = ?
                AND aggregation_key = ?
            LIMIT 1;
        """

        def _get_if_user_has_annotated_event(txn: LoggingTransaction) -> bool:
            txn.execute(
                sql,
                (
                    parent_id,
                    RelationTypes.ANNOTATION,
                    event_type,
                    sender,
                    aggregation_key,
                ),
            )

            return bool(txn.fetchone())

        return await self.db_pool.runInteraction(
            "get_if_user_has_annotated_event", _get_if_user_has_annotated_event
        )

    async def _get_bundled_aggregation_for_event(
        self, event: EventBase, user_id: str
    ) -> Optional[BundledAggregations]:
        """Generate bundled aggregations for an event.

        Note that this does not use a cache, but depends on cached methods.

        Args:
            event: The event to calculate bundled aggregations for.
            user_id: The user requesting the bundled aggregations.

        Returns:
            The bundled aggregations for an event, if bundled aggregations are
            enabled and the event can have bundled aggregations.
        """
        # State events and redacted events do not get bundled aggregations.
        if event.is_state() or event.internal_metadata.is_redacted():
            return None

        # Do not bundle aggregations for an event which represents an edit or an
        # annotation. It does not make sense for them to have related events.
        relates_to = event.content.get("m.relates_to")
        if isinstance(relates_to, (dict, frozendict)):
            relation_type = relates_to.get("rel_type")
            if relation_type in (RelationTypes.ANNOTATION, RelationTypes.REPLACE):
                return None

        event_id = event.event_id
        room_id = event.room_id

        # The bundled aggregations to include, a mapping of relation type to a
        # type-specific value. Some types include the direct return type here
        # while others need more processing during serialization.
        aggregations = BundledAggregations()

        annotations = await self.get_aggregation_groups_for_event(event_id, room_id)
        if annotations.chunk:
            aggregations.annotations = annotations.to_dict()

        references = await self.get_relations_for_event(
            event_id, room_id, RelationTypes.REFERENCE, direction="f"
        )
        if references.chunk:
            aggregations.references = references.to_dict()

        edit = None
        if event.type == EventTypes.Message:
            edit = await self.get_applicable_edit(event_id, room_id)

        if edit:
            aggregations.replace = edit

        # If this event is the start of a thread, include a summary of the replies.
        if self._msc3440_enabled:
            thread_count, latest_thread_event = await self.get_thread_summary(
                event_id, room_id
            )
            participated = await self.get_thread_participated(
                event_id, room_id, user_id
            )
            if latest_thread_event:
                aggregations.thread = _ThreadAggregation(
                    latest_event=latest_thread_event,
                    count=thread_count,
                    current_user_participated=participated,
                )

        # Store the bundled aggregations in the event metadata for later use.
        return aggregations

    async def get_bundled_aggregations(
        self,
        events: Iterable[EventBase],
        user_id: str,
    ) -> Dict[str, BundledAggregations]:
        """Generate bundled aggregations for events.

        Args:
            events: The iterable of events to calculate bundled aggregations for.
            user_id: The user requesting the bundled aggregations.

        Returns:
            A map of event ID to the bundled aggregation for the event. Not all
            events may have bundled aggregations in the results.
        """

        # TODO Parallelize.
        results = {}
        for event in events:
            event_result = await self._get_bundled_aggregation_for_event(event, user_id)
            if event_result:
                results[event.event_id] = event_result

        return results


class RelationsStore(RelationsWorkerStore):
    pass

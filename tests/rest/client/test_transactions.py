# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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

from http import HTTPStatus
from unittest.mock import Mock, call

from twisted.internet import defer, reactor

from synapse.logging.context import SENTINEL_CONTEXT, LoggingContext, current_context
from synapse.rest.client.transactions import CLEANUP_PERIOD_MS, HttpTransactionCache
from synapse.util import Clock

from tests import unittest
from tests.utils import MockClock


class HttpTransactionCacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MockClock()
        self.hs = Mock()
        self.hs.get_clock = Mock(return_value=self.clock)
        self.hs.get_auth = Mock()
        self.cache = HttpTransactionCache(self.hs)

        self.mock_http_response = (HTTPStatus.OK, "GOOD JOB!")
        self.mock_key = "foo"

    @defer.inlineCallbacks
    def test_executes_given_function(self):
        cb = Mock(return_value=defer.succeed(self.mock_http_response))
        res = yield self.cache.fetch_or_execute(
            self.mock_key, cb, "some_arg", keyword="arg"
        )
        cb.assert_called_once_with("some_arg", keyword="arg")
        self.assertEqual(res, self.mock_http_response)

    @defer.inlineCallbacks
    def test_deduplicates_based_on_key(self):
        cb = Mock(return_value=defer.succeed(self.mock_http_response))
        for i in range(3):  # invoke multiple times
            res = yield self.cache.fetch_or_execute(
                self.mock_key, cb, "some_arg", keyword="arg", changing_args=i
            )
            self.assertEqual(res, self.mock_http_response)
        # expect only a single call to do the work
        cb.assert_called_once_with("some_arg", keyword="arg", changing_args=0)

    @defer.inlineCallbacks
    def test_logcontexts_with_async_result(self):
        @defer.inlineCallbacks
        def cb():
            yield Clock(reactor).sleep(0)
            return "yay"

        @defer.inlineCallbacks
        def test():
            with LoggingContext("c") as c1:
                res = yield self.cache.fetch_or_execute(self.mock_key, cb)
                self.assertIs(current_context(), c1)
                self.assertEqual(res, "yay")

        # run the test twice in parallel
        d = defer.gatherResults([test(), test()])
        self.assertIs(current_context(), SENTINEL_CONTEXT)
        yield d
        self.assertIs(current_context(), SENTINEL_CONTEXT)

    @defer.inlineCallbacks
    def test_does_not_cache_exceptions(self):
        """Checks that, if the callback throws an exception, it is called again
        for the next request.
        """
        called = [False]

        def cb():
            if called[0]:
                # return a valid result the second time
                return defer.succeed(self.mock_http_response)

            called[0] = True
            raise Exception("boo")

        with LoggingContext("test") as test_context:
            try:
                yield self.cache.fetch_or_execute(self.mock_key, cb)
            except Exception as e:
                self.assertEqual(e.args[0], "boo")
            self.assertIs(current_context(), test_context)

            res = yield self.cache.fetch_or_execute(self.mock_key, cb)
            self.assertEqual(res, self.mock_http_response)
            self.assertIs(current_context(), test_context)

    @defer.inlineCallbacks
    def test_does_not_cache_failures(self):
        """Checks that, if the callback returns a failure, it is called again
        for the next request.
        """
        called = [False]

        def cb():
            if called[0]:
                # return a valid result the second time
                return defer.succeed(self.mock_http_response)

            called[0] = True
            return defer.fail(Exception("boo"))

        with LoggingContext("test") as test_context:
            try:
                yield self.cache.fetch_or_execute(self.mock_key, cb)
            except Exception as e:
                self.assertEqual(e.args[0], "boo")
            self.assertIs(current_context(), test_context)

            res = yield self.cache.fetch_or_execute(self.mock_key, cb)
            self.assertEqual(res, self.mock_http_response)
            self.assertIs(current_context(), test_context)

    @defer.inlineCallbacks
    def test_cleans_up(self):
        cb = Mock(return_value=defer.succeed(self.mock_http_response))
        yield self.cache.fetch_or_execute(self.mock_key, cb, "an arg")
        # should NOT have cleaned up yet
        self.clock.advance_time_msec(CLEANUP_PERIOD_MS / 2)

        yield self.cache.fetch_or_execute(self.mock_key, cb, "an arg")
        # still using cache
        cb.assert_called_once_with("an arg")

        self.clock.advance_time_msec(CLEANUP_PERIOD_MS)

        yield self.cache.fetch_or_execute(self.mock_key, cb, "an arg")
        # no longer using cache
        self.assertEqual(cb.call_count, 2)
        self.assertEqual(cb.call_args_list, [call("an arg"), call("an arg")])

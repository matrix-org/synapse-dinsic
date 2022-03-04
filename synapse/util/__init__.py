# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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

import json
import logging
import re

import attr
from frozendict import frozendict

from twisted.internet import defer, task

from synapse.logging import context

logger = logging.getLogger(__name__)


def _reject_invalid_json(val):
    """Do not allow Infinity, -Infinity, or NaN values in JSON."""
    raise ValueError("Invalid JSON value: '%s'" % val)


def _handle_frozendict(obj):
    """Helper for json_encoder. Makes frozendicts serializable by returning
    the underlying dict
    """
    if type(obj) is frozendict:
        # fishing the protected dict out of the object is a bit nasty,
        # but we don't really want the overhead of copying the dict.
        try:
            return obj._dict
        except AttributeError:
            # When the C implementation of frozendict is used,
            # there isn't a `_dict` attribute with a dict
            # so we resort to making a copy of the frozendict
            return dict(obj)
    raise TypeError(
        "Object of type %s is not JSON serializable" % obj.__class__.__name__
    )


# A custom JSON encoder which:
#   * handles frozendicts
#   * produces valid JSON (no NaNs etc)
#   * reduces redundant whitespace
json_encoder = json.JSONEncoder(
    allow_nan=False, separators=(",", ":"), default=_handle_frozendict
)

# Create a custom decoder to reject Python extensions to JSON.
json_decoder = json.JSONDecoder(parse_constant=_reject_invalid_json)


def unwrapFirstError(failure):
    # defer.gatherResults and DeferredLists wrap failures.
    failure.trap(defer.FirstError)
    return failure.value.subFailure


@attr.s(slots=True)
class Clock:
    """
    A Clock wraps a Twisted reactor and provides utilities on top of it.

    Args:
        reactor: The Twisted reactor to use.
    """

    _reactor = attr.ib()

    @defer.inlineCallbacks
    def sleep(self, seconds):
        d = defer.Deferred()
        with context.PreserveLoggingContext():
            self._reactor.callLater(seconds, d.callback, seconds)
            res = yield d
        return res

    def time(self):
        """Returns the current system time in seconds since epoch."""
        return self._reactor.seconds()

    def time_msec(self):
        """Returns the current system time in milliseconds since epoch."""
        return int(self.time() * 1000)

    def looping_call(self, f, msec, *args, **kwargs):
        """Call a function repeatedly.

        Waits `msec` initially before calling `f` for the first time.

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            f(function): The function to call repeatedly.
            msec(float): How long to wait between calls in milliseconds.
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        call = task.LoopingCall(f, *args, **kwargs)
        call.clock = self._reactor
        d = call.start(msec / 1000.0, now=False)
        d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        return call

    def call_later(self, delay, callback, *args, **kwargs):
        """Call something later

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            delay(float): How long to wait in seconds.
            callback(function): Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """

        def wrapped_callback(*args, **kwargs):
            with context.PreserveLoggingContext():
                callback(*args, **kwargs)

        with context.PreserveLoggingContext():
            return self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)

    def cancel_call_later(self, timer, ignore_errs=False):
        try:
            timer.cancel()
        except Exception:
            if not ignore_errs:
                raise


def log_failure(failure, msg, consumeErrors=True):
    """Creates a function suitable for passing to `Deferred.addErrback` that
    logs any failures that occur.

    Args:
        msg (str): Message to log
        consumeErrors (bool): If true consumes the failure, otherwise passes
            on down the callback chain

    Returns:
        func(Failure)
    """

    logger.error(
        msg, exc_info=(failure.type, failure.value, failure.getTracebackObject())
    )

    if not consumeErrors:
        return failure


def glob_to_regex(glob):
    """Converts a glob to a compiled regex object.

    The regex is anchored at the beginning and end of the string.

    Args:
        glob (str)

    Returns:
        re.RegexObject
    """
    res = ""
    for c in glob:
        if c == "*":
            res = res + ".*"
        elif c == "?":
            res = res + "."
        else:
            res = res + re.escape(c)

    # \A anchors at start of string, \Z at end of string
    return re.compile(r"\A" + res + r"\Z", re.IGNORECASE)

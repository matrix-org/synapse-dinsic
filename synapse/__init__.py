# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018-9 New Vector Ltd
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

""" This is a reference implementation of a Matrix homeserver.
"""

import json
import os
import sys

from matrix_common.versionstring import get_distribution_version_string

# Check that we're not running on an unsupported Python version.
if sys.version_info < (3, 7):
    print("Synapse requires Python 3.7 or above.")
    sys.exit(1)

# Allow using the asyncio reactor via env var.
if bool(os.environ.get("SYNAPSE_ASYNC_IO_REACTOR", False)):
    try:
        from incremental import Version

        import twisted

        # We need a bugfix that is included in Twisted 21.2.0:
        # https://twistedmatrix.com/trac/ticket/9787
        if twisted.version < Version("Twisted", 21, 2, 0):
            print("Using asyncio reactor requires Twisted>=21.2.0")
            sys.exit(1)

        import asyncio

        from twisted.internet import asyncioreactor

        asyncioreactor.install(asyncio.get_event_loop())
    except ImportError:
        pass

# Twisted and canonicaljson will fail to import when this file is executed to
# get the __version__ during a fresh install. That's OK and subsequent calls to
# actually start Synapse will import these libraries fine.
try:
    from twisted.internet import protocol
    from twisted.internet.protocol import Factory
    from twisted.names.dns import DNSDatagramProtocol

    protocol.Factory.noisy = False
    Factory.noisy = False
    DNSDatagramProtocol.noisy = False
except ImportError:
    pass

# Use the standard library json implementation instead of simplejson.
try:
    from canonicaljson import set_json_library

    set_json_library(json)
except ImportError:
    pass

__version__ = get_distribution_version_string("matrix-synapse")

if bool(os.environ.get("SYNAPSE_TEST_PATCH_LOG_CONTEXTS", False)):
    # We import here so that we don't have to install a bunch of deps when
    # running the packaging tox test.
    from synapse.util.patch_inline_callbacks import do_patch

    do_patch()

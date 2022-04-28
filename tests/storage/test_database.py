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

from synapse.storage.database import make_tuple_comparison_clause

from tests import unittest


class TupleComparisonClauseTestCase(unittest.TestCase):
    def test_native_tuple_comparison(self):
        clause, args = make_tuple_comparison_clause([("a", 1), ("b", 2)])
        self.assertEqual(clause, "(a,b) > (?,?)")
        self.assertEqual(args, [1, 2])

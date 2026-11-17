"""The brief's canonical walkthrough, as reusable JSONL records.

Customer C history
------------------
* ``c-v1``: business [0, 20), knowledge [0, +inf) -> S1, priority 0
* ``c-v2``: correction, business [5, 15), knowledge [10, +inf) -> S2, priority 1
* ``c-del``: business [6, 9), knowledge [12, +inf) -> deletion, priority 2

Store history
-------------
* S1 always East: ``s1-east`` business [int64-min, +inf), knowledge
  [0, +inf), priority 0
* S2: West on business [0, 8), North on [8, +inf), both knowledge [0, +inf),
  priority 0; plus a same-priority South correction on business [8, 10),
  knowledge [11, +inf) to force a level-2 conflict.

Orders (customer C) and the expected disposition:

    row  event known amount | store(level 1)        | region(level 2) | result
    O1     7     9     10   | S1 (correction not    | East            | fact East
                           |  known yet)            |                 |
    O2     7    10     20   | S2 (correction known)  | West (t<8)      | fact West
    O3     8    10     30   | S2                     | North (t>=8)    | fact North
    O4    15    10     40   | S1 (outside [5,15))    | East            | fact East
    O5     7    12     50   | DELETED (prio 2); the  | --              | quarantine
                           |  lower-priority S2 may  |                 | deleted
                           |  NOT be used as fallback|                 |
    O6     8    11     60   | S2                     | North vs South  | quarantine
                           |                        | same-priority   | conflict
"""

from __future__ import annotations

MIN_I64 = -(2 ** 63)
MAX_I64 = 2 ** 63 - 1
INF = None

CUSTOMER_HISTORY = [
    {"version_id": "c-v1", "key": "C", "valid_from": 0, "valid_to": 20,
     "known_from": 0, "known_to": INF, "priority": 0, "deleted": False,
     "value": "S1"},
    {"version_id": "c-v2", "key": "C", "valid_from": 5, "valid_to": 15,
     "known_from": 10, "known_to": INF, "priority": 1, "deleted": False,
     "value": "S2"},
    {"version_id": "c-del", "key": "C", "valid_from": 6, "valid_to": 9,
     "known_from": 12, "known_to": INF, "priority": 2, "deleted": True,
     "value": None},
]

STORE_HISTORY = [
    {"version_id": "s1-east", "key": "S1", "valid_from": MIN_I64,
     "valid_to": INF, "known_from": 0, "known_to": INF, "priority": 0,
     "deleted": False, "value": "East"},
    {"version_id": "s2-west", "key": "S2", "valid_from": 0, "valid_to": 8,
     "known_from": 0, "known_to": INF, "priority": 0, "deleted": False,
     "value": "West"},
    {"version_id": "s2-north", "key": "S2", "valid_from": 8, "valid_to": INF,
     "known_from": 0, "known_to": INF, "priority": 0, "deleted": False,
     "value": "North"},
    {"version_id": "s2-south", "key": "S2", "valid_from": 8, "valid_to": 10,
     "known_from": 11, "known_to": INF, "priority": 0, "deleted": False,
     "value": "South"},
]

ORDERS = [
    {"row_id": "O1", "customer": "C", "event_time": 7, "known_time": 9,
     "amount": 10},
    {"row_id": "O2", "customer": "C", "event_time": 7, "known_time": 10,
     "amount": 20},
    {"row_id": "O3", "customer": "C", "event_time": 8, "known_time": 10,
     "amount": 30},
    {"row_id": "O4", "customer": "C", "event_time": 15, "known_time": 10,
     "amount": 40},
    {"row_id": "O5", "customer": "C", "event_time": 7, "known_time": 12,
     "amount": 50},
    {"row_id": "O6", "customer": "C", "event_time": 8, "known_time": 11,
     "amount": 60},
]

EXPECTED_FACTS = {
    "O1": ("S1", "East", 10),
    "O2": ("S2", "West", 20),
    "O3": ("S2", "North", 30),
    "O4": ("S1", "East", 40),
}
EXPECTED_QUARANTINE = {
    "O5": ("customer", "deleted"),
    "O6": ("store", "conflict"),
}
EXPECTED_REGION_SUMS = {"East": 50, "West": 20, "North": 30}
